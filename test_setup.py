"""First-run setup wizard (setup.py): checks, app installs via winget, folders, Stremio sign-in, finishing."""
import os
os.environ.setdefault("KIZUNA_SUBTITLE_LANGUAGE", "ar")
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import settings
import setup
import shows
import stremio
import stremio_account


class Setup(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.p = [mock.patch.object(settings, "FILE", self.d / "settings.json"),
                  mock.patch.object(shows, "RESETS", self.d / "shows-pending.json")]
        for p in self.p:
            p.start()
        settings._cache.update(stamp="reset", data=dict(settings.DEFAULTS), damaged=False)
        settings.update({k: str(self.d / k) for k in settings.FOLDERS})

    def tearDown(self):
        for p in self.p:
            p.stop()
        settings._cache.update(stamp="reset", data=dict(settings.DEFAULTS), damaged=False)
        shutil.rmtree(self.d, ignore_errors=True)

    def test_new_install_starts_unfinished_and_finish_sticks(self):
        self.assertFalse(settings.get("setup_done"))
        with mock.patch.object(setup, "check", return_value={}):
            setup.finish()
        self.assertTrue(settings.get("setup_done"))

    def test_check_reports_what_is_missing(self):
        with mock.patch("shutil.which", return_value=None), mock.patch.object(setup, "_jellyfin_up", return_value=False), \
                mock.patch.object(stremio, "libraries", return_value=[]), \
                mock.patch.object(stremio, "addons", side_effect=ValueError("Connect Stremio first")):
            c = setup.check()
        self.assertEqual(c["tools"]["ffmpeg"], False)
        self.assertFalse(c["ready"] or c["jellyfin"]["running"] or c["stremio"]["connected"])
        self.assertFalse(any(f["exists"] for f in c["folders"].values()))

    def test_libraries_and_addons_are_recognised(self):
        libs = [{"name": "Weekly Anime", "path": str(self.d)}]
        addons = [{"manifest": {"name": "Streams", "resources": ["stream", "meta"]}},
                  {"manifest": {"name": "Subs", "resources": [{"name": "subtitles"}]}}]
        with mock.patch.object(setup, "_jellyfin_up", return_value=True), mock.patch.object(stremio, "libraries", return_value=libs), \
                mock.patch.object(stremio, "addons", return_value=addons):
            c = setup.check()
        self.assertEqual((c["jellyfin"]["weekly"], c["jellyfin"]["anime"]), ("Weekly Anime", "Weekly Anime"))   # inside it
        self.assertTrue(c["stremio"]["streams"] and c["stremio"]["subtitles"])

    def test_folders_are_created(self):
        with mock.patch.object(setup, "check", return_value={}):
            made = setup.make_folders()["made"]
        self.assertEqual(len(made), 3)
        self.assertTrue(all(Path(p).is_dir() for p in made))

    def test_only_known_apps_install_in_a_visible_window(self):
        with self.assertRaises(setup.SetupError):
            setup.install("calc")
        with mock.patch("shutil.which", return_value="winget.exe"), mock.patch("subprocess.Popen") as popen:
            setup.install("mkvtoolnix")
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[:3], ["cmd", "/c", "start"])
        self.assertIn("MoritzBunkus.MKVToolNix", cmd)
        with mock.patch("shutil.which", return_value=None), self.assertRaisesRegex(setup.SetupError, "winget"):
            setup.install("ffmpeg")

    def test_stremio_sign_in_keeps_only_the_session_key(self):
        saved = []
        with mock.patch.object(stremio, "get_json", return_value={"result": {"authKey": "KEY"}}) as call, \
                mock.patch.object(stremio_account, "save", side_effect=saved.append), \
                mock.patch.object(stremio, "sync"), mock.patch.object(setup, "check", return_value={"ok": 1}):
            setup.stremio_login("me@example.com", "secret")
        self.assertEqual(call.call_args.args[1]["password"], "secret")       # sent to Stremio's sign-in only
        self.assertEqual(saved, [{"addons": [], "authKey": "KEY"}])
        self.assertNotIn("secret", repr(saved))
        with mock.patch.object(stremio, "get_json", return_value={"error": "bad"}), self.assertRaises(setup.SetupError):
            setup.stremio_login("me@example.com", "wrong")
        with self.assertRaises(setup.SetupError):
            setup.stremio_login("not-an-email", "x")


if __name__ == "__main__":
    unittest.main()
