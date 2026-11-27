"""Regression tests for the 2026-11-18 reliability audit (findings 1-20). Real Windows file locks, real
ffmpeg/ffprobe/mkvmerge on tiny temp files, simulated crashes. Never touches real media or library folders."""
import os
os.environ.setdefault("KIZUNA_SUBTITLE_LANGUAGE", "ar")   # this suite tests the Arabic pipeline
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import agent
import audio_fit
import egress
import library_processing as lp
import mux
import processing_worker as worker
import srt2ass
import stremio as s
import stremio_batch as b
from test_audit import Sandbox, AgentFailures, import_watcher

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
real_sleep = time.sleep
ASS = ("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
       "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
       "Alignment,MarginL,MarginR,MarginV,Encoding\nStyle: Default,Cairo,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
       "0,0,0,0,100,100,0,0,1,2,1,2,10,10,10,1\n\n[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,"
       "Effect,Text\nDialogue: 0,0:00:00.20,0:00:01.50,Default,,0,0,0,,مرحبا\n")


def make_video(path, seconds=2, rate=10):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=blue:s=64x36:d={seconds}:r={rate}",
                        "-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
                        str(path)], capture_output=True, creationflags=NO_WINDOW)
    assert r.returncode == 0, r.stderr[-300:]
    return str(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, creationflags=NO_WINDOW)
    return float(json.loads(r.stdout or b"{}").get("format", {}).get("duration") or 0)


def hold(path, seconds):
    """Keep `path` open (as a reader would) for `seconds` in a thread: Windows then refuses to replace it."""
    f = open(path, "rb")
    t = threading.Timer(seconds, f.close)
    t.start()
    return t


def only_port(port):
    def policy(host, p):
        if p != port:
            raise egress.Denied("test: only the local test server")
        return "127.0.0.1"
    return policy


# ---------------------------------------------------------------- 1, 19. proxy: idle timeout and SSRF
class SlowServer:
    """Plain HTTP server that ignores Range (no resume possible) and trickles `body` out over `seconds`."""
    def __init__(self, body, seconds=3.0, silent=False):
        self.body, self.seconds, self.silent = body, seconds, silent
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.one, args=(conn,), daemon=True).start()

    def one(self, conn):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            if self.silent:                       # accept, then never answer: a dead upstream
                time.sleep(30)
                return
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: video/x-matroska\r\nContent-Length: "
                         + str(len(self.body)).encode() + b"\r\nAccept-Ranges: none\r\nConnection: close\r\n\r\n")
            steps = 15
            size = -(-len(self.body) // steps)
            for i in range(0, len(self.body), size):
                conn.sendall(self.body[i:i + size])
                time.sleep(self.seconds / steps)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()


class ProxyTimeouts(unittest.TestCase):
    def fetch(self, proxy, port, timeout=20):
        c = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=timeout)
        c.sendall(f"GET http://127.0.0.1:{port}/v.mkv HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        data = b""
        try:
            while chunk := c.recv(65536):
                data += chunk
        finally:
            c.close()
        return data

    def test_streaming_longer_than_the_idle_limit_is_not_cut(self):
        body = os.urandom(300_000)
        server = SlowServer(body, seconds=3)              # 3 s of steady data, idle limit 1 s
        proxy = egress.Proxy(only_port(server.port))
        try:
            with mock.patch.object(egress, "IDLE", 1):
                data = self.fetch(proxy, server.port)
            self.assertTrue(data.endswith(body), f"got {len(data)} bytes")
        finally:
            proxy.shutdown()
            proxy.server_close()
            server.close()

    def test_dead_tunnel_still_closes(self):
        server = SlowServer(b"", silent=True)
        proxy = egress.Proxy(only_port(server.port))
        try:
            with mock.patch.object(egress, "IDLE", 1):
                t0 = time.time()
                data = self.fetch(proxy, server.port, timeout=25)
            self.assertEqual(data, b"")
            self.assertLess(time.time() - t0, 8)          # no connection hangs forever
        finally:
            proxy.shutdown()
            proxy.server_close()
            server.close()

    def test_real_ffmpeg_download_without_resume_support_completes(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_video(Path(d, "src.mkv"), seconds=8)
            server = SlowServer(Path(src).read_bytes(), seconds=4)
            proxy = egress.Proxy(only_port(server.port))
            out = Path(d, "out.mkv")
            try:
                with mock.patch.object(egress, "IDLE", 1):
                    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-protocol_whitelist", egress.PROTOCOLS,
                                        "-rw_timeout", "30000000", "-i", f"http://127.0.0.1:{server.port}/src.mkv",
                                        "-map", "0", "-c", "copy", str(out)],
                                       capture_output=True, env=egress.ffmpeg_env(proxy), timeout=60, creationflags=NO_WINDOW)
                self.assertEqual(r.returncode, 0, r.stderr[-300:])
                self.assertAlmostEqual(duration(out), duration(src), delta=0.3)
            finally:
                proxy.shutdown()
                proxy.server_close()
                server.close()


