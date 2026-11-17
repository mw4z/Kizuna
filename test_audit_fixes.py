"""Audit regressions: temporary media, mocked providers, no real library changes."""
import ast
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np
import agent
import audio_fit
import cleanup
import stremio as s
import stremio_batch as b
from test_audit import Sandbox, ARABIC


class RecoveryAndCleanup(Sandbox):
    def test_cleanup_refuses_sibling_backup_and_escaped_paths(self):
        backup = self.lib + " Backup/Show/ep.mkv"
        with patch.object(cleanup, "due", return_value=[(backup, 0, 0)]), patch.object(cleanup, "recycle") as recycle:
            cleanup.run_once()
            recycle.assert_not_called()
        conn = sqlite3.connect(":memory:")
        conn.executescript("CREATE TABLE BaseItems(Id,Path,Type); CREATE TABLE UserData(ItemId,LastPlayedDate,Played);")
        valid = os.path.join(self.lib, "Show", "ep.mkv")
        for i, path in enumerate((backup, valid)):
            conn.execute("INSERT INTO BaseItems VALUES(?,?,?)", (i, path, "Media.Episode"))
            conn.execute("INSERT INTO UserData VALUES(?,?,?)", (i, "2026-01-01 00:00:00", 1))
        with patch.object(agent, "JELLYFIN_DB", os.path.join(self.lib, "none.db")), \
                patch.object(agent.sqlite3, "connect", return_value=conn):
            self.assertEqual([path for path, _ in cleanup.watched()], [valid])

    def test_jellyfin_db_reads_a_copy_never_the_live_files(self):
        # 2026-11-16: a mode=ro reader recreated jellyfin.db-shm/-wal as this user and locked the service out
        live = os.path.join(self.lib, "jellyfin.db")
        con = sqlite3.connect(live)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES (7)")
        con.commit()
        con.close()                                        # last close removes -wal/-shm, like Jellyfin's checkpoint
        before = sorted(os.listdir(self.lib))
        with patch.object(agent, "JELLYFIN_DB", live):
            with agent.jellyfin_db() as c:
                self.assertEqual(c.execute("select x from t").fetchall(), [(7,)])
                self.assertEqual(sorted(os.listdir(self.lib)), before)   # no -shm/-wal created next to the live db
        self.assertEqual(sorted(os.listdir(self.lib)), before)

    def test_restart_verifies_muxed_subtitle_and_delivers_once(self):
        video = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        ass = Path(agent.WORK, "expected.ass")
        ass.write_text("Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,test\n", encoding="utf-8")
        state = agent.load_state()
        state["delivery"][os.path.basename(video)] = agent.dialogue_hash(ass)
        agent.save_state(state)
        tree = ast.parse(Path(__file__).with_name("watcher.py").read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "needs_work")
        ns = {"agent": agent, "os": os, "json": json}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "watcher.py", "exec"), ns)

        def extract(*args):
            Path(args[-1].split(":", 1)[1]).write_bytes(ass.read_bytes())
            return types.SimpleNamespace(returncode=0)
        with patch.object(agent, "TARGET", video), patch.object(agent, "NOW", True), \
                patch.object(agent, "tracks", return_value=ARABIC), patch.object(agent, "run", side_effect=extract):
            self.assertTrue(ns["needs_work"](video))
            agent.main()
            self.assertTrue(Path(agent.library_target(os.path.basename(video))).exists())
            self.assertFalse(ns["needs_work"](video))
            agent.main()
        self.assertEqual(self.toasts.count("Anime FYA: ready"), 1)
        self.assertFalse(agent.load_state()["delivery"])

    def test_mismatched_recovery_never_copies(self):
        video = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        state = agent.load_state()
        state["delivery"][os.path.basename(video)] = "different hash"
        def extract(*args):
            Path(args[-1].split(":", 1)[1]).write_text("Dialogue: wrong\n", encoding="utf-8")
            return types.SimpleNamespace(returncode=0)
        with patch.object(agent, "run", side_effect=extract), patch.object(agent, "copy_to_library") as copy:
            with self.assertRaises(RuntimeError):
                agent.retry_copy(video, ARABIC, state)
            copy.assert_not_called()


