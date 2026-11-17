"""Offline queue checks; no account requests or real library downloads."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import stremio as s
import stremio_batch as b


class Tests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.patch = patch.object(b, "FILE", self.root / "batch.json")
        self.patch.start()
        b._state = None
        b._cancel.clear()

    def tearDown(self):
        self.patch.stop()
        b._state = None
        b._cancel.clear()
        s._items.clear()
        self.folder.cleanup()

    def data(self):
        return {"name": "Test", "meta": {"name": "Test", "releaseInfo": "2024"}, "type": "series", "scope": "show",
                "library": "Test", "library_id": "test", "preferences": {
                    "audio": "ar", "subtitle": "none", "resolution": "1080p", "hdr": "SDR", "provider": "Test"},
                "status": "running", "stage": "Starting", "episodes": [
                    {"id": f"tt1:1:{i}", "season": 1, "episode": i, "status": "pending"} for i in range(1, 5)]}

    def test_dismiss_is_saved_and_survives_reload_until_resume(self):
        # 2026-11-17: a page-only dismiss came back after every reload
        data = self.data()
        data.update(status="done", stage="Batch finished", created=111)
        for e in data["episodes"]:
            e["status"] = "done"
        data["episodes"][0]["status"] = "failed"
        b.save(data)
        self.assertIsNotNone(b.status())
        self.assertIsNone(b.dismiss())
        b._state = None                                         # like an app restart / another device
        self.assertIsNone(b.status())
        self.assertTrue(json.loads(b.FILE.read_text())["dismissed"])
        with patch.object(b, "launch", side_effect=lambda d: b.save(dict(d, status="running"))):
            b.resume()                                          # resuming brings it back
        self.assertFalse(json.loads(b.FILE.read_text())["dismissed"])
        self.assertIsNotNone(b.status())

    def test_running_batch_cannot_be_dismissed(self):
        b.save(self.data())
        with patch.object(b, "active", return_value=True):
            with self.assertRaises(s.UserError):
                b.dismiss()
        self.assertNotIn("dismissed", json.loads(b.FILE.read_text()))

    def test_season_and_show_include_only_released_unique_episodes(self):
        meta = {"videos": [
            {"id": "special", "season": 0, "episode": 1}, {"id": "one", "season": 1, "episode": 1},
            {"id": "one", "season": 1, "episode": 1}, {"id": "two", "season": 2, "episode": 1},
            {"id": "future", "season": 2, "episode": 2, "released": "2099-01-01T00:00:00Z"}]}
        self.assertEqual([v["id"] for v in b.episode_list(meta, "season", 1)], ["one"])
        self.assertEqual([v["id"] for v in b.episode_list(meta, "show", 1)], ["special", "one", "two"])

    def test_preferences_preserve_actual_language_quality_and_hdr(self):
        source = {"probe": {"streams": [{"index": 0, "codec_type": "video", "width": 1920, "height": 1080,
                                        "color_transfer": "bt709"},
                                       {"index": 1, "codec_type": "audio", "tags": {"language": "ara"}},
                                       {"index": 2, "codec_type": "subtitle", "tags": {"language": "eng"}}]}}
        pref = b.preferences(source, "1", "2")
        self.assertEqual((pref["audio"], pref["subtitle"], pref["resolution"], pref["hdr"]), ("ar", "en", "1080p", "SDR"))
        with self.assertRaises(s.UserError):
            b.preferences(source, "none", "none")
        source["probe"]["streams"][1]["tags"]["language"] = "und"
        with self.assertRaises(s.UserError):
            b.preferences(source, "1", "none")

    def test_selected_episodes_only_queue_exact_released_choices_from_current_season(self):
        meta = {"name": "Show", "videos": [
            {"id": "one", "season": 1, "episode": 1}, {"id": "two", "season": 1, "episode": 2},
            {"id": "three", "season": 1, "episode": 3}, {"id": "other", "season": 2, "episode": 9},
            {"id": "future", "season": 1, "episode": 4, "released": "2099-01-01T00:00:00Z"}]}
        source = {"video": {"type": "series", "season": 1, "meta": meta}}
        with patch.object(s, "item", return_value=source), patch.object(b, "preferences", return_value={}), \
                patch.object(s, "libraries", return_value=[{"id": "lib", "name": "Anime"}]), \
                patch.object(b, "launch") as launch, patch.object(b, "status"):
            b.start("key", "1", "none", "lib", "selected", "[3,1,3]")
            self.assertEqual([e["episode"] for e in launch.call_args.args[0]["episodes"]], [1, 3])
            for invalid in ("[]", "[9]", "[4]", "[true]", "[\"1\"]", "oops", "{}"):
                launch.reset_mock()
                with self.assertRaises(s.UserError):
                    b.start("key", "1", "none", "lib", "selected", invalid)
                launch.assert_not_called()

    def test_queue_keeps_going_and_preserves_outcomes(self):
        data = self.data()
        library = {"id": "test", "name": "Test", "path": str(self.root)}
        video = {"type": "series", "meta": data["meta"], "season": 1, "episode": 1}
        destination = s.target(video, library)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"existing")
        b._busy.acquire()
        with patch.object(s, "libraries", return_value=[library]), \
                patch.object(s.shutil, "disk_usage", return_value=types.SimpleNamespace(free=10**12)), \
                patch.object(b, "source_identity", side_effect=lambda key: key), \
                patch.object(b, "choose", side_effect=[None, ("source", "1", "none"), ("bad", "1", "none")]), \
                patch.object(s, "start_download", side_effect=[{"status": "done", "stage": "Saved"},
                                                               {"status": "failed", "stage": "Source failed"}]) as download:
            b.worker(data)
        self.assertEqual([e["status"] for e in data["episodes"]], ["existing", "unavailable", "done", "failed"])
        self.assertEqual(download.call_count, 2)
        self.assertTrue(all(c.kwargs["_batch"] for c in download.call_args_list))
        self.assertEqual(json.loads(b.FILE.read_text())["episodes"][2]["status"], "done")
        self.assertFalse(b.active())
        self.assertEqual(b.status()["counts"]["unavailable"], 1)

    def test_stop_preserves_remaining_episodes_and_resume_keeps_completed(self):
        data = self.data()
        data["episodes"][0]["status"] = "done"
        b._busy.acquire()
        b._cancel.set()
        with patch.object(b, "choose") as choose:
            b.worker(data)
            choose.assert_not_called()
        self.assertEqual(data["status"], "stopped")
        self.assertEqual(data["episodes"][1]["status"], "pending")
        with patch.object(b, "launch") as launch:
            b.resume()
            self.assertEqual(launch.call_args.args[0]["episodes"][0]["status"], "done")

    def test_actual_tracks_must_match_before_download(self):
        candidates = [
            {"key": "wrong-language", "quality": "1080p", "audio_languages": ["ja"]},
            {"key": "wrong-resolution", "quality": "720p", "audio_languages": ["ar"]},
            {"key": "correct", "quality": "1080p", "audio_languages": ["ar"]}]
        info = {"audio": [{"id": "1", "language": "ara"}], "subtitles": [], "dynamic_range": "SDR"}
        source = {"probe": {"streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}}
        with patch.object(s, "streams", return_value={"sources": candidates, "failed_addons": 0}), \
                patch.object(s, "inspect", return_value=info) as inspect, patch.object(s, "item", return_value=source):
            selected = b.choose(self.data(), {"kind": "video"})
            self.assertEqual(selected, ("correct", "1", "none"))
            inspect.assert_called_once_with("correct")

    def test_disk_full_pauses_without_consuming_pending_queue(self):
        data = self.data()
        library = {"id": "test", "name": "Test", "path": str(self.root)}
        b._busy.acquire()
        with patch.object(s, "libraries", return_value=[library]), \
                patch.object(s.shutil, "disk_usage", return_value=types.SimpleNamespace(free=0)), \
                patch.object(b, "choose") as choose:
            b.worker(data)
            choose.assert_not_called()
        self.assertEqual(data["status"], "paused")
        self.assertTrue(all(e["status"] == "pending" for e in data["episodes"]))

    def test_arabic_preparation_waiting_is_not_announced_done_or_failed(self):
        data = self.data()
        data["episodes"] = data["episodes"][:1]
        library = {"id": "test", "name": "Test", "path": str(self.root)}
        b._busy.acquire()
        with patch.object(s, "libraries", return_value=[library]), patch.object(s.shutil, "disk_usage", return_value=types.SimpleNamespace(free=10**12)), \
                patch.object(b, "source_identity", return_value="source"), patch.object(b, "choose", return_value=("source", "1", "none")), \
                patch.object(s, "start_download", return_value={"status": "waiting", "stage": "Arabic pending"}):
            b.worker(data)
            state = b.status()
        self.assertEqual(state["status"], "waiting")
        self.assertEqual(state["counts"]["waiting"], 1)
        self.assertEqual(state["counts"]["done"], 0)


if __name__ == "__main__":
    unittest.main()
