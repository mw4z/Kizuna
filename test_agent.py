"""Arabic dubs must bypass subtitle downloads, styling, timing, and muxing."""
import os
os.environ.setdefault("KIZUNA_SUBTITLE_LANGUAGE", "ar")   # this suite tests the Arabic pipeline
import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import agent


def audio(**properties):
    return {"type": "audio", "properties": properties}


class Tests(unittest.TestCase):
    def test_notifications_distinguish_movies_and_episodes(self):
        with patch.object(agent, "log"), patch.object(agent.subprocess, "run") as toast:
            agent.notify("Kizuna: movie ready", "Finding Nemo")
            self.assertEqual(toast.call_args.kwargs["env"]["T1"], "New movie ready")
            agent.notify("Kizuna: ready", "Episode 1")
            self.assertEqual(toast.call_args.kwargs["env"]["T1"], "New episode ready")

    def test_dub_detection_uses_audio_not_subtitles(self):
        for track in (audio(language="ara"), audio(language_ietf="ar-EG"), audio(track_name="Arabic (dub)"),
                      audio(track_name="العربية")):
            self.assertTrue(agent.has_target_audio([audio(language="jpn"), track]))
        self.assertFalse(agent.has_target_audio([{"type": "subtitles", "properties": {"language": "ara"}}]))
        self.assertFalse(agent.has_target_audio([audio(language="ara", flag_commentary=True)]))
        self.assertFalse(agent.has_target_audio([audio(language="ara", track_name="Arabic commentary")]))
        self.assertFalse(agent.has_target_audio([audio(language="jpn")]))

    def test_dub_is_copied_without_any_subtitle_work(self):
        state = {"errors": {"dub.mkv": 2}, "failed": {"dub.mkv": 123}}
        with patch.object(agent, "tracks", return_value=[audio(language="ara")]), \
                patch.object(agent, "copy_to_library") as copy, patch.object(agent, "show"), \
                patch.object(agent, "log"), patch.object(agent, "nyaa_subtitle") as download, \
                patch.object(agent.srt2ass, "convert") as convert, patch.object(agent, "timing") as timing, \
                patch.object(agent.mux, "mux") as mux:
            agent.process("dub.mkv", state)
            copy.assert_called_once_with("dub.mkv")
            for work in (download, convert, timing, mux):
                work.assert_not_called()
            self.assertEqual(state, {"errors": {}, "failed": {}})

    def test_ordinary_episode_still_searches_for_arabic_subtitles(self):
        state = {"nyaa_tries": {}, "told": {}}
        with patch.object(agent, "tracks", return_value=[audio(language="jpn")]), \
                patch.object(agent, "target_track", return_value=None), \
                patch.object(agent, "nyaa_subtitle", return_value=False) as download, \
                patch.object(agent, "show"), patch.object(agent, "notify"):
            agent.process("[DKB] Test - S01E01.mkv", state)
            download.assert_called_once()

    def test_old_failure_marker_does_not_prevent_dub_bypass(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "Detective Conan - S01E01.mkv"
            video.write_bytes(b"test")
            state = {"sizes": {}, "failed": {video.name: 4}}
            with patch.object(agent, "HERE", folder), patch.object(agent, "FOLDER", folder), \
                    patch.object(agent, "WORK", str(Path(folder) / "work")), patch.object(agent, "NOW", True), \
                    patch.object(agent, "lock_held", return_value=False), patch.object(agent, "load_state", return_value=state), \
                    patch.object(agent, "save_state"), patch.object(agent, "log"), \
                    patch.object(agent, "tracks", return_value=[audio(language="ara")]), patch.object(agent, "process") as process:
                agent.main()
                process.assert_called_once_with(str(video), state)

    def test_watcher_stops_retrying_dub_once_library_copy_exists(self):
        tree = ast.parse(Path(agent.HERE, "watcher.py").read_text(encoding="utf-8-sig"))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "needs_work")
        namespace = {"agent": agent, "os": os, "json": json}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "watcher.py", "exec"), namespace)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(agent, "tracks", return_value=[audio(language="ara")]), \
                patch.object(agent, "library_target", return_value=str(Path(folder) / "library.mkv")):
            self.assertTrue(namespace["needs_work"]("Detective Conan - S01E01.mkv"))
            Path(folder, "library.mkv").write_bytes(b"test")
            self.assertFalse(namespace["needs_work"]("Detective Conan - S01E01.mkv"))

    def test_subtitle_lines_past_the_video_end_are_cut(self):
        # Blu-ray subs on a shorter TV file (Bleach S14 KM): the mux length check failed on 12 s of extra lines
        with tempfile.TemporaryDirectory() as folder:
            ass = Path(folder, "ar.ass")
            ass.write_text("[Events]\nDialogue: 0,0:23:59.00,0:24:01.00,Default,,0,0,0,,a\n"
                           "Dialogue: 0,0:24:03.00,0:24:14.37,Default,,0,0,0,,b\n"
                           "Dialogue: 0,0:24:14.37,0:24:16.37,Default,,0,0,0,,c\n", encoding="utf-8-sig")
            self.assertEqual(agent.clamp_to(str(ass), 1444.44), 2)
            lines = [l for l in ass.read_text(encoding="utf-8-sig").splitlines() if l.startswith("Dialogue")]
            self.assertEqual(lines, ["Dialogue: 0,0:23:59.00,0:24:01.00,Default,,0,0,0,,a",
                                     "Dialogue: 0,0:24:03.00,0:24:04.44,Default,,0,0,0,,b"])
            self.assertEqual(agent.clamp_to(str(ass), 1444.44), 0)              # already inside: untouched


if __name__ == "__main__":
    unittest.main()