class Timing(unittest.TestCase):
    def test_clear_known_audio_offset_still_measures_correctly(self):
        rng = np.random.default_rng(1)
        sig = np.repeat(rng.integers(0, 2, 1000), 20).astype(float)
        offset, strength = audio_fit.best_offset(np.roll(sig, 40), sig)
        self.assertEqual(offset, 2.0)
        self.assertGreater(strength, 3)

    def test_silence_and_weak_fit_never_change_timing(self):
        sig = np.tile(np.r_[np.ones(40), np.zeros(60)], 120)
        spans = [(i / 20, (i + 40) / 20) for i in range(0, len(sig), 100)]
        with patch.object(audio_fit, "audio_env", return_value=np.zeros(len(sig))), \
                patch.object(audio_fit, "cue_signal", return_value=(sig, spans)), patch.object(agent, "log"):
            self.assertEqual(agent.timing("unused", "unused"), (0.0, False))
        weak = {"global": 2, "strength": 1.1, "windows": [{"offset": 2, "strength": 1.1}] * 6}
        with patch.object(audio_fit, "fit", return_value=weak), patch.object(agent, "log"):
            self.assertEqual(agent.timing("unused", "unused"), (0.0, False))

    def test_audio_decode_failure_and_silence_are_rejected(self):
        media = os.path.join(tempfile.mkdtemp(), "silent.mkv")
        open(media, "wb").close()
        for code in (0, 1):
            with patch.object(audio_fit.subprocess, "run", return_value=types.SimpleNamespace(
                    returncode=code, stdout=np.zeros(audio_fit.SR * 5, dtype=np.float32).tobytes())):
                with self.assertRaises(ValueError):
                    audio_fit.audio_env(media)