class StremioLocalServerLockdown(unittest.TestCase):
    def ask(self, request):
        proxy = egress.Proxy()                             # the real policy
        try:
            c = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=10)
            c.sendall(request.encode())
            reply = c.recv(200)
            c.close()
            return reply, list(proxy.denied)
        finally:
            proxy.shutdown()
            proxy.server_close()

    def test_forwarding_endpoints_and_tunnels_are_refused(self):
        for req in ("GET http://127.0.0.1:11470/proxy/d=http%3A%2F%2F192.168.0.1/ HTTP/1.1\r\nHost: x\r\n\r\n",
                    "GET http://127.0.0.1:11470/hlsv2/a/master.m3u8?mediaURL=http%3A%2F%2F192.168.100.1 HTTP/1.1\r\nHost: x\r\n\r\n",
                    "GET http://localhost:11470/" + "ab" * 20 + "/0?x=1 HTTP/1.1\r\nHost: x\r\n\r\n",
                    "CONNECT 127.0.0.1:11470 HTTP/1.1\r\nHost: x\r\n\r\n",
                    "GET http://127.0.0.1:8096/Items HTTP/1.1\r\nHost: x\r\n\r\n",          # Jellyfin
                    "GET http://192.168.100.1/ HTTP/1.1\r\nHost: x\r\n\r\n",                # router
                    "GET http://2130706433:8096/ HTTP/1.1\r\nHost: x\r\n\r\n",              # decimal 127.0.0.1
                    "CONNECT [::ffff:192.168.0.1]:443 HTTP/1.1\r\nHost: x\r\n\r\n"):
            reply, denied = self.ask(req)
            self.assertTrue(reply.startswith(b"HTTP/1.1 403"), req)
            self.assertTrue(denied, req)

    def test_only_torrent_file_streams_are_allowed_locally(self):
        self.assertTrue(egress.local_media_path("/" + "a" * 40 + "/3"))
        self.assertTrue(egress.local_media_path("/" + "AB" * 20 + "/0"))
        for bad in ("/proxy/x", "/" + "a" * 40 + "/3/", "/" + "a" * 39 + "/3", "/hlsv2/x/master.m3u8", "/stats.json", ""):
            self.assertFalse(egress.local_media_path(bad), bad)
        self.assertFalse(egress.local_media_path("/" + "a" * 40 + "/3", "mediaURL=x"))


