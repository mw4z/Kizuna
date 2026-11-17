"""Regression tests for audit findings 1-15 (2026-11-15). Everything runs in temp folders: no real
episode, library, state or log file is touched. Needs ffmpeg/ffprobe and MKVToolNix (already required).
Run: python -m unittest test_audit
"""
import http.server
import importlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agent  # noqa: E402
import cleanup  # noqa: E402
import egress  # noqa: E402
import mux  # noqa: E402
import library_processing


class Sandbox(unittest.TestCase):
    """Points every agent path at a temp folder for the duration of a test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fya-test-")
        self.inbox = os.path.join(self.tmp, "inbox")
        self.lib = os.path.join(self.tmp, "library")
        os.makedirs(self.inbox)
        os.makedirs(self.lib)
        self.patches = [mock.patch.object(agent, n, v) for n, v in (
            ("FOLDER", self.inbox), ("LIBRARY", self.lib), ("STATE", os.path.join(self.tmp, "state.json")),
            ("LOG", os.path.join(self.tmp, "log.txt")), ("WORK", os.path.join(self.tmp, "work")),
            ("PFILE", os.path.join(self.tmp, "progress.json")), ("HERE", self.tmp))]
        self.patches.append(mock.patch.object(library_processing, "FILE", __import__("pathlib").Path(self.tmp, "library-processing.json")))
        self.anime = os.path.join(self.tmp, "anime")
        os.makedirs(self.anime)
        self.patches.append(mock.patch.object(cleanup, "ANIME", self.anime))      # never the real Anime folder
        for p in self.patches:
            p.start()
        os.makedirs(agent.WORK)
        self.toasts = []
        self.notify = mock.patch.object(agent, "notify", lambda title, text: self.toasts.append(title))
        self.notify.start()

    def tearDown(self):
        self.notify.stop()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def episode(self, name, data=b"x" * 1000, folder=None):
        p = os.path.join(folder or self.inbox, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(data)
        return p

    def progress(self):
        return json.load(open(agent.PFILE, encoding="utf-8"))


# ---------------------------------------------------------------- 1. one file per agent run
class AgentTargetsOneFile(Sandbox):
    def run_main(self, argv):
        seen = []
        with mock.patch.object(sys, "argv", argv):
            a = importlib.reload(agent)
        try:
            for n, v in (("STATE", os.path.join(self.tmp, "state.json")), ("LOG", os.path.join(self.tmp, "log.txt")),
                         ("WORK", os.path.join(self.tmp, "work")), ("PFILE", os.path.join(self.tmp, "progress.json")),
                         ("HERE", self.tmp), ("LIBRARY", self.lib)):
                setattr(a, n, v)
            a.notify = lambda *x: None
            a.tracks = lambda v: []
            a.process = lambda video, state: seen.append(os.path.basename(video))
            a.main()
            return a.FOLDER, seen              # read before the reload below resets the module
        finally:
            with mock.patch.object(sys, "argv", ["agent.py"]):
                importlib.reload(agent)

    def test_only_the_settled_file_is_processed(self):
        done = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        self.episode("[DKB] Ranma - S03E08 [1080p].mkv")            # still syncing: must not be touched
        folder, seen = self.run_main(["agent.py", done, "--now"])
        self.assertEqual(seen, ["[DKB] Ao no Hako - S02E06 [1080p].mkv"])
        self.assertEqual(os.path.normcase(folder), os.path.normcase(self.inbox))

    def test_other_arrivals_do_not_add_strikes(self):
        bad = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        other = self.episode("[DKB] Ranma - S03E08 [1080p].mkv")
        with mock.patch.object(sys, "argv", ["agent.py", other, "--now"]):
            a = importlib.reload(agent)
        try:
            a.STATE, a.LOG, a.WORK, a.PFILE, a.HERE = (os.path.join(self.tmp, f) for f in
                                                      ("state.json", "log.txt", "work", "progress.json", ""))
            a.HERE = self.tmp
            a.notify = lambda *x: None
            a.tracks = lambda v: []
            a.process = mock.Mock(side_effect=RuntimeError("disk full"))
            for _ in range(3):                   # three unrelated runs for OTHER episodes
                a.main()
            st = json.load(open(a.STATE, encoding="utf-8"))
            self.assertNotIn(os.path.basename(bad), st["errors"])
            self.assertNotIn(os.path.basename(bad), st["failed"])
        finally:
            with mock.patch.object(sys, "argv", ["agent.py"]):
                importlib.reload(agent)


# ---------------------------------------------------------------- 3, 4, 5, 10. failure recovery in the agent
ARABIC = [{"id": 3, "type": "subtitles", "properties": {"track_name": "العربية"}}]


class AgentFailures(Sandbox):
    def fake_pipeline(self, embedded_matches=True):
        lines = "".join(f"Dialogue: 0,0:00:{i:02d}.00,0:00:{i:02d}.50,Default,,0,0,0,,line {i}\n" for i in range(50))
        ass_text = "[Events]\n" + lines

        def convert(srt, ass, title, off=0):
            open(ass, "w", encoding="utf-8").write(ass_text)

        def run(*a, **k):
            if a and str(a[0]).endswith("mkvextract.exe"):
                out = a[-1].split(":", 1)[1]
                open(out, "w", encoding="utf-8").write(ass_text if embedded_matches else "[Events]\n")
            return mock.Mock(returncode=0)
        return [mock.patch.object(agent, "arabic_track", lambda v: 2),
                mock.patch.object(agent, "extract_srt", lambda v, t, out: open(out, "w").write("1") or True),
                mock.patch.object(agent.srt2ass, "convert", convert),
                mock.patch.object(agent, "timing", lambda v, a: (0.0, True)),
                mock.patch.object(agent, "qc", lambda a: []),
                mock.patch.object(agent.mux, "mux", lambda v, a, progress=None, check=None: "ok"),
                mock.patch.object(agent, "tracks", lambda v: ARABIC),
                mock.patch.object(agent, "has_arabic_audio", lambda t: False),
                mock.patch.object(agent, "run", run)]

    def process(self, video, state, **kw):
        ps = self.fake_pipeline(**kw)
        for p in ps:
            p.start()
        try:
            agent.process(video, state)
        finally:
            for p in ps:
                p.stop()

    def test_ready_alert_only_when_copied_to_jellyfin(self):
        v = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        state = agent.load_state()
        self.process(v, state)
        self.assertEqual(self.toasts, ["Anime FYA: ready"])
        self.assertTrue(os.path.exists(agent.library_target(os.path.basename(v))))
        self.assertEqual(state["copy_failed"], {})

    def test_failed_copy_is_visible_retried_and_alerts_only_on_recovery(self):
        v = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        state = agent.load_state()
        with mock.patch.object(agent.shutil, "copy2", side_effect=OSError("disk full")):
            self.process(v, state)
        self.assertNotIn("Anime FYA: ready", self.toasts)                    # no false success
        self.assertIn(os.path.basename(v), state["copy_failed"])
        self.assertEqual(self.progress()["status"], "fail")
        self.assertFalse(any(f.endswith(".part") for _, _, fs in os.walk(self.lib) for f in fs))
        with mock.patch.object(agent, "has_arabic_audio", lambda t: False), self.fake_pipeline()[-1]:
            agent.retry_copy(v, ARABIC, state)                                  # disk freed: retry works
        self.assertEqual(self.toasts.count("Anime FYA: ready"), 1)
        self.assertEqual(state["copy_failed"], {})
        self.assertTrue(os.path.exists(agent.library_target(os.path.basename(v))))

    def test_failed_verification_is_recorded(self):
        v = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        state = agent.load_state()
        self.process(v, state, embedded_matches=False)
        self.assertEqual(state["failed"].get(os.path.basename(v)), os.path.getsize(v))
        self.assertNotIn("Anime FYA: ready", self.toasts)

    def test_waiting_for_arabic_does_not_look_busy(self):
        v = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        state = agent.load_state()
        state["nyaa_tries"][os.path.basename(v)] = time.time()              # searched 1 minute ago
        with mock.patch.object(agent, "tracks", lambda x: []), \
                mock.patch.object(agent, "has_arabic_audio", lambda t: False), \
                mock.patch.object(agent, "arabic_track", lambda x: None), \
                mock.patch.object(agent, "nyaa_arabic") as nyaa:
            agent.process(v, state)
        nyaa.assert_not_called()
        self.assertNotEqual(self.progress()["status"], "run")

    def test_strikes_reset_after_failure_and_gone_files_are_forgotten(self):
        v = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        with mock.patch.object(agent, "TARGET", v), mock.patch.object(agent, "NOW", True), \
                mock.patch.object(agent, "tracks", lambda x: []), \
                mock.patch.object(agent, "process", side_effect=RuntimeError("locked")):
            for _ in range(3):
                agent.main()
        st = json.load(open(agent.STATE, encoding="utf-8"))
        fn = os.path.basename(v)
        self.assertIn(fn, st["failed"])
        self.assertNotIn(fn, st["errors"])                                  # replaced file gets 3 fresh tries
        os.remove(v)
        gone = self.episode("[DKB] Ranma - S03E08 [1080p].mkv")
        with mock.patch.object(agent, "TARGET", gone), mock.patch.object(agent, "NOW", True), \
                mock.patch.object(agent, "tracks", lambda x: []), mock.patch.object(agent, "process"):
            agent.main()
        st = json.load(open(agent.STATE, encoding="utf-8"))
        self.assertNotIn(fn, st["failed"])                                  # deleted file no longer listed


# ---------------------------------------------------------------- 2. cleanup order
class CleanupOrder(Sandbox):
    def test_locked_inbox_copy_keeps_the_library_copy_and_schedule(self):
        lib = self.episode("Blue Box (2024)/Season 02/ep.mkv", folder=self.lib)
        inbox = self.episode("ep.mkv")
        recycled, locked = [], {os.path.normcase(inbox)}

        def recycle(p):
            if os.path.normcase(p) in locked:
                return False
            recycled.append(p)
            os.remove(p)
            return True
        with mock.patch.object(cleanup, "due", lambda now=None: [(lib, 0, 0)] if os.path.exists(lib) else []), \
                mock.patch.object(cleanup, "recycle", recycle):
            cleanup.run_once()
            self.assertTrue(os.path.exists(lib), "library copy must stay while the inbox copy is locked")
            self.assertEqual(recycled, [])
            locked.clear()                                                  # next hour: lock released
            cleanup.run_once()
        self.assertFalse(os.path.exists(inbox))
        self.assertFalse(os.path.exists(lib))
        self.assertEqual([os.path.normcase(p) for p in recycled], [os.path.normcase(inbox), os.path.normcase(lib)])

    def test_anime_library_episode_recycled_but_inbox_namesake_kept(self):
        # 2026-11-17: Anime library joins the cleanup; its files are named "10.mkv", so an inbox file with the
        # same name is unrelated and must stay
        ep = self.episode("Les Mis/10.mkv", folder=self.anime)
        namesake = self.episode("10.mkv")                                   # in the OneDrive inbox
        movie = self.episode("Movie (2020)/Movie (2020).mkv", folder=os.path.join(self.tmp, "Movies"))
        recycled = []

        def recycle(p):
            recycled.append(p)
            os.remove(p)
            return True
        with mock.patch.object(cleanup, "due", lambda now=None: [(ep, 0, 0), (movie, 0, 0)]), \
                mock.patch.object(cleanup, "recycle", recycle):
            cleanup.run_once()
        self.assertEqual([os.path.normcase(p) for p in recycled], [os.path.normcase(ep)])
        self.assertTrue(os.path.exists(namesake))
        self.assertTrue(os.path.exists(movie))                              # Movies are never cleaned

    def test_dry_run_deletes_nothing(self):
        lib = self.episode("Show/Season 01/ep.mkv", folder=self.lib)
        with mock.patch.object(cleanup, "due", lambda now=None: [(lib, 0, 0)]), \
                mock.patch.object(cleanup, "recycle") as rec, mock.patch("sys.stdout", io.StringIO()):
            cleanup.run_once(dry=True)
        rec.assert_not_called()
        self.assertTrue(os.path.exists(lib))


# ---------------------------------------------------------------- 7. mux with no subtitle tracks (real tools)
class MuxNoSubtitles(Sandbox):
    def test_video_without_subtitles_gets_arabic_last_and_default(self):
        v = os.path.join(self.tmp, "nosubs.mkv")
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=2",
                            "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-c:a", "aac", "-shortest", v],
                           capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        ass = os.path.join(self.tmp, "ar.ass")
        open(ass, "w", encoding="utf-8-sig").write(
            "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,"
            "SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
            "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\nStyle: Default,Cairo,40,"
            "&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,10,1\n\n[Events]\n"
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
            "Dialogue: 0,0:00:00.20,0:00:01.50,Default,,0,0,0,,مرحبا\n")
        res = mux.mux(v, ass)
        self.assertTrue(res.startswith("ok"), res)
        tr = mux.info(v)["tracks"]
        self.assertEqual(tr[-1]["properties"].get("track_name"), "العربية")
        self.assertTrue(tr[-1]["properties"].get("default_track"))
        self.assertFalse(os.path.exists(v[:-4] + ".tmp.mkv"))


# ---------------------------------------------------------------- 11. timing decision
class Timing(Sandbox):
    def fit(self, windows, g):
        return mock.patch.object(agent.audio_fit, "fit", lambda v, a: {"windows": [{"offset": w, "strength": 10} for w in windows],
                                                                       "global": g, "strength": 10})

    def real(self, windows, g, strength):
        return mock.patch.object(agent.audio_fit, "fit", lambda v, a: {
            "windows": [{"offset": o, "strength": s} for o, s in windows], "global": g, "strength": strength})

    def test_real_episodes_with_weak_windows_still_pass(self):
        # measured on the real files 2026-11-15: correct timing, but Black Clover's windows score only 2-3
        black_clover = [(-0.15, 2.9), (0.1, 2.1), (0.1, 3.4), (0.1, 2.6), (-0.2, 2.7), (-82.0, 4.0)]
        ranma = [(-0.2, 4.0), (0.05, 3.0), (0.1, 4.8), (81.9, 3.6), (0.1, 2.5), (0.05, 2.8)]
        tokyo = [(0.0, 4.0), (-0.05, 4.2), (-0.05, 6.9), (0.05, 4.1), (0.05, 11.2), (0.0, 4.3)]
        for windows, g, strength in ((black_clover, 0.05, 11.1), (ranma, 0.05, 6.2), (tokyo, 0.0, 16.9)):
            with self.real(windows, g, strength):
                self.assertEqual(agent.timing("v", "a"), (0.0, True), windows)

    def test_weak_windows_can_never_shift_subtitles(self):
        weak = [(2.4, 2.0), (2.35, 2.5), (2.3, 2.9), (2.4, 2.2)]
        with self.real(weak, 2.35, 9.0):                                    # strong overall, weak windows
            self.assertEqual(agent.timing("v", "a"), (0.0, False))
        strong = [(2.4, 4.0), (2.35, 5.0), (2.3, 3.5), (2.4, 2.0)]
        with self.real(strong, 2.35, 9.0):
            off, ok = agent.timing("v", "a")
        self.assertTrue(ok)
        self.assertAlmostEqual(off, 2.375, places=2)

    def test_weak_overall_fit_blocks_even_at_zero(self):
        with self.real([(0.0, 0.0)] * 6, 0.0, 0.0):                        # silence: degenerate all-zero fit
            self.assertEqual(agent.timing("v", "a"), (0.0, False))

    def test_consensus_ignores_song_outlier(self):
        with self.fit([-0.2, 0.05, 0.1, 81.9, 0.1, 0.05], 0.05):
            self.assertEqual(agent.timing("v", "a"), (0.0, True))           # Ranma S03E07 real numbers

    def test_consistent_offset_is_applied(self):
        with self.fit([2.4, 2.35, 2.3, 2.4], 2.35):
            off, ok = agent.timing("v", "a")
        self.assertTrue(ok)
        self.assertAlmostEqual(off, 2.375, places=2)

    def test_inconsistent_timing_blocks_the_mux(self):
        with self.fit([0.0, 5.0, 11.0, 30.0], 0.0):
            self.assertEqual(agent.timing("v", "a"), (0.0, False))
        with self.fit([1.0, 1.1, 1.05], 4.0):                               # windows agree, global disagrees
            self.assertEqual(agent.timing("v", "a"), (0.0, False))


# ---------------------------------------------------------------- 6, 14 + copy retry: watcher
def import_watcher():
    out, err = sys.stdout, sys.stderr
    try:
        import watcher
        return watcher
    finally:
        sys.stdout, sys.stderr = out, err                      # watcher redirects output to its log on import


class Watcher(Sandbox):
    def test_timeout_kills_agent_and_its_children(self):
        w = import_watcher()
        fake = os.path.join(self.tmp, "agent.py")
        pidfile = os.path.join(self.tmp, "child.pid")
        open(fake, "w").write(
            "import subprocess,sys,time\n"
            f"c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
            f"open({pidfile!r},'w').write(str(c.pid))\ntime.sleep(120)\n")
        import psutil
        with mock.patch.object(w, "HERE", self.tmp):
            t0 = time.time()
            code = w.run_agent(self.episode("x.mkv"), timeout=4)
        self.assertIsNone(code)
        self.assertLess(time.time() - t0, 60)
        child = int(open(pidfile).read())
        self.assertFalse(psutil.pid_exists(child) and psutil.Process(child).status() != psutil.STATUS_ZOMBIE,
                         "orphaned child still running")

    def test_timeout_requeues_the_episode(self):
        w = import_watcher()
        v = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        queued = []
        with mock.patch.object(w, "needs_work", lambda p: True), mock.patch.object(w, "settled", lambda p, l=None: True), \
                mock.patch.object(w, "run_agent", lambda p: None), \
                mock.patch.object(w, "queue", lambda p, d=0: queued.append((p, d))):
            w.pending.clear()
            w.pending[v] = 0
            w.step()
        self.assertEqual(queued, [(v, 1200)])

    def test_agent_runs_on_the_exact_file(self):
        w = import_watcher()
        v = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        with mock.patch.object(w.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            w.run_agent(v)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[2:], [v, "--now"])

    def test_own_tmp_swap_is_not_queued(self):
        w = import_watcher()
        queued = []
        ev = lambda src, dst: mock.Mock(is_directory=False, src_path=src, dest_path=dst)  # noqa: E731
        with mock.patch.object(w, "queue", lambda p, d=0: queued.append(p)):
            w.Handler().on_moved(ev(r"C:\in\ep.tmp.mkv", r"C:\in\ep.mkv"))
            w.Handler().on_moved(ev(r"C:\in\ep.mkv.part", r"C:\in\ep.mkv"))   # a downloader's rename still counts
        self.assertEqual(queued, [r"C:\in\ep.mkv"])

    def test_failed_library_copy_keeps_the_episode_queued(self):
        w = import_watcher()
        v = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        json.dump({"copy_failed": {os.path.basename(v): 1000}}, open(agent.STATE, "w", encoding="utf-8"))
        with mock.patch.object(agent, "tracks", lambda p: ARABIC), mock.patch.object(agent, "has_arabic_audio", lambda t: False):
            self.assertTrue(w.needs_work(v))


# ---------------------------------------------------------------- 9, 15. status page
class StatusServer(Sandbox):
    def setUp(self):
        super().setUp()
        import status
        self.status = status
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), status.make_handler())
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def get(self, path, host=None, headers=None, method="GET"):
        c = __import__("http.client").client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        c.request(method, path, body=b"{}" if method == "POST" else None, headers=h)
        r = c.getresponse()
        r.read()
        return r.status

    def test_every_page_rejects_foreign_host_names(self):
        for path in ("/", "/api/status", "/stremio", "/app.png"):
            self.assertEqual(self.get(path, host=f"evil.example:{self.port}"), 403, path)
        self.assertEqual(self.get("/api/stremio/sync", host="evil.example", method="POST"), 403)
        self.assertEqual(self.get("/", host=f"localhost:{self.port}"), 200)
        self.assertEqual(self.get("/app.png"), 200)

    def test_stremio_api_needs_the_token(self):
        self.assertEqual(self.get("/api/stremio/connection", headers={"X-MyAnimeWatcher": "wrong"}), 403)

    def test_waiting_list_matches_agent_rules(self):
        conan = self.episode("[SubsPlease] Detective Conan - 1221 (1080p).mkv")
        failed = self.episode("[DKB] Ranma - S03E08 [1080p].mkv")
        waiting = self.episode("[DKB] Ao no Hako - S02E06 [1080p].mkv")
        json.dump({"failed": {os.path.basename(failed): 1000}, "errors": {os.path.basename(failed): 3}},
                  open(agent.STATE, "w", encoding="utf-8"))
        s = self.status
        with mock.patch.object(s, "_has_arabic", lambda p: False), mock.patch.object(s, "_shows", lambda l=(): []), \
                mock.patch.object(s, "_deletions", lambda: []), mock.patch.object(s, "_jellyfin_up", lambda: True):
            d = s.collect()
        self.assertEqual(d["waiting"], [s.nice(os.path.basename(waiting))])
        self.assertEqual(len([p for p in d["problems"] if "Ranma" in p]), 1)
        self.assertNotIn(s.nice(os.path.basename(conan)), d["waiting"])


# ---------------------------------------------------------------- 8. egress proxy (redirects, HLS, https, DNS)
class _Origin(http.server.ThreadingHTTPServer):
    def __init__(self, routes):
        hits = self.hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.path)
                code, headers, body = routes(self.path) if routes else (200, {}, b"")
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_HEAD = do_GET
        super().__init__(("127.0.0.1", 0), H)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


class EgressProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fya-egress-")
        clip = os.path.join(cls.tmp, "clip.ts")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x36:rate=5:duration=1",
                        "-c:v", "libx264", "-f", "mpegts", clip], check=True)
        cls.clip = open(clip, "rb").read()
        cls.lan = _Origin(lambda p: (200, {"Content-Type": "video/mp2t"}, cls.clip))   # stands in for router/Jellyfin
        lan_url = f"http://127.0.0.1:{cls.lan.port}"

        def routes(p):
            if p == "/redirect":
                return 302, {"Location": lan_url + "/secret"}, b""
            if p == "/list.m3u8":
                return 200, {"Content-Type": "application/vnd.apple.mpegurl"}, (
                    "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n" + lan_url + "/segment.ts\n#EXT-X-ENDLIST\n").encode()
            if p == "/ok.ts":
                return 200, {"Content-Type": "video/mp2t"}, cls.clip
            return 404, {}, b""
        cls.public = _Origin(routes)                                    # stands in for a public CDN
        allowed = cls.public.port

        def policy(host, port):                                         # same shape as default_policy
            if port != allowed:
                raise egress.Denied(f"{host}:{port} is not a public address")
            return "127.0.0.1"
        cls.proxy = egress.Proxy(policy)

    @classmethod
    def tearDownClass(cls):
        for s in (cls.proxy, cls.lan, cls.public):
            s.shutdown()
            s.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.lan.hits.clear()
        self.proxy.denied.clear()

    def ffprobe(self, url):
        return subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", egress.PROTOCOLS, "-rw_timeout",
                               "10000000", "-i", url, "-show_format", "-of", "json"],
                              capture_output=True, timeout=60, env=egress.ffmpeg_env(self.proxy))

    def test_allowed_source_still_works_through_the_proxy(self):
        r = self.ffprobe(f"http://127.0.0.1:{self.public.port}/ok.ts")
        self.assertEqual(r.returncode, 0, r.stderr[-300:])

    def test_redirect_to_lan_is_blocked(self):
        self.ffprobe(f"http://127.0.0.1:{self.public.port}/redirect")
        self.assertEqual(self.lan.hits, [])
        self.assertTrue(any(str(self.lan.port) in d for d in self.proxy.denied))

    def test_hls_segment_on_lan_is_blocked(self):
        self.ffprobe(f"http://127.0.0.1:{self.public.port}/list.m3u8")
        self.assertEqual(self.lan.hits, [])
        self.assertTrue(any(str(self.lan.port) in d for d in self.proxy.denied))

    def test_https_is_tunnelled_through_the_proxy(self):
        self.ffprobe(f"https://127.0.0.1:{self.lan.port}/x")              # must ask the proxy (CONNECT), not go direct
        self.assertEqual(self.lan.hits, [])
        self.assertTrue(any(str(self.lan.port) in d for d in self.proxy.denied))

    def test_urllib_allowed_request_keeps_its_own_host_header(self):
        seen = []
        orig = self.public.RequestHandlerClass.do_GET

        def spy(handler):
            seen.append(handler.headers.get("Host"))
            return orig(handler)
        with mock.patch.object(self.public.RequestHandlerClass, "do_GET", spy):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), egress.AlwaysProxy(self.proxy))
            with opener.open(f"http://127.0.0.1:{self.public.port}/ok.ts", timeout=10) as r:
                self.assertEqual(r.status, 200)
        self.assertEqual(seen, [f"127.0.0.1:{self.public.port}"])     # not the proxy's address

    def test_urllib_requests_and_redirects_are_checked(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), egress.AlwaysProxy(self.proxy))
        with self.assertRaises(urllib.error.HTTPError):
            opener.open(f"http://127.0.0.1:{self.lan.port}/secret", timeout=10)
        with self.assertRaises(urllib.error.HTTPError):
            opener.open(f"http://127.0.0.1:{self.public.port}/redirect", timeout=10)
        self.assertEqual(self.lan.hits, [])

    def test_default_policy_pins_the_checked_address(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        mixed = public + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.100.1", 443))]
        with mock.patch.object(egress.socket, "getaddrinfo", lambda *a, **k: public):
            self.assertEqual(egress.default_policy("cdn.example", 443), "93.184.216.34")
        for answer in (mixed, [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8096))]):
            with mock.patch.object(egress.socket, "getaddrinfo", lambda *a, **k: answer):
                with self.assertRaises(egress.Denied):
                    egress.default_policy("rebind.example", 443)
        self.assertEqual(egress.default_policy("127.0.0.1", 11470), "127.0.0.1")   # Stremio torrent server
        with self.assertRaises(egress.Denied):
            egress.default_policy("127.0.0.1", 8096)                             # Jellyfin


class StremioUsesTheProxy(unittest.TestCase):
    def test_ffprobe_and_ffmpeg_get_the_proxy_and_protocols(self):
        import stremio
        src = {"stream": {"url": "https://cdn.example/a.mkv"}, "provider": "", "resolved_at": time.monotonic(),
               "resolved_url": "https://cdn.example/a.mkv"}
        with mock.patch.object(stremio.socket, "getaddrinfo",
                               lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]), \
                mock.patch.object(stremio.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1)
            with self.assertRaises(stremio.UserError):
                stremio.probe(src)
        cmd, env = run.call_args[0][0], run.call_args[1]["env"]
        self.assertIn(egress.PROTOCOLS, cmd)
        self.assertEqual(env["http_proxy"], egress.proxy().url)
        self.assertNotIn("no_proxy", {k.lower() for k in env})

    def test_media_may_not_point_at_jellyfin(self):
        import stremio
        with self.assertRaises(stremio.UserError):
            stremio.stream_url({"url": "http://127.0.0.1:8096/Items/x/Download"})
        self.assertTrue(stremio.stream_url({"url": "http://127.0.0.1:11470/" + "ab" * 20 + "/0"}))
        for other in ("/proxy/d=http%3A%2F%2F192.168.0.1/", "/hlsv2/x/master.m3u8?mediaURL=http://192.168.0.1/",
                      "/" + "ab" * 20 + "/0?x=1", "/abc/0"):   # Stremio's URL-forwarding endpoints reach the LAN
            with self.assertRaises(stremio.UserError):
                stremio.stream_url({"url": "http://127.0.0.1:11470" + other})

    def test_posters_must_be_https_dns_names(self):    # 13
        import stremio
        ok = "https://image.tmdb.org/t/p/w500/a.jpg"
        self.assertEqual(stremio.safe_poster(ok), ok)
        for bad in ("http://image.tmdb.org/a.jpg", "https://192.168.0.1/reboot", "https://[::1]/x",
                    "https://router/x", "https://nas.local/x", "https://u:p@cdn.example/x", "javascript:alert(1)", None):
            self.assertEqual(stremio.safe_poster(bad), "", bad)


class StyledArabicIsNeverRebuilt(Sandbox):
    """2026-11-15: library processing flattened Bleach 196-201's styled fansub track into plain style."""

    def make(self, name, sub_ext, track_name):
        import hashlib
        video = os.path.join(self.tmp, "v.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=2",
                        "-c:v", "libx264", video], check=True, capture_output=True)
        sub = os.path.join(self.tmp, "ar." + sub_ext)
        if sub_ext == "ass":
            open(sub, "w", encoding="utf-8-sig").write(
                "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,"
                "SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,"
                "Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\nStyle: Notes,Cairo,"
                "32,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,0,8,10,10,10,1\n\n"
                "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
                "Dialogue: 0,0:00:00.20,0:00:01.50,Notes,,0,0,0,,ملاحظة\n")
        else:
            open(sub, "w", encoding="utf-8").write("1\n00:00:00,200 --> 00:00:01,500\nمرحبا\n")
        out = os.path.join(self.lib, name)
        subprocess.run([mux.MKV + r"\mkvmerge.exe", "-q", "-o", out, video, "--language", "0:ara",
                        "--track-name", f"0:{track_name}", sub], check=True, capture_output=True)
        return out, hashlib.sha256(open(out, "rb").read()).hexdigest()

    def setUp(self):
        super().setUp()
        import library_processing as lp
        self.lp = lp
        self.lpp = [mock.patch.object(lp, "FILE", __import__("pathlib").Path(self.tmp) / "lp.json"),
                    mock.patch.object(lp, "ROOT", __import__("pathlib").Path(self.tmp) / "lp")]
        for p in self.lpp:
            p.start()

    def tearDown(self):
        for p in self.lpp:
            p.stop()
        super().tearDown()

    def digest(self, path):
        import hashlib
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    def test_styled_fansub_track_is_left_untouched_on_pickup(self):
        path, before = self.make("Bleach - S10E07.mkv", "ass", "العربية [Kurai Mangetsu]")
        from pathlib import Path
        with mock.patch.object(self.lp, "process_record", side_effect=AssertionError("must not process")), \
                mock.patch.object(self.lp.shutil, "copy2", side_effect=AssertionError("must not copy")):
            record = self.lp.submit(Path(path), Path(path), manual=True)
        self.assertEqual(record["status"], "done")
        self.assertEqual(self.digest(path), before)
        self.assertEqual(record["completed_signature"], self.lp.signature(path))   # next poll skips it cheaply

    def test_already_queued_styled_file_is_released_unchanged(self):
        path, before = self.make("Bleach - S10E14.mkv", "ass", "العربية [Kurai Mangetsu]")
        key = self.lp.key_for(path)
        record = {"source": path, "destination": path, "manual": True, "metadata": {}, "status": "waiting",
                  "stage": "Timing inconsistent. Not muxed, needs a look", "input_signature": self.lp.signature(path),
                  "backup": "original.mkv", "retry_at": 0}
        (self.lp.ROOT / key).mkdir(parents=True)
        real_popen = subprocess.Popen

        def popen(cmd, *a, **k):                       # mkvmerge (track check) is fine; the worker is not
            if any("processing_worker" in str(x) for x in cmd):
                raise AssertionError("must not start a worker")
            return real_popen(cmd, *a, **k)
        with mock.patch.object(self.lp.subprocess, "Popen", popen):
            out = self.lp.process_record(key, record)
        self.assertEqual(out["status"], "done")
        self.assertEqual(self.digest(path), before)

    def test_plain_srt_arabic_is_still_processed(self):
        path, _ = self.make("Show - S01E01.mkv", "srt", "العربية")
        from pathlib import Path
        self.assertFalse(self.lp.styled_arabic(path))
        with mock.patch.object(self.lp, "process_record", return_value={"status": "running"}) as proc:
            self.lp.submit(Path(path), Path(path), manual=True)
        proc.assert_called_once()


class TorrentDoesNotOpenRouterPorts(unittest.TestCase):    # 12
    def test_upnp_and_natpmp_are_off(self):
        import ast
        tree = ast.parse(open(os.path.join(HERE, "torrent.py"), encoding="utf-8").read())
        settings = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                        settings[k.value] = v.value
        self.assertIs(settings.get("enable_upnp"), False)
        self.assertIs(settings.get("enable_natpmp"), False)


if __name__ == "__main__":
    unittest.main()