class Batch(unittest.TestCase):
    def tearDown(self):
        s._items.clear()
        b._state = None
        b._cancel.clear()

    def test_queue_keeps_runtime_and_selected_track_roles(self):
        meta = {"name": "Show", "runtime": "24 min", "videos": [{"id": "ep1", "season": 1, "episode": 1}]}
        source = {"video": {"type": "series", "season": 1, "meta": meta}, "probe": {"streams": [
            {"index": 0, "codec_type": "video", "width": 1920, "height": 1080},
            {"index": 1, "codec_type": "audio", "tags": {"language": "eng", "title": "Commentary"}},
            {"index": 2, "codec_type": "subtitle", "tags": {"language": "eng"}, "disposition": {"forced": 1}}]}}
        with patch.object(s, "item", return_value=source), patch.object(s, "libraries", return_value=[{"id": "lib", "name": "Anime"}]), \
                patch.object(b, "launch") as launch, patch.object(b, "status"):
            b.start("key", "1", "2", "lib", "show")
        data = launch.call_args.args[0]
        self.assertEqual(data["meta"]["runtime"], "24 min")
        self.assertTrue(data["preferences"]["audio_role"]["commentary"])
        self.assertTrue(data["preferences"]["subtitle_role"]["forced"])
        with self.assertRaises(s.UserError):
            s.validate_media({"stream": {}, "video": {"season": 1, "meta": data["meta"]}},
                             {"format": {"duration": "120", "size": "1000000"}})

    def test_choose_preserves_main_audio_and_full_subtitles(self):
        source = {"stream": {"url": "https://example.com/episode"}, "probe": {"streams": [
            {"codec_type": "video", "width": 1920, "height": 1080}]}}
        info = {"dynamic_range": "SDR", "audio": [
            {"id": "1", "language": "eng", "label": "English commentary"},
            {"id": "2", "language": "eng", "label": "English main"}], "subtitles": [
            {"id": "3", "language": "eng", "label": "English forced"},
            {"id": "4", "language": "eng", "label": "English full"}]}
        data = {"preferences": {"audio": "en", "subtitle": "en", "resolution": "1080p", "hdr": "SDR", "provider": "Test"}}
        with patch.object(s, "streams", return_value={"failed_addons": 0, "sources": [
                {"key": "key", "quality": "1080p", "audio_languages": ["en"]}]}), \
                patch.object(s, "item", return_value=source), patch.object(s, "inspect", return_value=info):
            self.assertEqual(b.choose(data, {"kind": "video"}), ("key", "2", "4"))
            self.assertIsNone(b.choose(data, {"kind": "video"}, {b.source_identity("key")}))

    def test_failed_source_retries_alternative_but_disk_failure_pauses(self):
        for outcome in ({"status": "failed", "stage": "Source failed", "retryable": True},
                        {"status": "failed", "stage": "Not enough disk space", "retryable": False}):
            with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                data = {"name": "Show", "type": "series", "meta": {"name": "Show"}, "library_id": "lib",
                        "status": "running", "episodes": [{"id": "ep1", "season": 1, "episode": 1, "status": "pending"}]}
                stack.enter_context(patch.object(b, "FILE", Path(folder, "batch.json")))
                stack.enter_context(patch.object(s, "libraries", return_value=[{"id": "lib", "path": folder}]))
                stack.enter_context(patch.object(s.shutil, "disk_usage", return_value=types.SimpleNamespace(free=10**12)))
                stack.enter_context(patch.object(b, "source_identity", side_effect=lambda key: key))
                choose = stack.enter_context(patch.object(b, "choose", side_effect=[("bad", "1", "none"), ("good", "1", "none")]))
                download = stack.enter_context(patch.object(s, "start_download", side_effect=[outcome, {"status": "done", "stage": "Saved"}]))
                b._busy.acquire()
                b.worker(data)
                if outcome["retryable"]:
                    self.assertEqual(data["episodes"][0]["status"], "done")
                    self.assertEqual(download.call_count, 2)
                    self.assertEqual(choose.call_args.args[2], {"bad"})
                else:
                    self.assertEqual(data["status"], "paused")
                    self.assertEqual(data["episodes"][0]["status"], "pending")
                    self.assertEqual(download.call_count, 1)


class Deadline(unittest.TestCase):
    def test_endless_source_is_killed_and_never_published(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            stack.enter_context(patch.object(s, "JOB", root / "job.json"))
            stack.enter_context(patch.object(s, "input_args", return_value=[]))
            stack.enter_context(patch.object(agent, "log"))
            notify = stack.enter_context(patch.object(agent, "notify"))
            popen = stack.enter_context(patch.object(s.subprocess, "Popen"))
            proc = popen.return_value.__enter__.return_value
            proc.poll.return_value = None
            proc.stdout = []
            proc.wait.return_value = 1
            timer = stack.enter_context(patch.object(s.threading, "Timer"))
            timer.side_effect = lambda seconds, expire: types.SimpleNamespace(
                start=expire, cancel=Mock(), daemon=False)
            source = {"stream": {}, "probe": {"streams": [], "format": {"duration": "1200"}}}
            # the fake timer fires at once; the limit counts real downloading time, so start it long ago
            job = {"name": "Test", "status": "running", "library": "Test", "started": time.time() - 10000}
            dest = root / "episode.mkv"
            s._job_lock.acquire()
            s.download(source, "none", "none", dest, job)
            self.assertTrue(proc.kill.called)
            self.assertEqual(timer.call_args.args[0], 9600)
            self.assertEqual(job["status"], "failed")
            self.assertTrue(job["retryable"])
            self.assertIn("time limit", job["stage"])
            self.assertFalse(dest.exists())
            notify.assert_not_called()
            self.assertFalse(s._job_lock.locked())


if __name__ == "__main__":
    unittest.main()
