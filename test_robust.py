"""Regression tests for the 2026-11-17 "Needs a look" causes: Arabic folder names, hardsubbed files,
over-cautious timing, and subtitle-site adverts."""
import os
os.environ.setdefault("KIZUNA_SUBTITLE_LANGUAGE", "ar")   # this suite tests the Arabic pipeline
import contextlib
import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import hardsub
import processing_worker as worker
import srt2ass
from test_audit import Sandbox


def srt_text(starts, text="مرحبا بك يا صديقي العزيز"):
    def t(x):
        h, r = divmod(x, 3600)
        m, s = divmod(r, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)) % 1000:03d}"
    return "".join(f"{i + 1}\n{t(a)} --> {t(a + 1.5)}\n{text}\n\n" for i, a in enumerate(starts))


class Adverts(unittest.TestCase):
    def test_opensubtitles_and_release_name_lines_are_dropped(self):
        d = tempfile.mkdtemp()
        src = os.path.join(d, "a.srt")
        open(src, "w", encoding="utf-8").write(
            "1\n00:00:01,000 --> 00:00:06,000\n>>OpenSubtitles v3+ v0.0.4<<\n<u>=>Collateral.2004.UHD.BluRay.srt</u>\n\n"
            "2\n00:00:07,000 --> 00:00:08,000\nزوروا موقعنا www.example.com\n\n"
            "3\n00:01:06,740 --> 00:01:09,510\n- أنت بخير؟\n- أنا بخير يا صاح، لا تقلق.\n\n")
        out = os.path.join(d, "a.ass")
        self.assertEqual(srt2ass.convert(src, out, "t"), 1)
        self.assertNotIn("OpenSubtitles", open(out, encoding="utf-8-sig").read())


class ReferenceTiming(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def write(self, name, starts):
        p = os.path.join(self.d, name)
        open(p, "w", encoding="utf-8").write(srt_text(starts))
        return p

    def test_shift_found_from_the_files_own_subtitle_track(self):
        rnd = random.Random(7)
        ref = sorted(rnd.uniform(0, 6600) for _ in range(1100))
        # the Arabic was made for another release: 2.35 s late, a few lines split/merged differently
        ara = [t - 2.35 + rnd.choice((0, 0, 0, 0.02, -0.03)) for t in ref if rnd.random() > 0.1]
        ara += [rnd.uniform(0, 6600) for _ in range(80)]
        self.assertAlmostEqual(agent.cue_shift(self.write("en.srt", ref), self.write("ar.srt", sorted(ara))), 2.35, delta=0.06)

    def test_unrelated_subtitles_give_no_shift(self):
        rnd = random.Random(3)
        ref = sorted(rnd.uniform(0, 6600) for _ in range(1100))
        other = sorted(rnd.uniform(0, 6600) for _ in range(1100))
        self.assertIsNone(agent.cue_shift(self.write("en.srt", ref), self.write("ar.srt", other)))


class TimingFallback(Sandbox):
    def run_process(self, ref, recheck):
        srt = Path(self.tmp, "in.srt")
        srt.write_text(srt_text(range(0, 1200, 3)), encoding="utf-8")
        calls = []
        with mock.patch.object(agent, "tracks", return_value=[]), mock.patch.object(agent, "target_track", return_value=None), \
                mock.patch.object(agent, "timing", side_effect=[(0.0, False), recheck]), \
                mock.patch.object(agent, "reference_offset", return_value=ref), \
                mock.patch.object(agent.srt2ass, "convert", side_effect=lambda *a, **k: calls.append(a)), \
                mock.patch.object(agent, "qc", return_value=["stop here"]):
            agent.process(self.episode("Movie.mkv"), agent.load_state(), publish=False, subtitle_path=str(srt))
        return calls

    def test_reference_shift_used_when_audio_confirms_it(self):
        calls = self.run_process(2.35, (0.0, True))
        self.assertEqual(calls[-1][-1], 2.35)                                 # final convert carries the shift
        self.assertNotIn("Timing inconsistent", Path(agent.PFILE).read_text(encoding="utf-8"))

    def test_reference_shift_rejected_when_audio_disagrees(self):
        self.run_process(2.35, (0.0, False))
        self.assertIn("Timing inconsistent", Path(agent.PFILE).read_text(encoding="utf-8"))


class JellyfinLookup(unittest.TestCase):
    def fake_db(self):
        c = sqlite3.connect(":memory:")
        c.executescript("CREATE TABLE BaseItems(Id, Name, Type); CREATE TABLE BaseItemProviders(ItemId, ProviderId, ProviderValue);")
        c.execute("INSERT INTO BaseItems VALUES (1, 'حادثة داروين', 'MediaBrowser.Controller.Entities.TV.Series')")
        c.execute("INSERT INTO BaseItemProviders VALUES (1, 'Imdb', 'tt38980285'), (1, 'Tmdb', '255064')")
        return contextlib.contextmanager(lambda: iter([c]))

    def test_arabic_folder_name_resolves_through_jellyfin(self):
        with mock.patch.object(agent, "jellyfin_db", self.fake_db()):
            self.assertEqual(worker.jellyfin_imdb("حادثة داروين", "series"), "tt38980285")
            self.assertIsNone(worker.jellyfin_imdb("حادثة داروين", "movie"))
            self.assertIsNone(worker.jellyfin_imdb("Unknown", "series"))


class EpisodeImdbId(unittest.TestCase):
    def test_tmdb_show_id_uses_the_episode_imdb_id(self):
        # Dexter: Resurrection: meta id "tmdb:259909", episode id "tt33043892:2:3" -> searched nothing before
        md = {"type": "series", "id": "tt33043892:2:3", "season": 2, "episode": 3,
              "meta": {"id": "tmdb:259909", "name": "دكستر: القيامة", "releaseInfo": "2025-"}}
        import stremio
        with mock.patch.object(stremio, "get_json", return_value={"meta": {"name": "Dexter: Resurrection"}}), \
                mock.patch.object(worker.fansub, "pick", return_value="Arabic") as pick:
            self.assertEqual(worker.fetch_subtitle(md, "out.srt", "official_ok", "media.mkv"), "Arabic")
        self.assertEqual(pick.call_args.args[1:4], ("tt33043892", 2, 3))


class BurnedIn(unittest.TestCase):
    def test_hardsubbed_file_is_left_unchanged(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "metadata.json").write_text(json.dumps({"type": "series", "season": 1, "episode": 12,
                                                          "meta": {"name": "Show"}}))
        (folder / "raw.media").write_bytes(b"raw")
        with mock.patch.object(agent, "tracks", return_value=[]), mock.patch.object(agent, "target_track", return_value=None), \
                mock.patch.object(hardsub, "burned_in_subtitles", return_value=(True, 5, 7)), \
                mock.patch.object(worker, "fetch_subtitle") as fetch, mock.patch.object(agent, "process") as process:
            self.assertEqual(worker.prepare(folder), worker.BURNED_IN)
        fetch.assert_not_called()
        process.assert_not_called()

    def test_needs_several_frames_of_arabic_dialogue(self):
        self.assertEqual(hardsub.dialogue_words("إنه جهاز لوحي أعطاني"), ["إنه", "جهاز", "لوحي", "أعطاني"])
        self.assertEqual(hardsub.dialogue_words("1080p BluRay"), [])
        self.assertGreaterEqual(hardsub.MIN_HITS, 4)          # one Spacetoon title card must never be enough


if __name__ == "__main__":
    unittest.main()