# ---------------------------------------------------------------- 2. job file vs status polling (real Windows locks)
class AtomicWrites(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_download_progress_survives_concurrent_status_polling(self):
        errors, done = [], threading.Event()
        with mock.patch.object(s, "JOB", self.d / "job.json"):
            s.write_job({"status": "running", "pct": 0})
            def poll():
                while not done.is_set():
                    try:
                        s.job_status()
                    except Exception as e:      # noqa: BLE001
                        errors.append(e)
            readers = [threading.Thread(target=poll) for _ in range(4)]
            for t in readers:
                t.start()
            try:
                job = {"status": "running"}
                for i in range(400):
                    job["pct"] = i
                    s.write_job(job)
            except Exception as e:              # noqa: BLE001
                errors.append(e)
            finally:
                done.set()
                for t in readers:
                    t.join()
            self.assertEqual(errors, [])
            self.assertEqual(json.loads(s.JOB.read_text(encoding="utf-8"))["pct"], 399)

    def test_another_process_reading_does_not_fail_the_writer(self):
        target = self.d / "progress.json"
        agent.write_atomic(target, "{}")
        reader = subprocess.Popen([sys.executable, "-c",
                                   "import sys,time\nend=time.time()+4\n"
                                   "while time.time()<end:\n"
                                   "    try: open(sys.argv[1],'rb').read()\n"
                                   "    except OSError: pass\n", str(target)], creationflags=NO_WINDOW)
        try:
            for i in range(300):
                agent.write_atomic(target, json.dumps({"i": i}))
        finally:
            reader.wait(30)
        self.assertEqual(json.loads(target.read_text())["i"], 299)
        self.assertEqual(list(self.d.glob("*.tmp")), [])

    def test_long_lock_fails_cleanly_and_keeps_the_old_file(self):
        target = self.d / "state.json"
        target.write_text('{"old": 1}')
        t = hold(target, 4)
        try:
            with self.assertRaises(PermissionError):
                agent.write_atomic(target, '{"new": 1}')
        finally:
            t.join()
        self.assertEqual(json.loads(target.read_text()), {"old": 1})       # interrupted write: old file intact
        self.assertEqual(list(self.d.glob("*.tmp")), [])                  # and no temp file left behind


class ProgressAndStateWrites(Sandbox):
    def test_locked_progress_file_never_raises(self):
        agent.show("Ep", "one", 1)
        t = hold(agent.PFILE, 4)
        try:
            agent.show("Ep", "two", 2)                                     # gives up quietly: progress is cosmetic
        finally:
            t.join()
        agent.show("Ep", "three", 3)
        self.assertEqual(self.progress()["stage"], "three")

    def test_state_save_waits_out_a_short_read(self):
        state = agent.load_state()
        agent.save_state(state)
        t = hold(agent.STATE, 0.5)
        state["told"]["x"] = 1
        agent.save_state(state)
        t.join()
        self.assertEqual(agent.load_state()["told"], {"x": 1})


# ---------------------------------------------------------------- 3. mux: the original is only replaced by verified output
class MuxSafety(Sandbox):
    def setUp(self):
        super().setUp()
        self.video = make_video(Path(self.inbox, "Show - S01E01.mkv"))
        self.ass = Path(self.tmp, "ar.ass")
        self.ass.write_text(ASS, encoding="utf-8-sig")
        self.before = sha(self.video)
        self.tmp_mkv = self.video[:-4] + ".tmp.mkv"

    def test_locked_episode_keeps_original_and_verified_output(self):
        t = hold(self.video, 3)
        try:
            with self.assertRaises(mux.Locked) as e:
                mux.mux(self.video, str(self.ass))
            self.assertEqual(sha(self.video), self.before)
            self.assertTrue(os.path.exists(e.exception.tmp))
        finally:
            t.join()

    def test_failed_check_never_replaces_the_original(self):
        with self.assertRaises(mux.VerifyFailed):
            mux.mux(self.video, str(self.ass), check=lambda tmp: False)
        self.assertEqual(sha(self.video), self.before)
        self.assertFalse(os.path.exists(self.tmp_mkv))

    def test_failed_mkvmerge_leaves_no_partial_file(self):
        with self.assertRaises(RuntimeError):
            mux.mux(self.video, str(Path(self.tmp, "missing.ass")))
        self.assertEqual(sha(self.video), self.before)
        self.assertFalse(os.path.exists(self.tmp_mkv))

    def real_process(self):
        """agent.process with the real mux/mkvmerge/mkvextract; subtitle search and timing stubbed."""
        def convert(srt, ass, title, off=0, layout=None):
            Path(ass).write_text(ASS, encoding="utf-8-sig")
        with ExitStack() as st:
            for name, value in (("target_track", lambda v: 2), ("timing", lambda v, a: (0.0, True)), ("qc", lambda a: []),
                                ("extract_srt", lambda v, t, out: open(out, "w").write("1") or True)):
                st.enter_context(mock.patch.object(agent, name, value))
            st.enter_context(mock.patch.object(agent.srt2ass, "convert", convert))
            st.enter_context(mock.patch.object(agent.time, "sleep", lambda x: real_sleep(0.3)))
            return agent.process(self.video, agent.load_state(), publish=False)

    def test_stale_partial_tmp_is_never_swapped_in(self):
        Path(self.tmp_mkv).write_bytes(b"half-written leftover from a crash")
        self.assertTrue(self.real_process())
        names = [t["properties"].get("track_name") for t in agent.tracks(self.video)]
        self.assertIn("العربية", names)                                   # the real, verified result
        self.assertGreater(duration(self.video), 1.5)
        self.assertFalse(os.path.exists(self.tmp_mkv))

    def test_episode_locked_by_a_player_finishes_after_release(self):
        t = hold(self.video, 2)
        try:
            self.assertTrue(self.real_process())
        finally:
            t.join()
        self.assertIn("العربية", [t["properties"].get("track_name") for t in agent.tracks(self.video)])
        self.assertFalse(os.path.exists(self.tmp_mkv))


class MuxLoop(AgentFailures):
    def run_with_mux(self, fake_mux, video):
        ps = self.fake_pipeline()
        ps[5] = mock.patch.object(agent.mux, "mux", fake_mux)
        ps.append(mock.patch.object(agent.time, "sleep", lambda x: None))
        for p in ps:
            p.start()
        try:
            agent.process(video, agent.load_state(), publish=False)
        finally:
            for p in ps:
                p.stop()

    def test_progress_error_is_not_mistaken_for_a_locked_episode(self):
        v = self.episode("Show - S01E01.mkv", data=b"ORIGINAL")
        stale = v[:-4] + ".tmp.mkv"
        calls = []
        def fake_mux(video, ass, progress=None, check=None):
            calls.append(1)
            if len(calls) == 1:
                Path(stale).write_bytes(b"PARTIAL")               # mkvmerge got half way...
                raise PermissionError("progress.json is being read")   # ...then a non-lock PermissionError
            return "ok"
        self.run_with_mux(fake_mux, v)
        self.assertEqual(Path(v).read_bytes(), b"ORIGINAL")       # the partial file never replaced the episode
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(len(calls), 2)

    def test_verify_failure_is_needs_a_look_and_original_untouched(self):
        v = self.episode("Show - S01E01.mkv", data=b"ORIGINAL")
        state = agent.load_state()
        ps = self.fake_pipeline()
        ps[5] = mock.patch.object(agent.mux, "mux", mock.Mock(side_effect=mux.VerifyFailed("x")))
        for p in ps:
            p.start()
        try:
            agent.process(v, state, publish=False)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(Path(v).read_bytes(), b"ORIGINAL")
        self.assertIn("Show - S01E01.mkv", state["failed"])
        self.assertIn("needs a look", self.progress()["stage"])


# ---------------------------------------------------------------- 4, 6, 17. library queue
class LibraryQueue(Sandbox):
    def setUp(self):
        super().setUp()
        self.root_patch = mock.patch.object(lp, "ROOT", Path(self.tmp, "processing"))
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        super().tearDown()

    def stremio_record(self):
        destination = Path(self.lib, "Show", "Season 01", "Show - S01E01.mkv")
        key = lp.key_for(destination)
        folder = lp.ROOT / key
        folder.mkdir(parents=True)
        (folder / "raw.media").write_bytes(b"downloaded bytes")
        record = {"source": str(Path(self.tmp, "x.stremio.part")), "destination": str(destination), "manual": False,
                  "input_signature": [1, 1], "status": "pending", "stage": "Waiting", "backup": "original.1.1.mkv",
                  "metadata": {"type": "series", "season": 1, "episode": 1, "meta": {"name": "Show"}}}
        lp.save_record(key, record)
        return key, folder, destination, record

    def worker_result(self, folder, result):
        class Process:
            returncode = 0
            def __enter__(self):
                (folder / "result.json").write_text(json.dumps(result))
                return self
            def __exit__(self, *a): pass
            def poll(self): return 0
        return mock.patch.object(lp.subprocess, "Popen", return_value=Process())

    def test_burned_in_stremio_download_is_delivered_once(self):
        key, folder, destination, record = self.stremio_record()
        with self.worker_result(folder, {"ready": False, "leave_unchanged": True, "stage": "Arabic is burned in"}):
            result = lp.process_record(key, record)
        self.assertEqual(result["status"], "done")
        self.assertEqual(destination.read_bytes(), b"downloaded bytes")
        self.assertFalse((folder / "raw.media").exists())
        self.assertFalse(list(destination.parent.glob("*.part")))
        self.assertEqual(result["completed_signature"], lp.signature(destination))

    def test_deleted_manual_file_is_not_brought_back(self):
        key, folder, destination, record = self.stremio_record()
        record.update(manual=True, source=str(destination))          # a library file, deleted by the user meanwhile
        with self.worker_result(folder, {"ready": False, "leave_unchanged": True, "stage": "burned"}):
            lp.process_record(key, record)
        self.assertFalse(destination.exists())

    def test_crash_after_delivery_rename_is_recognised_on_restart(self):
        key, folder, destination, record = self.stremio_record()
        with mock.patch.object(lp.os, "rename", side_effect=lambda a, b: (os.replace(a, b), (_ for _ in ()).throw(SystemExit))):
            with self.worker_result(folder, {"ready": False, "leave_unchanged": True, "stage": "burned"}), \
                    self.assertRaises(SystemExit):
                lp.process_record(key, record)                  # "crash" right after the file landed
        saved = lp.read()[key]
        with mock.patch.object(lp.subprocess, "Popen") as spawn:
            result = lp.process_record(key, saved)
        spawn.assert_not_called()
        self.assertEqual(result["status"], "done")
        self.assertEqual(destination.read_bytes(), b"downloaded bytes")

    def test_retries_back_off_to_once_a_day(self):
        key, folder, destination, record = self.stremio_record()
        gaps = []
        for _ in range(8):
            with self.worker_result(folder, {"ready": False, "stage": "No Arabic yet"}):
                result = lp.process_record(key, record)
            gaps.append(round((result["retry_at"] - time.time()) / 3600))
        self.assertEqual(gaps, [1, 2, 4, 8, 16, 24, 24, 24])
        self.assertTrue((folder / "raw.media").exists())          # nothing lost while waiting
        with self.worker_result(folder, {"ready": False, "leave_unchanged": True, "stage": "burned"}):
            result = lp.process_record(key, record)
        self.assertNotIn("attempts", result)

    def test_failures_back_off_too(self):
        key, folder, destination, record = self.stremio_record()
        with mock.patch.object(lp.subprocess, "Popen", side_effect=OSError("boom")):
            first = lp.process_record(key, record)["retry_at"] - time.time()
            second = lp.process_record(key, record)["retry_at"] - time.time()
        self.assertAlmostEqual(first, 1200, delta=5)
        self.assertAlmostEqual(second, 2400, delta=5)

    def test_dubbed_library_file_is_skipped_before_any_copy(self):
        video = Path(self.episode("Show - S01E02.mkv", data=b"dub", folder=self.lib))
        dub = [{"type": "audio", "properties": {"language": "ara"}}]
        with mock.patch.object(agent, "tracks", return_value=dub), mock.patch.object(lp.shutil, "copy2") as copy, \
                mock.patch.object(lp, "process_record") as process:
            record = lp.submit(video, video, manual=True)
        copy.assert_not_called()
        process.assert_not_called()
        self.assertEqual(record["status"], "done")
        self.assertIn("dub", record["stage"].lower())
        self.assertEqual(video.read_bytes(), b"dub")


# ---------------------------------------------------------------- 5. watcher timeouts
class WatcherTimeout(Sandbox):
    def step(self, age):
        w = import_watcher()
        v = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        queued = []
        with mock.patch.object(w, "needs_work", lambda p: True), mock.patch.object(w, "settled", lambda p, l=None: True), \
                mock.patch.object(w, "run_agent", lambda p: None), \
                mock.patch.object(w, "queue", lambda p, d=0: queued.append((p, d))):
            w.pending.clear()
            w.first_seen.clear()
            w.first_seen[v] = time.time() - age
            w.pending[v] = 0
            w.step()
        return queued, w, v

    def test_file_that_always_times_out_is_dropped_after_two_days(self):
        queued, w, v = self.step(3 * 86400)
        self.assertEqual(queued, [])
        self.assertNotIn(v, w.first_seen)

    def test_recent_timeout_still_retries(self):
        queued, w, v = self.step(3600)
        self.assertEqual(queued, [(v, 1200)])


# ---------------------------------------------------------------- 7, 8, 9, 15, 16. download controls (real ffmpeg)
class DownloadControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.short = make_video(cls.dir / "short.mkv", seconds=3, rate=5)
        cls.long = make_video(cls.dir / "long.mkv", seconds=600, rate=1)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.submit = mock.Mock(side_effect=lambda path, destination, metadata, progress: (
            shutil.move(str(path), str(destination)), {"status": "done", "stage": "ok"})[1])
        self.patches = [mock.patch.object(s, "JOB", self.d / "job.json"), mock.patch.object(s.agent, "log"),
                        mock.patch.object(lp, "FILE", self.d / "queue.json"), mock.patch.object(lp, "ROOT", self.d / "q"),
                        mock.patch.object(lp, "submit", self.submit)]
        for p in self.patches:
            p.start()
        s._cancel_job.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        s._cancel_job.clear()
        if s._job_lock.locked():
            s._job_lock.release()
        shutil.rmtree(self.d, ignore_errors=True)

    def source(self, media, realtime=True, on_resolve=None):
        def input_args(source):
            if on_resolve:
                on_resolve()
            return (["-re"] if realtime else []) + ["-i", str(media)]
        probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(media)],
                                          capture_output=True, creationflags=NO_WINDOW).stdout)
        return {"video": {"type": "movie", "meta": {"name": "T"}}, "stream": {}, "probe": probe}, \
            mock.patch.object(s, "input_args", input_args)

    def run_download(self, source, job, dest, submit=None, background=False):
        s._job_lock.acquire()
        if submit:
            self.submit.side_effect = submit
        args = (source, "none", "none", dest, job)
        if background:
            t = threading.Thread(target=s.download, args=args)
            t.start()
            return t, self.submit
        s.download(*args)
        return None, self.submit

    def test_cancel_while_the_source_is_still_resolving(self):
        source, patch = self.source(self.long, on_resolve=s._cancel_job.set)
        dest = self.d / "lib" / "T.mkv"
        job = {"status": "running", "started": time.time()}
        t0 = time.time()
        with patch:
            _, sub = self.run_download(source, job, dest)
        self.assertLess(time.time() - t0, 20)                     # not the whole 10-minute file
        self.assertEqual((job["status"], job["retryable"]), ("cancelled", False))
        sub.assert_not_called()
        self.assertFalse(list((self.d / "lib").glob("*")))

    def test_cancel_during_verification_stops_before_processing(self):
        source, patch = self.source(self.short, realtime=False)
        dest = self.d / "lib" / "T.mkv"
        job = {"status": "running", "started": time.time()}
        with patch, mock.patch.object(s, "validate_media", side_effect=lambda *a: s._cancel_job.set()):
            _, sub = self.run_download(source, job, dest)
        self.assertEqual(job["status"], "cancelled")
        sub.assert_not_called()
        self.assertFalse(list((self.d / "lib").glob("*")))

    def test_cancel_wins_over_a_verification_failure(self):
        source, patch = self.source(self.short, realtime=False)
        job = {"status": "running", "started": time.time()}
        def bad(*a):
            s._cancel_job.set()
            raise s.UserError("placeholder video")
        with patch, mock.patch.object(s, "validate_media", side_effect=bad):
            self.run_download(source, job, self.d / "lib" / "T.mkv")
        self.assertEqual((job["status"], job["retryable"]), ("cancelled", False))

    def test_cancel_is_refused_once_preparation_owns_the_file(self):
        s.write_job({"status": "running", "phase": "processing"})
        s._job_lock.acquire()
        try:
            with self.assertRaises(s.UserError):
                s.cancel_download()
        finally:
            s._job_lock.release()
        self.assertFalse(s._cancel_job.is_set())

    def test_time_limit_still_stops_a_stalled_source(self):
        source, patch = self.source(self.long)
        job = {"status": "running", "started": time.time()}
        with patch, mock.patch.object(s, "download_limit", lambda d: 3):
            self.run_download(source, job, self.d / "lib" / "T.mkv")
        self.assertEqual(job["status"], "failed")
        self.assertIn("time limit", job["stage"])
        self.assertTrue(job["retryable"])

    def test_long_pause_does_not_count_against_the_time_limit(self):
        source, patch = self.source(self.short)                    # 3 s at real speed
        dest = self.d / "lib" / "T.mkv"
        job = {"status": "running", "started": time.time()}
        with patch, mock.patch.object(s, "download_limit", lambda d: 6), mock.patch.object(s, "PAUSE_RECHECK", 0.2):
            t, _ = self.run_download(source, job, dest, background=True)
            for _ in range(200):
                if s._job_process is not None:
                    break
                time.sleep(0.05)
            time.sleep(0.5)
            s.pause_download()
            time.sleep(8)                                          # paused well past the 6 s limit
            s.resume_download()
            t.join(60)
        self.assertEqual(job["status"], "done", job)
        self.assertTrue(dest.exists())
        self.assertAlmostEqual(duration(dest), 3, delta=0.5)

    def test_processing_key_is_saved_before_arabic_preparation(self):
        source, patch = self.source(self.short, realtime=False)
        dest = self.d / "lib" / "T.mkv"
        seen = {}
        def submit(path, destination, metadata, progress):
            seen.update(s.read_job())                              # what a restart right now would find
            shutil.move(str(path), str(destination))
            return {"status": "waiting", "stage": "No Arabic yet"}
        with patch:
            self.run_download(source, {"status": "running", "started": time.time()}, dest, submit=submit)
        self.assertEqual(seen.get("processing_key"), lp.key_for(dest))
        self.assertEqual(seen.get("phase"), "processing")


