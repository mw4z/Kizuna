"""Settings page and the redesigned pages: validation, safe defaults, and that every preference really changes behaviour."""
import json
from pathlib import Path
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

import agent
import cleanup
import settings
import shows
import stremio as s
from test_audit import Sandbox


class Prefs(Sandbox):
    def setUp(self):
        super().setUp()
        self.p = mock.patch.object(settings, "FILE", Path(self.tmp, "settings.json"))
        self.p.start()
        settings._cache["stamp"] = "reset"

    def tearDown(self):
        self.p.stop()
        settings._cache["stamp"] = "reset"
        super().tearDown()

    def test_missing_or_damaged_file_means_todays_defaults(self):
        self.assertEqual(settings.get(), settings.DEFAULTS)
        settings.FILE.write_text("{broken", encoding="utf-8")
        self.assertEqual(settings.get(), settings.DEFAULTS)
        settings.FILE.write_text(json.dumps({"check_minutes": 9999, "weekly_audio": ["xx"], "clock": "13h"}), encoding="utf-8")
        self.assertEqual(settings.get(), settings.DEFAULTS)                       # bad values fall back, never crash

    def test_update_validates_and_keeps_the_old_file_on_error(self):
        with mock.patch.object(s, "libraries", return_value=[{"name": "Anime"}, {"name": "Movies"}]):
            saved = settings.update({"weekly_audio": "ar,ja", "weekly_resolutions": "1080p", "check_minutes": "30",
                                     "keep_hours": "72", "notify_ready": "false", "clock": "24h", "library_movie": "Movies"})
        self.assertEqual(saved["settings"]["weekly_audio"], ["ar", "ja"])
        self.assertEqual((settings.get("check_minutes"), settings.get("keep_hours"), settings.get("notify_ready")), (30, 72, False))
        before = settings.FILE.read_text(encoding="utf-8")
        for bad in ({"check_minutes": "1"}, {"keep_hours": "0"}, {"weekly_audio": ""}, {"weekly_resolutions": "999p"},
                    {"clock": "never"}, {"library_movie": "../x"}):
            with self.assertRaises(settings.SettingsError):
                settings.update(bad)
        self.assertEqual(settings.FILE.read_text(encoding="utf-8"), before)

    def test_weekly_source_choices_follow_languages_and_resolutions(self):
        settings.update({"weekly_audio": "ja,en", "weekly_resolutions": "1080p,720p", "weekly_subtitle": "en"})
        prefs = shows.preferences()
        self.assertEqual([(p["resolution"], p["audio"]) for p in prefs],
                         [("1080p", "ja"), ("1080p", "en"), ("720p", "ja"), ("720p", "en")])
        self.assertEqual({p["subtitle"] for p in prefs}, {"en"})

    def test_cleanup_waits_as_long_as_the_setting_says(self):
        settings.update({"keep_hours": "72"})
        watched_at = time.time() - 3600
        with mock.patch.object(cleanup, "watched", return_value=[(self.episode("a.mkv"), watched_at)]):
            (_, _, due), = cleanup.due()
        self.assertAlmostEqual(due - watched_at, 72 * 3600)

    def test_ready_notifications_can_be_turned_off(self):
        self.notify.stop()                                                        # use the real notify()
        try:
            settings.update({"notify_ready": "false"})
            with mock.patch.object(agent.subprocess, "run") as run:
                agent.notify("Anime FYA: ready", "Episode 1")
            run.assert_not_called()
            settings.update({"notify_ready": "true"})
            with mock.patch.object(agent.subprocess, "run") as run:
                agent.notify("Anime FYA: ready", "Episode 1")
            run.assert_called_once()
        finally:
            self.notify.start()


class Pages(Sandbox):
    def setUp(self):
        super().setUp()
        import status
        from http.server import ThreadingHTTPServer
        self.extra = [mock.patch.object(settings, "FILE", Path(self.tmp, "settings.json")),
                      mock.patch.object(shows, "FILE", Path(self.tmp, "shows.json"))]
        for p in self.extra:
            p.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), status.make_handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self.extra:
            p.stop()
        super().tearDown()

    def get(self, path, token=False, host=None):
        req = urllib.request.Request(self.base + path, headers={**({"X-MyAnimeWatcher": s.TOKEN} if token else {}),
                                                                 **({"Host": host} if host else {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, "", b""

    def test_every_page_shares_the_shell_and_gets_the_token(self):
        for path in ("/", "/stremio", "/weekly", "/settings"):
            code, ctype, body = self.get(path)
            page = body.decode("utf-8")
            self.assertEqual(code, 200, path)
            self.assertIn("text/html", ctype)
            self.assertIn('href="/kizuna.css"', page, path)
            self.assertIn('src="/kizuna.js"', page, path)
            self.assertIn(s.TOKEN, page, path)
            self.assertNotIn("__STREMIO_TOKEN__", page, path)

    def test_shared_assets_and_fonts_only_from_the_list(self):
        for path, kind in (("/kizuna.css", "text/css"), ("/kizuna.js", "javascript"), ("/fonts/Cairo-SemiBold.ttf", "font/ttf")):
            code, ctype, body = self.get(path)
            self.assertEqual(code, 200, path)
            self.assertIn(kind, ctype)
            self.assertTrue(body)
        for path in ("/fonts/../agent.py", "/fonts/Cairo-SemiBold.ttf/../../state.json", "/agent.py", "/settings.json",
                     "/stremio-account.bin", "/%2e%2e/agent.py"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_settings_api_needs_the_token(self):
        self.assertEqual(self.get("/api/stremio/settings")[0], 403)
        code, _, body = self.get("/api/stremio/settings", token=True)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["settings"], settings.DEFAULTS)
        self.assertEqual(self.get("/", host="evil.example")[0], 403)

    def test_status_separates_real_problems_from_waiting(self):
        import status
        d = status.collect()
        self.assertIn("attention", d)
        self.assertIn("pending", d)
        self.assertIsInstance(d["keep_hours"], int)


if __name__ == "__main__":
    unittest.main()
