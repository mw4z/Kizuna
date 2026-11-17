"""Offline checks: python test_dkb.py."""
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import urllib.parse

import dkb
import shows
shows.FILE = Path(__file__).parent / "fixtures" / "shows.json"   # test list, never the user's


def title(ep, show="Ao no Hako"):
    return f"[DKB] {show} - {ep} [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs][weekly]"


URL = "https://nyaa.si/download/123.torrent"


class DKBTests(unittest.TestCase):
    def setUp(self):
        log = patch.object(dkb.agent, "log")
        log.start()
        self.addCleanup(log.stop)
        tmp = tempfile.TemporaryDirectory()             # never look at the real library, inbox or state files
        self.addCleanup(tmp.cleanup)
        for p in [patch.object(dkb.agent, n, tmp.name) for n in ("LIBRARY", "FOLDER", "HERE")] + \
                 [patch.object(shows, "STATE", Path(tmp.name) / "weekly-state.json")]:
            p.start()
            self.addCleanup(p.stop)

    def test_real_listing(self):
        parser = dkb.ReleaseLinks()
        parser.feed((Path(__file__).parent / "fixtures" / "dkb-releases.html").read_text(encoding="utf-8"))
        self.assertEqual(dkb.episode(parser.releases[0][0]), ("Ao no Hako", "S02E05"))
        self.assertEqual(parser.releases[0][1], "https://nyaa.si/download/2184099.torrent")

    def test_season_title_punctuation(self):
        parser = dkb.ReleaseLinks()
        parser.feed((Path(__file__).parent / "fixtures" / "dkb-tokyo.html").read_text(encoding="utf-8"))
        self.assertEqual(dkb.episode(parser.releases[0][0]), ("Tokyo Revengers", "S04E07"))
        self.assertEqual(dkb.episode("[DKB] Tokyo Revengers Santen Sensou-hen - S04E07 [1080p].mkv"),
                         ("Tokyo Revengers", "S04E07"))

    def test_only_followed_weekly_1080p_dkb(self):
        for release in (title("S02E06"), title("S02E06", "Unwanted"),
                        title("S02E06").replace("1080p", "720p"),
                        title("S02E06").replace("weekly", "batch"),
                        title("S02E06").replace("[DKB]", "[Other]")):
            parser = dkb.ReleaseLinks()
            parser.feed(f'<a href="{URL}">Torrent</a><a href="magnet:?dn={urllib.parse.quote(release)}">Magnet</a>')
            self.assertEqual(len(parser.releases), int(release == title("S02E06")))

    def test_baseline_new_episode_order_and_duplicates(self):
        state = {"baseline": {}, "done": {}}
        self.assertEqual(dkb.candidates(state, "Ao no Hako", [(title("S02E05"), URL)]), [])
        state["done"]["Ao no Hako|S02E06"] = "existing.mkv"
        releases = [(title(ep), URL) for ep in ("S02E10", "S02E05", "S02E06", "S02E07")]
        self.assertEqual([dkb.episode(t)[1] for t, _ in dkb.candidates(state, "Ao no Hako", releases)],
                         ["S02E07", "S02E10"])

    def test_reject_mismatched_and_unsafe_metadata(self):
        class Files:
            def num_files(self): return 1
            def file_path(self, i): return self.name
            def file_size(self, i): return 123
            def symlink(self, i): return ""
        class Torrent:
            def files(self): return files
        files = Files()
        files.name = title("S02E06") + ".mkv"
        self.assertEqual(dkb.torrent_file(Torrent(), title("S02E06")), (files.name, 123))
        for name in ("../escape.mkv", "C:\\escape.mkv", title("S02E07") + ".mkv",
                     title("S02E06").replace("1080p", "720p") + ".mkv"):
            files.name = name
            with self.assertRaises(ValueError):
                dkb.torrent_file(Torrent(), title("S02E06"))

    def test_timeout_is_retried_then_completed_once(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(dkb, "STATE", str(Path(folder) / "state.json")), \
                patch.object(dkb, "SHOWS", ("Ao no Hako",)), \
                patch.object(dkb.agent, "log"), \
                patch.object(dkb, "listing") as listing, \
                patch.object(dkb, "existing", return_value=None), \
                patch.object(dkb, "download") as download:
            listing.return_value = [(title("S02E05"), URL)]
            ready = unittest.mock.Mock()
            dkb.poll(folder, ready)
            download.assert_not_called()
            listing.return_value.append((title("S02E06"), URL))
            download.side_effect = TimeoutError("partial")
            self.assertEqual(dkb.poll(folder, ready)["done"], {})
            ready.assert_not_called()
            download.side_effect = None
            download.return_value = str(Path(folder) / "finished.mkv")
            state = dkb.poll(folder, ready)
            self.assertIn("Ao no Hako|S02E06", state["done"])
            ready.assert_called_once_with(download.return_value)
            dkb.poll(folder, ready)
            self.assertEqual(download.call_count, 2)

    def test_corrupt_state_does_not_reset_baseline(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(dkb, "STATE", str(Path(folder) / "state.json")), \
                patch.object(dkb, "listing") as listing:
            Path(dkb.STATE).write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                dkb.poll(folder, lambda p: None)
            listing.assert_not_called()

    def test_only_verified_complete_download_is_published(self):
        name = title("S02E06") + ".mkv"
        files = types.SimpleNamespace(num_files=lambda: 1, file_path=lambda i: name,
                                      file_size=lambda i: 123, symlink=lambda i: "")
        lt = types.SimpleNamespace(bdecode=lambda data: data,
                                   torrent_info=lambda data: types.SimpleNamespace(files=lambda: files))
        with tempfile.TemporaryDirectory() as root, \
                patch.dict("sys.modules", {"libtorrent": lt}), \
                patch.object(dkb.agent, "WORK", str(Path(root) / "work")), \
                patch.object(dkb, "fetch", return_value=b"torrent"), \
                patch.object(dkb.agent, "LOG", str(Path(root) / "log.txt")), \
                patch.object(dkb.agent, "tracks", return_value=[{"type": "video"}]), \
                patch.object(dkb.shutil, "disk_usage", return_value=types.SimpleNamespace(free=10**12)), \
                patch.object(dkb.subprocess, "run") as run:
            folder = str(Path(root) / "incoming")
            source = Path(root) / "work" / "dkb" / "123" / name
            run.side_effect = dkb.subprocess.CalledProcessError(1, "torrent.py")
            with self.assertRaises(dkb.subprocess.CalledProcessError):
                dkb.download(title("S02E06"), URL, folder)
            self.assertEqual(list(Path(folder).glob("*.mkv")), [])
            run.side_effect = lambda *a, **k: source.write_bytes(b"x" * 122)
            with self.assertRaises(RuntimeError):
                dkb.download(title("S02E06"), URL, folder)
            self.assertEqual(list(Path(folder).glob("*.mkv")), [])
            run.side_effect = lambda *a, **k: source.write_bytes(b"x" * 123)
            path = dkb.download(title("S02E06"), URL, folder)
            self.assertEqual(Path(path).read_bytes(), b"x" * 123)
            self.assertFalse(Path(path + ".part").exists())
            self.assertFalse(source.exists())


if __name__ == "__main__":
    unittest.main()