class RestartRecovery(Sandbox):
    def setUp(self):
        super().setUp()
        self.job_patch = mock.patch.object(s, "JOB", Path(self.tmp, "job.json"))
        self.job_patch.start()

    def tearDown(self):
        self.job_patch.stop()
        super().tearDown()

    def test_restart_during_arabic_preparation_follows_the_queue(self):
        dest = Path(self.lib, "T.mkv")
        key = lp.key_for(dest)
        lp.save_record(key, {"status": "waiting", "stage": "No Arabic released yet", "destination": str(dest), "pct": 0})
        s.write_job({"status": "running", "phase": "processing", "processing_key": key, "stage": "Preparing"})
        status = s.job_status()
        self.assertEqual(status["status"], "waiting")
        self.assertEqual(status["stage"], "No Arabic released yet")

    def test_old_finished_record_does_not_mark_a_new_download_done(self):
        dest = Path(self.lib, "T.mkv")
        key = lp.key_for(dest)
        lp.save_record(key, {"status": "done", "stage": "Finished last month", "destination": str(dest), "pct": 100})
        s.write_job({"status": "running", "phase": "processing", "processing_key": key, "stage": "Preparing",
                     "started": time.time() + 5})
        s._job_lock.acquire()
        try:
            status = s.job_status()
        finally:
            s._job_lock.release()
        self.assertEqual((status["status"], status["stage"]), ("running", "Preparing"))

    def test_restart_mid_download_removes_the_orphaned_part(self):
        part = Path(self.lib, "T.stremio.part")
        part.write_bytes(b"x" * 1000)
        s.write_job({"status": "running", "stage": "Downloading", "part": str(part)})
        self.assertEqual(s.job_status()["status"], "interrupted")
        self.assertFalse(part.exists())

    def test_part_is_kept_when_preparation_owns_it_or_job_is_live(self):
        part = Path(self.lib, "T.stremio.part")
        part.write_bytes(b"x")
        s.write_job({"status": "running", "phase": "processing", "part": str(part)})
        s.job_status()
        self.assertTrue(part.exists())
        s.write_job({"status": "running", "part": str(part)})
        s._job_lock.acquire()
        try:
            s.job_status()                                          # a live download: never touched
        finally:
            s._job_lock.release()
        self.assertTrue(part.exists())
        other = Path(self.lib, "T.mkv")
        other.write_bytes(b"x")
        s.write_job({"status": "running", "part": str(other)})      # only *.stremio.part is ever deleted
        s.job_status()
        self.assertTrue(other.exists())


