"""Mandatory routing, retained originals, retries and Arabic-dub bypass."""
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import subprocess
import types
import unittest
from unittest.mock import patch

import agent
import library_processing as lp
import processing_worker as worker
import stremio as s
from test_audit import Sandbox

ARABIC = [{"type": "subtitles", "properties": {"track_name": "العربية", "language": "ara"}}]
DUB = [{"type": "audio", "properties": {"language": "ara"}}]


class Tests(Sandbox):
    def setUp(self):
        super().setUp()
        self.root_patch = patch.object(lp, "ROOT", Path(self.tmp, "processing"))
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        super().tearDown()

    def record(self, manual=False, suffix=".mkv"):
        source = Path(self.episode("Show - S01E01" + suffix, data=b"original bytes", folder=self.lib))
        destination = source if manual else Path(self.lib, "ready.mkv")
        key = lp.key_for(destination)
        folder = lp.ROOT / key
        folder.mkdir(parents=True)
        (folder / "raw.media").write_bytes(source.read_bytes())
        (folder / "media.mkv").write_bytes(b"processed bytes")
        record = {"source": str(source), "destination": str(destination), "manual": manual,
                  "input_signature": lp.signature(source), "status": "pending", "stage": "Waiting",
                  "backup": "original.test" + suffix, "metadata": {"type": "series", "meta": {"name": "Show"}}}
        lp.save_record(key, record)
        return key, folder, source, destination, record

    def test_publication_requires_arabic_and_preserves_original_on_failure(self):
        key, folder, source, destination, record = self.record(manual=True)
        with patch.object(agent, "tracks", return_value=[]):
            with self.assertRaises(RuntimeError):
                lp.publish(key, record, folder)
        self.assertEqual(source.read_bytes(), b"original bytes")
        self.assertFalse(self.toasts)

    def test_manual_publication_keeps_exact_backup_and_replaces_atomically(self):
        key, folder, source, destination, record = self.record(manual=True)
        with patch.object(agent, "tracks", return_value=ARABIC):
            lp.publish(key, record, folder)
        self.assertEqual(destination.read_bytes(), b"processed bytes")
        self.assertEqual((folder / record["backup"]).read_bytes(), b"original bytes")
        self.assertEqual(record["status"], "done")
        self.assertEqual(self.toasts, ["Kizuna: ready"])

    def test_changed_manual_file_is_never_overwritten(self):
        key, folder, source, destination, record = self.record(manual=True)
        source.write_bytes(b"a new version arrived during processing")
        with patch.object(agent, "tracks", return_value=ARABIC):
            with self.assertRaises(RuntimeError):
                lp.publish(key, record, folder)
        self.assertEqual(source.read_bytes(), b"a new version arrived during processing")

    def test_manual_non_mkv_gets_correct_container_extension_and_original_backup(self):
        key, folder, source, destination, record = self.record(manual=True, suffix=".mp4")
        with patch.object(agent, "tracks", return_value=ARABIC):
            lp.publish(key, record, folder)
        self.assertFalse(source.exists())
        self.assertEqual(source.with_suffix(".mkv").read_bytes(), b"processed bytes")
        self.assertEqual((folder / record["backup"]).read_bytes(), b"original bytes")

    def test_arabic_dub_manual_file_is_unchanged(self):
        key, folder, source, destination, record = self.record(manual=True)
        before = lp.signature(source)
        with patch.object(agent, "tracks", return_value=DUB):
            lp.publish(key, record, folder)
        self.assertEqual(lp.signature(source), before)
        self.assertEqual(source.read_bytes(), b"original bytes")
        self.assertFalse(self.toasts)

    def test_burned_in_arabic_marks_done_without_touching_the_file(self):
        key, folder, source, destination, record = self.record(manual=True)
        before = lp.signature(source)
        class Process:
            returncode = 0
            def __enter__(self):
                (folder / "result.json").write_text(json.dumps(
                    {"ready": False, "leave_unchanged": True, "stage": "Arabic is burned into the video; nothing to add"}))
                return self
            def __exit__(self, *args): pass
            def poll(self): return 0
        with patch.object(lp.subprocess, "Popen", return_value=Process()):
            result = lp.process_record(key, record)
        self.assertEqual(result["status"], "done")
        self.assertNotIn("retry_at", {k: v for k, v in result.items() if v})
        self.assertEqual(lp.signature(source), before)
        self.assertFalse(self.toasts)

    def test_missing_subtitles_retains_download_and_retries_without_publishing(self):
        key, folder, source, destination, record = self.record()
        class Process:
            returncode = 0
            def __enter__(self):
                (folder / "result.json").write_text(json.dumps({"ready": False, "stage": "No Arabic released yet"}))
                return self
            def __exit__(self, *args): pass
            def poll(self): return 0
        with patch.object(lp.subprocess, "Popen", return_value=Process()):
            result = lp.process_record(key, record)
        self.assertEqual(result["status"], "waiting")
        self.assertFalse(destination.exists())
        self.assertEqual((folder / "raw.media").read_bytes(), b"original bytes")
        self.assertIn("retry_at", result)
        self.assertFalse(self.toasts)

    def test_submit_moves_verified_stremio_temp_to_durable_queue(self):
        source = Path(self.episode("download.stremio.part"))
        destination = Path(self.lib, "Show.mkv")
        def wait(key, record, progress=None):
            record.update(status="waiting", stage="Arabic pending")
            lp.save_record(key, record)
            return record
        with patch.object(lp, "process_record", side_effect=wait):
            record = lp.submit(source, destination, {"type": "movie", "meta": {"name": "Show"}})
        self.assertFalse(source.exists())
        self.assertFalse(destination.exists())
        self.assertTrue((lp.ROOT / lp.key_for(destination) / "raw.media").exists())
        self.assertEqual(record["status"], "waiting")

    def test_recursive_manual_discovery_waits_for_stability_and_ignores_partial_files(self):
        video = Path(self.episode("Show/Season 01/Show - S01E01.mp4", folder=self.lib))
        self.episode("Show/incomplete.stremio.part", folder=self.lib)
        with patch.object(s, "libraries", return_value=[{"path": self.lib}]), patch.object(lp, "submit") as submit:
            with patch.object(lp.time, "time", return_value=100):
                observed = lp.poll({})
            submit.assert_not_called()
            with patch.object(lp.time, "time", return_value=161):
                lp.poll(observed)
            submit.assert_called_once_with(video, video, manual=True)

    def test_numbered_episode_in_season_folder_is_series(self):
        meta = lp.metadata_for(Path(self.lib, "Show (2024)", "Season 11", "07.mkv"))
        self.assertEqual((meta["type"], meta["season"], meta["episode"], meta["meta"]["name"]), ("series", 11, 7, "Show"))

    def test_worker_routes_sidecar_to_same_pipeline_without_weekly_delivery(self):
        folder = Path(self.tmp, "isolated")
        folder.mkdir()
        (folder / "metadata.json").write_text(json.dumps({"type": "movie", "meta": {"name": "Movie"}}))
        (folder / "raw.media").write_bytes(b"raw")
        (folder / "arabic.srt").write_text("Arabic test subtitle")
        with patch.object(agent, "tracks", return_value=[]), patch.object(worker, "fetch_arabic") as fetch, \
                patch.object(agent, "process", return_value=True) as process:
            self.assertTrue(worker.prepare(folder))
            fetch.assert_not_called()
            self.assertFalse(process.call_args.kwargs["publish"])
            self.assertEqual(process.call_args.kwargs["subtitle_path"], str(folder / "arabic.srt"))

    def test_already_completed_record_is_not_prepared_again(self):
        key, folder, source, destination, record = self.record(manual=True)
        record.update(status="done", completed_signature=lp.signature(destination))
        lp.save_record(key, record)
        with patch.object(lp, "process_record") as process:
            result = lp.submit(source, destination, manual=True)
            process.assert_not_called()
        self.assertEqual(result["status"], "done")

    def test_retry_does_not_overwrite_a_job_completed_by_another_thread(self):
        key, folder, source, destination, record = self.record()
        done = dict(record, status="done")
        with patch.object(lp, "read", side_effect=[{key: record}, {key: done}, {key: done}]), \
                patch.object(s, "libraries", return_value=[]), patch.object(lp, "process_record") as process:
            lp.poll({})
            process.assert_not_called()

    def test_restart_does_not_spawn_over_a_surviving_worker(self):
        key, folder, source, destination, record = self.record()
        (folder / "worker.pid").write_text("42", encoding="ascii")
        with patch("psutil.Process", return_value=types.SimpleNamespace(cmdline=lambda: ["python", "processing_worker.py", str(folder)])), \
                patch.object(lp.subprocess, "Popen") as spawn:
            result = lp.process_record(key, record)
        self.assertEqual(result["status"], "waiting")
        self.assertIn("still finishing", result["stage"])
        spawn.assert_not_called()

    def test_conan_preserves_manual_typesetting_exception(self):
        folder = Path(self.tmp, "conan")
        folder.mkdir()
        (folder / "metadata.json").write_text(json.dumps({"type": "series", "meta": {"name": "Detective Conan"}}))
        (folder / "raw.media").write_bytes(b"raw")
        with patch.object(agent, "tracks", return_value=[]), patch.object(agent, "process") as process:
            self.assertFalse(worker.prepare(folder))
            process.assert_not_called()

    def test_bleach_is_fansub_first_and_waits_when_nothing_found(self):
        # Bleach (by id or by name) asks the picker in fansub_first mode; with no Arabic anywhere (add-ons
        # nor inside the file) the episode waits instead of being processed.
        for meta in ({"name": "Bleach"}, {"name": "المطهر"}, {"name": "Something", "id": "tt0434665"}):
            folder = Path(self.tmp, "bleach-" + str(abs(hash(json.dumps(meta)))))
            folder.mkdir()
            (folder / "metadata.json").write_text(json.dumps({"type": "series", "season": 11, "episode": 1, "meta": meta}))
            (folder / "raw.media").write_bytes(b"raw")
            with patch.object(agent, "tracks", return_value=[]), patch.object(agent, "process") as process, \
                    patch.object(agent, "arabic_track", return_value=None), \
                    patch.object(worker, "fetch_arabic", return_value=None) as fetch:
                self.assertFalse(worker.prepare(folder), meta)
                process.assert_not_called()
                self.assertEqual(fetch.call_args.args[2], "fansub_first")

    def test_real_dub_runs_through_isolated_worker_and_publishes(self):
        source = Path(self.lib, "dub.stremio.part")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:d=1", "-f", "lavfi", "-i",
                        "sine=duration=1", "-c:v", "mpeg4", "-c:a", "aac", "-metadata:s:a:0", "language=ara",
                        "-f", "matroska", str(source)], check=True, capture_output=True)
        destination = Path(self.lib, "Dub.mkv")
        # The worker script location is real; all data and ledgers remain in the sandbox.
        with patch.object(agent, "HERE", str(Path(__file__).parent)):
            result = lp.submit(source, destination, {"type": "movie", "meta": {"name": "Dub"}})
        self.assertEqual(result["status"], "done", result)
        self.assertTrue(destination.exists())
        self.assertTrue(agent.has_arabic_audio(agent.tracks(str(destination))))
        self.assertEqual(self.toasts, ["Kizuna: movie ready"])

    def test_real_subtitle_extraction_style_mux_and_verification_before_delivery(self):
        folder = Path(self.tmp, "styled")
        folder.mkdir()
        subtitle = folder / "input.srt"
        subtitle.write_text("\n\n".join(f"{i}\n00:00:00,100 --> 00:00:04,500\nاختبار الترجمة {i}" for i in range(1, 111)), encoding="utf-8")
        (folder / "metadata.json").write_text(json.dumps({"type": "movie", "meta": {"name": "Movie"}}))
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:d=5", "-f", "lavfi", "-i",
                        "sine=duration=5", "-i", str(subtitle), "-map", "0:v", "-map", "1:a", "-map", "2:s",
                        "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "srt", "-metadata:s:a:0", "language=jpn",
                        "-metadata:s:s:0", "language=ara", "-f", "matroska", str(folder / "raw.media")], check=True, capture_output=True)
        with patch.object(agent, "timing", return_value=(0, True)) as timing:
            self.assertTrue(worker.prepare(folder))
            timing.assert_called_once()
        tracks = agent.tracks(str(folder / "media.mkv"))
        arabic = [t for t in tracks if t["type"] == "subtitles" and t["properties"].get("track_name") == "العربية"]
        self.assertEqual(len(arabic), 1)
        self.assertTrue(arabic[0]["properties"]["default_track"])
        self.assertFalse(self.toasts)  # Preparation alone must never announce library readiness.
        self.assertEqual(agent.dialogue_hash(folder / "ar.ass"), agent.dialogue_hash(folder / "emb.ass"))


if __name__ == "__main__":
    unittest.main()
