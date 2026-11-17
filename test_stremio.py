"""Offline validation, including a tiny local media download (no real library writes)."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch, MagicMock

import egress
import stremio as s
import stremio_account
import library_processing


def only_port(port):
    """Test proxy policy: the live proxy refuses loopback (audit #8); allow just the local test server."""
    def policy(host, p):
        if p != port:
            raise egress.Denied("test proxy: only the local test server")
        return "127.0.0.1"
    return policy


class Tests(unittest.TestCase):
    def tearDown(self):
        s._items.clear()
        s._requests.clear()

    def test_account_encryption_roundtrip(self):
        raw = b'{"private":"not-for-browser"}'
        encrypted = stremio_account.protect(raw)
        self.assertNotIn(raw, encrypted)
        self.assertEqual(stremio_account.protect(encrypted, decrypt=True), raw)

    def test_pause_freezes_and_resume_continues_the_download_process(self):
        import psutil
        import sys
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=s.NO_WINDOW)
        job = {"name": "Show", "status": "running", "started": time.time() - 100}
        try:
            with tempfile.TemporaryDirectory() as d, patch.object(s, "JOB", Path(d) / "job.json"), \
                    patch.object(s, "_job_process", proc), patch.object(s, "_job_state", job):
                s._job_lock.acquire()
                try:
                    started = job["started"]
                    s.pause_download()
                    self.assertEqual(psutil.Process(proc.pid).status(), psutil.STATUS_STOPPED)
                    self.assertTrue(json.loads(s.JOB.read_text(encoding="utf-8"))["paused"])
                    time.sleep(1.2)
                    s.resume_download()
                    self.assertNotEqual(psutil.Process(proc.pid).status(), psutil.STATUS_STOPPED)
                    self.assertGreaterEqual(job["started"] - started, 1.1)        # time-left ignores the pause
                    self.assertFalse(json.loads(s.JOB.read_text(encoding="utf-8"))["paused"])
                    job["phase"] = "processing"
                    with self.assertRaises(s.UserError):                          # never during Arabic preparation
                        s.pause_download()
                finally:
                    s._job_lock.release()
        finally:
            proc.kill()
            proc.wait()

    def test_http_sources_reconnect_where_they_stopped(self):
        source = {"stream": {"url": "https://example.com/v.mkv"}}
        with patch.object(s, "resolve_media", return_value="https://example.com/v.mkv"):
            args = s.input_args(source)
        self.assertIn("-reconnect", args)
        self.assertLess(args.index("-reconnect"), args.index("-i"))

    def test_failed_download_can_be_dismissed_everywhere(self):
        # 2026-11-17: a failed job stayed on the page at 99% with no way to hide it
        with tempfile.TemporaryDirectory() as d, patch.object(s, "JOB", Path(d) / "job.json"):
            s.write_job({"name": "Show", "status": "failed", "stage": "The saved file is incomplete", "pct": 99})
            self.assertEqual(s.job_status()["status"], "failed")
            s._job_lock.acquire()
            try:
                with self.assertRaises(s.UserError):          # a running download must be cancelled instead
                    s.dismiss_download()
            finally:
                s._job_lock.release()
            self.assertIsNone(s.dismiss_download())
            self.assertIsNone(s.job_status())                  # hidden for every device / reload
            s.write_job({"name": "Next", "status": "running"})  # a new download shows again
            self.assertEqual(s.job_status()["name"], "Next")

    def test_media_paths_stay_in_library(self):
        with tempfile.TemporaryDirectory() as root:
            video = {"type": "series", "season": 2, "episode": 3,
                     "meta": {"name": "A/B: Test", "releaseInfo": "2024-"}}
            path = s.target(video, {"path": root})
            self.assertEqual(path.name, "AB Test (2024) - S02E03.mkv")
            self.assertTrue(path.is_relative_to(Path(root)))
            video["type"] = "movie"
            self.assertEqual(s.target(video, {"path": root}).name, "AB Test (2024).mkv")
            with self.assertRaises(s.UserError):
                s.clean_name("../../")

    def test_reject_private_and_non_http_sources(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/private", "http://169.254.169.254/", "https://user:pass@example.com/"):
            with self.assertRaises(s.UserError):
                s.safe_url(url)

    def test_supported_types_and_prefixes(self):
        a = {"manifest": {"resources": [{"name": "stream", "types": ["series"], "idPrefixes": ["tt"]}]}}
        self.assertTrue(s.supports(a, "stream", "series", "tt123:1:1"))
        self.assertFalse(s.supports(a, "stream", "movie", "tt123"))
        self.assertFalse(s.supports(a, "stream", "series", "kitsu:123"))

    def test_opaque_selection_kind_and_expiry(self):
        key = s.remember({"kind": "title", "id": "tt123"})
        self.assertEqual(s.item(key, "title")["id"], "tt123")
        with self.assertRaises(s.UserError):
            s.item(key, "source")
        with patch.object(s.time, "time", return_value=s.time.time() + 7201):
            with self.assertRaises(s.UserError):
                s.item(key, "title")

    def test_long_search_session_keeps_cache_bounded_without_blocking(self):
        for i in range(2000):
            s._items[str(i)] = (s.time.time(), {"kind": "title"})
        key = s.remember({"kind": "title", "name": "New result"})
        self.assertEqual(len(s._items), 2000)
        self.assertNotIn("0", s._items)
        self.assertEqual(s.item(key, "title")["name"], "New result")

    def test_reject_unknown_tracks_before_start(self):
        key = s.remember({"kind": "source", "probe": {"streams": [
            {"index": 1, "codec_type": "audio"}, {"index": 2, "codec_type": "subtitle"}]}})
        for audio, subtitle in (("99", "2"), ("1", "99"), ("none", "2")):
            with self.assertRaises(s.UserError):
                s.start_download(key, audio, subtitle, "anything")

    def test_header_injection_is_rejected(self):
        source = {"stream": {"url": "https://example.com/media", "behaviorHints": {
            "proxyHeaders": {"request": {"User-Agent": "ok\r\nInjected: bad"}}}}}
        with patch.object(s, "stream_url", return_value="https://example.com/media"):
            with self.assertRaises(s.UserError):
                s.input_args(source)

    def test_torrent_requires_specific_file(self):
        self.assertIn("/3", s.stream_url({"infoHash": "a" * 40, "fileIdx": 3}))
        with self.assertRaises(s.UserError):
            s.stream_url({"infoHash": "a" * 40})

    def test_aiostreams_resolution_formats(self):
        self.assertEqual(s.quality({"name": "🔥4K UHD"}), "2160p")
        self.assertEqual(s.quality({"name": "🚀 FHD"}), "1080p")
        self.assertEqual(s.quality({"name": "AIOStreams", "behaviorHints": {"filename": "Movie.720p.mkv"}}), "720p")

    def test_hdr_labels_and_actual_metadata(self):
        for filename, expected in (("Movie.DV.HDR.2160p.mkv", "Dolby Vision"),
                                   ("Movie.HDR10+.mkv", "HDR10+"), ("Movie.HLG.mkv", "HLG"),
                                   ("Movie.HDR10.mkv", "HDR10"), ("Movie.SDR.mkv", "SDR"),
                                   ("Movie.10bit.HEVC.mkv", "Unspecified")):
            self.assertEqual(s.dynamic_range({"behaviorHints": {"filename": filename}}), expected)
        self.assertEqual(s.verified_range({"color_transfer": "smpte2084"}), "HDR10")
        self.assertEqual(s.verified_range({"color_transfer": "arib-std-b67"}), "HLG")
        self.assertEqual(s.verified_range({"color_transfer": "bt709"}), "SDR")
        self.assertEqual(s.verified_range({"pix_fmt": "yuv420p10le"}), "Unspecified")
        self.assertEqual(s.verified_range({"side_data_list": [{"side_data_type": "DOVI configuration record"}]}), "Dolby Vision")

    def test_audio_labels_do_not_confuse_subtitles_with_audio(self):
        self.assertEqual(s.reported_audio({"description": "🗣️ 🇬🇧 / 🇯🇵\nSubtitles: Arabic"}), ["en", "ja"])
        self.assertEqual(s.reported_audio({"description": "Audio: Japanese | Subs: English"}), ["ja"])
        self.assertEqual(s.reported_audio({"audioTracks": [{"lang": "ara"}, {"language": "jpn"}]}), ["ar", "ja"])
        self.assertEqual(s.reported_audio({"description": "English subtitles", "behaviorHints": {"filename": "Movie.Eng.Sub.mkv"}}), [])
        self.assertEqual(s.reported_audio({"description": "الصوت: Arabic"}), ["ar"])

    def test_bidooo_catalog_without_search_can_be_found(self):
        addon = {"transportUrl": "https://bidooo.example/manifest.json", "manifest": {
            "name": "Bidooo Arabic Catalog", "catalogs": [{"id": "bidooo_series", "type": "series"}]}}
        with patch.object(s, "addons", return_value=[addon]), patch.object(s, "get_json", return_value={
                "metas": [{"id": "tt1", "name": "Hunter X Hunter"}, {"id": "tt2", "name": "Another show"}]}) as request:
            result = s.search("hunter", "series")
            self.assertEqual([m["name"] for m in result["results"]], ["Hunter X Hunter"])
            self.assertNotIn("search=", request.call_args.args[0])

    def test_bidooo_reports_arabic_without_claiming_verification(self):
        key = s.remember({"kind": "video", "type": "movie", "id": "tt1"})
        result = {"_provider": "Bidooo Arabic Catalog", "streams": [{"name": "Bidooo", "url": "https://example.com/movie"}]}
        with patch.object(s, "addons", return_value=[]), patch.object(s, "parallel", return_value=([result], 0)):
            data = s.streams(key)
            self.assertEqual(data["sources"][0]["audio_languages"], ["ar"])
            self.assertFalse(data["sources"][0]["audio_verified"])
            self.assertEqual(data["providers"][0]["count"], 1)

    def test_global_search_keeps_results_in_their_real_categories(self):
        addon = {"transportUrl": "https://catalog.example/manifest.json", "manifest": {
            "name": "Catalog", "catalogs": [{"id": "top", "type": mt, "extra": [{"name": "search"}]}
                                             for mt in ("movie", "series", "anime")]}}
        def response(url, **kwargs):
            mt = next(mt for mt in ("movie", "series", "anime") if "/" + mt + "/" in url)
            return {"metas": [{"id": "tt" + str(i), "name": "Match " + mt} for i in range(30)]}
        with patch.object(s, "addons", return_value=[addon]), patch.object(s, "get_json", side_effect=response):
            result = s.search("match", "all")
            self.assertEqual({m["type"] for m in result["results"]}, {"movie", "series", "anime"})
            self.assertEqual(len(result["results"]), 60)
            self.assertTrue(all(sum(m["type"] == mt for m in result["results"]) == 20 for mt in ("movie", "series", "anime")))

    def test_remaining_time_handles_start_and_completion(self):
        self.assertEqual(s.estimate_remaining(100, .25, now=125), 75)
        self.assertEqual(s.estimate_remaining(100, 1, now=125), 0)
        self.assertIsNone(s.estimate_remaining(100, 0, now=125))
        self.assertIsNone(s.estimate_remaining(100, .25, now=99))

    def test_download_job_identifies_the_episode(self):
        with tempfile.TemporaryDirectory() as folder:
            source = {"video": {"type": "series", "season": 1, "episode": 13, "meta": {"name": "Show"}},
                      "stream": {}, "probe": {"streams": []}}
            with patch.object(s, "item", return_value=source), patch.object(s, "libraries", return_value=[
                    {"id": "test", "name": "Anime", "path": folder}]), patch.object(s, "write_job"), \
                    patch.object(s.shutil, "disk_usage", return_value=types.SimpleNamespace(free=10**12)), \
                    patch.object(s, "download", side_effect=lambda *args: s._job_lock.release()):
                job = s.start_download("source", "none", "none", "test", _batch=True)
            self.assertEqual((job["name"], job["season"], job["episode"]), ("Show", 1, 13))

    def test_known_error_video_urls_are_rejected(self):
        for url in ("https://cdn.example/static/unavailable_for_legal_reasons.mp4",
                    "https://cdn.example/static/403.mp4", "https://cdn.example/download_failed.mp4",
                    "https://proxy.example/play?url=https%3A%2F%2Fcdn.example%2Fstatic%2Fno_matching_file.mp4"):
            with self.assertRaises(s.UserError):
                s.reject_error_url(url, "AIOStreams Stable")
        s.reject_error_url("https://cdn.example/movies/200.mp4", "Other provider")

    def test_error_video_redirect_is_rejected_before_ffprobe(self):
        response = MagicMock()
        response.url = "https://cdn.example/static/unavailable_for_legal_reasons.mp4"
        opener = MagicMock()
        opener.open.return_value = response
        source = {"stream": {"url": "https://resolver.example/play"}, "provider": "AIOStreams Stable"}
        with patch.object(s, "stream_url", return_value=source["stream"]["url"]), \
                patch.object(s.urllib.request, "build_opener", return_value=opener), \
                patch.object(s.subprocess, "run") as probe:
            with self.assertRaises(s.UserError):
                s.probe(source)
            probe.assert_not_called()

    def test_placeholder_mismatch_does_not_reject_legitimate_short_videos(self):
        data = {"format": {"duration": "120", "size": "1000000"}}
        source = {"stream": {"description": "⏱️ 24m:54s", "behaviorHints": {"videoSize": 1e9}}}
        with self.assertRaises(s.UserError):
            s.validate_media(source, data)
        s.validate_media({"stream": {"description": "⏱️ 2m", "behaviorHints": {"videoSize": 1e6}}}, data)
        s.validate_media({"stream": {}}, data)

    def test_failed_addon_does_not_erase_other_results(self):
        def fail(): raise RuntimeError("private-url-token")
        results, failures = s.parallel([(fail, ()), (lambda: {"metas": ["ok"]}, ())])
        self.assertEqual(results, [{"metas": ["ok"]}])
        self.assertEqual(failures, 1)

    def test_slow_addon_does_not_block_and_is_reused(self):
        release = threading.Event()
        fast = {"transportUrl": "https://fast.example/manifest.json"}
        slow = {"transportUrl": "https://slow.example/manifest.json"}

        def fetch(url, **kwargs):
            if "slow.example" in url:
                release.wait(2)
                return {"metas": ["late"]}
            return {"metas": ["ready"]}

        with patch.object(s, "get_json", side_effect=fetch) as request:
            try:
                started = time.monotonic()
                results, pending = s.parallel([(s.resource, (a, "catalog", "movie", "top"))
                                              for a in (fast, slow)], timeout=.1)
                self.assertLess(time.monotonic() - started, .8)
                self.assertEqual(results, [{"metas": ["ready"]}])
                self.assertEqual(pending, 1)
                release.set()
                self.assertEqual(s.resource(slow, "catalog", "movie", "top"), {"metas": ["late"]})
                self.assertEqual(s.resource(fast, "catalog", "movie", "top"), {"metas": ["ready"]})
                self.assertEqual(request.call_count, 2)
            finally:
                release.set()

    def test_inspection_is_cached_without_subtitle_network_wait(self):
        key = s.remember({"kind": "source", "stream": {"name": "1080p"}})
        data = {"streams": [{"index": 0, "codec_type": "video", "width": 1920, "height": 1080},
                            {"index": 1, "codec_type": "audio", "tags": {"language": "eng"}}]}
        with patch.object(s, "probe", return_value=data) as probe, \
                patch.object(s, "addons", side_effect=AssertionError("Embedded inspection must not query add-ons")):
            first = s.inspect(key)
            second = s.inspect(key)
            self.assertEqual(first["audio"][0]["language"], "eng")
            self.assertEqual(first, second)
            probe.assert_called_once()

    def test_real_selected_track_download(self):
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args): pass

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, text in (("en", "Test"), ("ar", "اختبار")):
                (root / f"{name}.srt").write_text(f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n", encoding="utf-8")
            media = root / "source.mkv"
            cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                   "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
                   "-i", str(root / "en.srt"), "-i", str(root / "ar.srt"),
                   "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s", "-map", "4:s",
                   "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "srt", "-metadata:s:a:0", "language=jpn",
                   "-metadata:s:a:1", "language=eng", "-metadata:s:s:0", "language=eng",
                   "-metadata:s:s:1", "language=ara", str(media)]
            subprocess.run(cmd, check=True, capture_output=True, creationflags=s.NO_WINDOW)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=folder))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                source = {"kind": "source", "video": {"type": "movie"},
                          "stream": {"url": f"http://127.0.0.1:{server.server_port}/source.mkv"}}
                with patch.object(s, "stream_url", return_value=source["stream"]["url"]), \
                        patch.object(egress, "_proxy", egress.Proxy(only_port(server.server_port))), \
                        patch.object(s, "JOB", root / "job.json"), patch.object(s.agent, "notify") as notify, \
                        patch.object(s.agent, "log"):
                    def prepared(path, destination, metadata, progress):
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        __import__("shutil").move(str(path), str(destination))
                        return {"status": "done", "stage": "Arabic preparation tested separately"}
                    data = s.probe(source)
                    source["probe"] = data
                    dest = root / "library" / "selected.mkv"
                    job = {"name": "Test", "status": "running", "library": "Test"}
                    s._job_lock.acquire()
                    with patch.object(library_processing, "submit", side_effect=prepared) as pipeline:
                        s.download(source, "2", "4", dest, job)
                        pipeline.assert_called_once()
                    self.assertEqual(job["status"], "done", job)
                    notify.assert_not_called()  # The mandatory pipeline owns the ready notification.
                    self.assertTrue(dest.exists())
                    self.assertFalse(list(dest.parent.glob("*.part")))
                    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(dest)],
                                            check=True, capture_output=True, creationflags=s.NO_WINDOW)
                    streams = json.loads(result.stdout)["streams"]
                    self.assertEqual([t["codec_type"] for t in streams], ["video", "audio", "subtitle"])
                    self.assertEqual(streams[1]["tags"]["language"], "eng")
                    self.assertEqual(streams[2]["tags"]["language"], "ara")
                    mandatory_dest = root / "library" / "mandatory.mkv"
                    s._job_lock.acquire()
                    with patch.object(library_processing, "submit", side_effect=prepared):
                        s.download(source, "2", "none", mandatory_dest, {"name": "Test", "status": "running", "library": "Test"})
                    mandatory_streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(mandatory_dest)],
                                                                 capture_output=True, check=True).stdout)["streams"]
                    self.assertTrue(any(t.get("codec_type") == "subtitle" and t.get("tags", {}).get("language") == "ara" for t in mandatory_streams))
                    held_dest = root / "library" / "held.mkv"
                    held_job = {"name": "Test", "status": "running", "library": "Test"}
                    def held(path, destination, metadata, progress):
                        __import__("shutil").move(str(path), str(root / "held.raw"))
                        return {"status": "waiting", "stage": "No official Arabic subtitle yet"}
                    s._job_lock.acquire()
                    with patch.object(library_processing, "submit", side_effect=held):
                        s.download(source, "2", "none", held_dest, held_job)
                    self.assertEqual(held_job["status"], "waiting")
                    self.assertFalse(held_dest.exists())
                    self.assertTrue((root / "held.raw").exists())
                    notify.assert_not_called()
                    source["stream"]["description"] = "⏱️ 24m:54s"
                    source["stream"]["behaviorHints"] = {"videoSize": 1e9}
                    rejected = root / "library" / "rejected.mkv"
                    failed_job = {"name": "Rejected", "status": "running", "library": "Test"}
                    notify.reset_mock()
                    s._job_lock.acquire()
                    s.download(source, "2", "4", rejected, failed_job)
                    self.assertEqual(failed_job["status"], "failed")
                    self.assertFalse(rejected.exists())
                    self.assertFalse(list(rejected.parent.glob("*.part")))
                    notify.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