class BatchCancel(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(b, "FILE", self.root / "batch.json")
        self.patch.start()
        b._state = None
        b._cancel.clear()

    def tearDown(self):
        self.patch.stop()
        b._state = None
        s._items.clear()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_cancel_pauses_the_batch_instead_of_trying_the_next_release(self):
        data = {"name": "T", "meta": {"name": "T", "releaseInfo": "2024"}, "type": "series", "scope": "show",
                "library": "T", "library_id": "T:0", "preferences": {}, "status": "running", "stage": "",
                "episodes": [{"id": f"tt1:1:{i}", "season": 1, "episode": i, "status": "pending"} for i in (1, 2)]}
        library = {"id": s.library_id("T", str(self.root)), "name": "T", "path": str(self.root)}
        b._busy.acquire()
        with mock.patch.object(s, "libraries", return_value=[library]) as libs, \
                mock.patch.object(s.shutil, "disk_usage", return_value=types.SimpleNamespace(free=10**12)), \
                mock.patch.object(b, "source_identity", side_effect=lambda k: k), \
                mock.patch.object(b, "choose", side_effect=[("one", "1", "none"), ("two", "1", "none")]), \
                mock.patch.object(s, "start_download", return_value={"status": "cancelled", "retryable": False,
                                                                     "stage": "Download cancelled."}) as start:
            libs.side_effect = lambda _legacy=False: [dict(library, id="T:0")] if _legacy else [library]
            b.worker(data)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(data["status"], "paused")
        self.assertEqual([e["status"] for e in data["episodes"]], ["pending", "pending"])
        self.assertEqual(data["library_id"], library["id"])               # old saved id migrated to the stable one


# ---------------------------------------------------------------- 10, 11. subtitle files
class SubtitleFiles(Sandbox):
    SRT = "1\n00:00:01,000 --> 00:00:02,000\nمرحبا يا صديقي\n\n2\n00:00:03,000 --> 00:00:04,000\nوداعا\n"

    def test_windows_1256_and_utf16_sidecars_become_utf8(self):
        for enc in ("cp1256", "utf-16", "utf-8-sig", "utf-8"):
            folder = Path(tempfile.mkdtemp(dir=self.tmp))
            video = Path(self.lib, "Movie.mkv")
            video.write_bytes(b"v")
            Path(self.lib, "Movie.ar.srt").write_bytes(self.SRT.replace("\n", "\r\n").encode(enc))
            lp.sidecar(video, folder)
            text = (folder / "arabic.srt").read_text(encoding="utf-8")
            self.assertIn("مرحبا يا صديقي", text, enc)
            out = folder / "a.ass"
            self.assertEqual(srt2ass.convert(str(folder / "arabic.srt"), str(out), "t"), 2, enc)

    def test_blank_lines_with_spaces_still_separate_cues(self):
        src = Path(self.tmp, "a.srt")
        src.write_text(self.SRT.replace("\n\n2", "\n \t\n2"), encoding="utf-8")
        out = Path(self.tmp, "a.ass")
        self.assertEqual(srt2ass.convert(str(src), str(out), "t"), 2)
        self.assertNotIn("-->", out.read_text(encoding="utf-8-sig"))


# ---------------------------------------------------------------- 12. movie names
class MovieNames(unittest.TestCase):
    def test_titles_and_years(self):
        cases = {"Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1": ("Dune Part Two", "2024"),
                 "Blade Runner 2049 (2017)": ("Blade Runner 2049", "2017"),
                 "1917 (2019)": ("1917", "2019"),
                 "Charlotte's Web (2006)": ("Charlotte's Web", "2006"),
                 "Collateral.2004.UHD.BluRay.2160p": ("Collateral", "2004"),
                 "Inception": ("Inception", "")}
        for stem, (name, year) in cases.items():
            self.assertEqual(lp.movie_name(stem), {"name": name, "releaseInfo": year}, stem)
        md = lp.metadata_for(Path("C:/Movies/Dune.Part.Two.2024.1080p.mkv"))
        self.assertEqual((md["type"], md["meta"]["name"]), ("movie", "Dune Part Two"))


# ---------------------------------------------------------------- 13. reference-track fallback
class ReferenceTracks(Sandbox):
    def test_short_signs_track_does_not_end_the_search(self):
        tracks = [{"id": i, "type": "subtitles", "properties": {"codec_id": "S_TEXT/UTF8", "language": "eng"}} for i in (2, 3)]
        with mock.patch.object(agent, "tracks", return_value=tracks), \
                mock.patch.object(agent, "extract_srt", return_value=True), \
                mock.patch.object(agent, "cue_shift", side_effect=[None, 2.35]) as shift:
            self.assertEqual(agent.reference_offset("v.mkv", "ar.srt"), 2.35)
        self.assertEqual(shift.call_count, 2)


# ---------------------------------------------------------------- 14. Jellyfin DB copies from the status page
class StatusCache(unittest.TestCase):
    def test_series_ids_copy_the_database_at_most_every_ten_minutes(self):
        import contextlib
        import status
        calls = []
        @contextlib.contextmanager
        def db():
            calls.append(1)
            yield types.SimpleNamespace(execute=lambda *a: types.SimpleNamespace(fetchall=lambda: [("A-B", "x/Show")]))
        with mock.patch.object(agent, "jellyfin_db", db), mock.patch.dict(status._ids, {"t": 0, "data": {}}):
            for _ in range(5):
                self.assertEqual(status._series_ids(), {"Show": "ab"})
            self.assertEqual(len(calls), 1)
            status._ids["t"] -= 601
            status._series_ids()
            self.assertEqual(len(calls), 2)


# ---------------------------------------------------------------- 17. repeated work
class RepeatedWork(Sandbox):
    def test_audio_is_decoded_once_per_file_version(self):
        media = Path(self.tmp, "m.mkv")
        media.write_bytes(b"x")
        noise = (os.urandom(audio_fit.SR * 4 * 6))
        import numpy as np
        pcm = np.frombuffer(noise, dtype=np.uint8).astype(np.float32).tobytes()[:audio_fit.SR * 4 * 6]
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stdout=pcm))
        audio_fit._audio_env.cache_clear()
        with mock.patch.object(audio_fit.subprocess, "run", run):
            a = audio_fit.audio_env(str(media))
            a[:] = 0                                               # a caller scribbling on its copy...
            b2 = audio_fit.audio_env(str(media))
            self.assertEqual(run.call_count, 1)
            self.assertGreater(b2.max(), 0)                        # ...doesn't poison the cache
            os.utime(media, ns=(1, 1))                             # file replaced (e.g. after the mux)
            audio_fit.audio_env(str(media))
            self.assertEqual(run.call_count, 2)
        audio_fit._audio_env.cache_clear()

    def test_worker_does_not_ask_the_addons_twice(self):
        folder = Path(self.tmp, "job")
        folder.mkdir()
        (folder / "metadata.json").write_text(json.dumps({"type": "series", "season": 1, "episode": 2, "meta": {"name": "Show"}}))
        (folder / "raw.media").write_bytes(b"raw")
        import hardsub
        with mock.patch.object(agent, "tracks", return_value=[]), mock.patch.object(agent, "target_track", return_value=None), \
                mock.patch.object(hardsub, "burned_in_subtitles", return_value=(False, 0, 20)), \
                mock.patch.object(worker.fansub, "mode_for", return_value="official_ok"), \
                mock.patch.object(worker, "fetch_subtitle", return_value=None) as fetch, \
                mock.patch.object(agent, "process", return_value=None) as process:
            worker.prepare(folder)
        fetch.assert_called_once()
        self.assertFalse(process.call_args.kwargs["addons"])

    def test_agent_skips_addons_when_told(self):
        v = self.episode("Show - S01E02.mkv")
        with mock.patch.object(agent, "tracks", return_value=[]), mock.patch.object(agent, "target_track", return_value=None), \
                mock.patch.object(agent, "nyaa_subtitle", return_value=False), \
                mock.patch.object(agent, "addon_subtitle") as addons:
            agent.process(v, agent.load_state(), publish=False, addons=False)
        addons.assert_not_called()


# ---------------------------------------------------------------- 18. library ids
class LibraryIds(unittest.TestCase):
    def test_ids_survive_a_missing_library_folder_and_old_ids_still_resolve(self):
        root = Path(tempfile.mkdtemp())
        try:
            folders = {}
            for name in ("Anime", "Movies", "Series"):
                folders[name] = root / "media" / name
                folders[name].mkdir(parents=True)
                (root / "cfg" / name).mkdir(parents=True)
                (root / "cfg" / name / "options.xml").write_text(
                    f"<LibraryOptions><PathInfos><MediaPathInfo><Path>{folders[name]}</Path></MediaPathInfo></PathInfos></LibraryOptions>")
            with mock.patch.object(s, "LIBRARY_ROOT", root / "cfg"):
                before = {lib["name"]: lib["id"] for lib in s.libraries()}
                legacy = {lib["name"]: lib["id"] for lib in s.libraries(_legacy=True)}
                folders["Anime"].rmdir()                                # one library folder temporarily missing
                after = {lib["name"]: lib["id"] for lib in s.libraries()}
                self.assertEqual(after["Series"], before["Series"])
                self.assertEqual(after["Movies"], before["Movies"])
                self.assertEqual(s.find_library(before["Series"])["path"], str(folders["Series"]))
                self.assertIsNone(s.find_library(before["Anime"]))
                folders["Anime"].mkdir()
                self.assertEqual(s.find_library(legacy["Series"])["name"], "Series")   # batch saved with an old id
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 20. dependencies
class Requirements(unittest.TestCase):
    PACKAGES = {"cv2": "opencv-python", "winrt": "winrt-runtime", "PIL": "pillow", "numpy": "numpy", "psutil": "psutil",
                "watchdog": "watchdog", "libtorrent": "libtorrent", "open_clip": "open_clip_torch", "torch": "torch"}

    def test_every_third_party_import_is_listed(self):
        import ast
        here = Path(__file__).parent
        local = {p.stem for p in here.glob("*.py")}
        listed = {re.split(r"[=<> ]", line.strip())[0].lower()
                  for line in (here / "requirements.txt").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")}
        missing = set()
        for path in here.glob("*.py"):
            if path.name.startswith("test_"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
                for name in names:
                    top = name.split(".")[0]
                    if top in sys.stdlib_module_names or top in local or top == "ccl_chromium_reader":
                        continue
                    if self.PACKAGES.get(top, top).lower() not in listed:
                        missing.add(f"{path.name}: {top}")
        self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main()
