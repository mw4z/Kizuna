"""Tests for the automatic fansub picker (fansub.py) and its use by the processing worker."""
import os
os.environ.setdefault("KIZUNA_SUBTITLE_LANGUAGE", "ar")   # this suite tests the Arabic pipeline
FIXTURE_POLICY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "subtitle_policy.json")
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import shows
shows.FILE = Path(__file__).parent / "fixtures" / "shows.json"   # test list, never the user's
import fansub
import processing_worker as worker


def sub(name, url=None, lang="ara"):
    return {"id": f"v3+|1|{name}", "lang": lang, "url": url or f"https://subs.example/{abs(hash(name))}.srt"}


def srt(lines=120, text="مرحبا بك يا صديقي العزيز"):
    return "".join(f"{i + 1}\n00:{i // 60:02d}:{i % 60:02d},000 --> 00:{i // 60:02d}:{i % 60:02d},800\n{text}\n\n"
                   for i in range(lines))


class Policy(unittest.TestCase):
    def test_modes_by_name_and_id(self):
        with mock.patch.object(fansub, "POLICY_FILE", FIXTURE_POLICY):      # the test show rules (Bleach, Conan)
            self.assertEqual(fansub.mode_for("Bleach"), "fansub_first")
            self.assertEqual(fansub.mode_for("المطهر"), "fansub_first")
            self.assertEqual(fansub.mode_for("", "tt0434665"), "fansub_first")
            self.assertEqual(fansub.mode_for("Detective Conan"), "manual")
            self.assertEqual(fansub.mode_for("Blue Box"), "official_ok")

    def test_policy_file_overrides(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "p.json")
        json.dump({"default": "fansub", "shows": []}, open(p, "w", encoding="utf-8"))
        with mock.patch.object(fansub, "POLICY_FILE", p):
            self.assertEqual(fansub.mode_for("Blue Box"), "fansub")
        shutil.rmtree(d)


class Ranking(unittest.TestCase):
    def test_fansub_mode_rejects_official_and_machine_and_ranks_groups(self):
        subs = [sub("Bleach - 101 [Almighty & Shahid][BD][Judas]"), sub("Bleach 206 AI translated"),
                sub("Bleach 206 Google Translate"), sub("Bleach Ep_206"), sub("[Pirates] Bleach EP 206"),
                sub("English one", lang="eng"), sub("[Pirates] dup", url="https://subs.example/same.srt"),
                sub("[Pirates] dup again", url="https://subs.example/same.srt")]
        names = [n for _, n, _ in fansub.ranked(subs, "fansub")]
        self.assertEqual(names, ["[Pirates] Bleach EP 206", "[Pirates] dup", "Bleach Ep_206"])

    def test_official_allowed_mode_keeps_official_but_never_machine(self):
        names = [n for _, n, _ in fansub.ranked([sub("Show [NF] WEB"), sub("Show DeepL"), sub("Show fan")], "official_ok")]
        self.assertEqual(names, ["Show [NF] WEB", "Show fan"])


class Numbering(unittest.TestCase):
    def test_absolute_number_from_cinemeta(self):
        meta = {"meta": {"videos": [{"season": 0, "episode": 1}] + [{"season": 1, "episode": e} for e in (1, 2)] +
                         [{"season": 2, "episode": e} for e in (1, 2, 3)]}}
        import stremio
        with mock.patch.object(stremio, "get_json", return_value=meta):
            self.assertEqual(fansub.episode_ids("tt1", 2, 2), ["tt1:2:2", "tt1:1:4"])   # specials ignored
            self.assertEqual(fansub.episode_ids("tt1", 1, 2), ["tt1:1:2"])              # already absolute
        with mock.patch.object(stremio, "get_json", side_effect=OSError("offline")):
            self.assertEqual(fansub.episode_ids("tt1", 2, 2), ["tt1:2:2"])


class TextCheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self, text):
        p = os.path.join(self.d, "x.srt")
        open(p, "w", encoding="utf-8").write(text)
        return p

    def test_real_arabic_passes_and_others_fail(self):
        self.assertTrue(fansub.text_ok(self.write(srt()))[0])
        self.assertFalse(fansub.text_ok(self.write(srt(text="Hello my dear friend")))[0])
        self.assertFalse(fansub.text_ok(self.write(srt(lines=40)))[0])
        self.assertFalse(fansub.text_ok(self.write("1\n00:00:00,000 --> 00:00:01,000\nترجمة: Google Translate AI\n\n"
                                                   + srt()))[0])


class OfficialCopies(unittest.TestCase):
    """Real case 2026-11-15: 'Black_Clover_S02E07_ARABIC_1' was Crunchyroll's Arabic, shifted one line and
    wrapped in U+2067/U+2069 direction marks."""

    def test_reupload_with_marks_and_shift_is_detected(self):
        d = tempfile.mkdtemp()
        lines = [f"هذه الجملة رقم {i} من الحلقة" for i in range(150)]
        off = os.path.join(d, "off.srt")
        open(off, "w", encoding="utf-8").write("".join(f"{i}\n00:00:01,000 --> 00:00:02,000\n{l} \n\n" for i, l in enumerate(lines)))
        cand = os.path.join(d, "c.srt")
        open(cand, "w", encoding="utf-8").write("".join(f"{i}\n00:00:01,000 --> 00:00:02,000\n⁧{l}⁩\n\n"
                                                        for i, l in enumerate(["سطر إضافي في البداية"] + lines)))
        fan = os.path.join(d, "f.srt")
        open(fan, "w", encoding="utf-8").write(srt(text="ترجمة مختلفة تماما من فريق المعجبين للحلقة"))
        official = fansub.normalized_lines(off)
        self.assertTrue(fansub.copies_official(cand, official))
        self.assertFalse(fansub.copies_official(fan, official))
        self.assertFalse(fansub.copies_official(cand, set()))          # no official to compare: not a copy
        shutil.rmtree(d)

    def test_official_ass_tracks_are_compared_but_our_own_is_not(self):
        # DKB ships Crunchyroll's Arabic as ASS: it must still count as the official text
        d = tempfile.mkdtemp()
        tracks = [{"id": 2, "type": "subtitles", "properties": {"language": "ara", "codec_id": "S_TEXT/ASS", "track_name": "العربية"}},
                  {"id": 8, "type": "subtitles", "properties": {"language": "ara", "codec_id": "S_TEXT/ASS",
                                                                "track_name": "Arabic (Saudi Arabia) / العربية"}}]
        extracted = []

        def run(cmd, **k):
            extracted.append(cmd[cmd.index("-map") + 1])
            open(cmd[-1], "w", encoding="utf-8").write(srt(text="هذا هو النص الرسمي للحلقة"))
            return mock.Mock(returncode=0)
        with mock.patch.object(agent, "tracks", return_value=tracks), mock.patch.object(fansub.subprocess, "run", run):
            lines = fansub.official_lines("v.mkv", d)
        self.assertEqual(extracted, ["0:8"])                            # not our own "العربية" output
        self.assertTrue(lines)
        shutil.rmtree(d)


class Pick(unittest.TestCase):
    """Real srt2ass conversion; add-ons, downloads and the audio timing check are faked."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.log = mock.patch.object(agent, "log")
        self.log.start()

    def tearDown(self):
        self.log.stop()
        shutil.rmtree(self.d)

    def run_pick(self, subs, fits_by_name, mode="fansub", broken=()):
        import stremio

        def download(s_, out):
            name = fansub.release_name(s_)
            if name in broken:
                return False
            open(out, "w", encoding="utf-8").write(srt(text=f"مرحبا بك يا صديقي العزيز في هذه الحلقة الجديدة {name}"))
            return True

        def timing(video, ass):
            text = open(ass, encoding="utf-8-sig").read()
            return 0.0, any(n in text and ok for n, ok in fits_by_name.items())
        with mock.patch.object(stremio, "addons", return_value=[{}]), \
                mock.patch.object(stremio, "supports", return_value=True), \
                mock.patch.object(stremio, "parallel", return_value=([{"subtitles": subs}], 0)), \
                mock.patch.object(fansub, "episode_ids", return_value=["tt1:1:1"]), \
                mock.patch.object(fansub, "download", side_effect=download), \
                mock.patch.object(agent, "timing", side_effect=timing):
            dest = os.path.join(self.d, "out.srt")
            return fansub.pick("video.mkv", "tt1", 1, 1, dest, mode), dest

    def test_first_candidate_that_fits_the_audio_wins(self):
        subs = [sub("[Pirates] broken"), sub("[Pirates] wrong timing"), sub("Bleach Ep_1 fits"),
                sub("Shahid official fits")]
        name, dest = self.run_pick(subs, {"wrong timing": False, "fits": True}, broken=("[Pirates] broken",))
        self.assertEqual(name, "Bleach Ep_1 fits")
        self.assertIn("fits", open(dest, encoding="utf-8").read())

    def test_nothing_acceptable_returns_none(self):
        name, dest = self.run_pick([sub("Shahid official fits"), sub("AI fits")], {"fits": True})
        self.assertIsNone(name)
        self.assertFalse(os.path.exists(dest))

    def test_fansub_first_prefers_any_fitting_fansub(self):
        name, _ = self.run_pick([sub("Shahid official fits"), sub("[Pirates] fits")], {"fits": True}, mode="fansub_first")
        self.assertEqual(name, "[Pirates] fits")

    def test_fansub_first_falls_back_to_official_never_machine(self):
        subs = [sub("[Pirates] wrong timing"), sub("AI fits"), sub("Shahid official fits")]
        name, _ = self.run_pick(subs, {"wrong timing": False, "fits": True}, mode="fansub_first")
        self.assertEqual(name, "Shahid official fits")
        name, _ = self.run_pick([sub("AI fits"), sub("Google fits")], {"fits": True}, mode="fansub_first")
        self.assertIsNone(name)

    def test_fansub_first_official_reupload_is_level_two(self):
        subs = [sub("Ep_1 reupload fits"), sub("[Pirates] wrong timing")]
        with mock.patch.object(fansub, "copies_official", side_effect=lambda c, o: "reupload" in open(c, encoding="utf-8").read()):
            name, _ = self.run_pick(subs, {"wrong timing": False, "fits": True}, mode="fansub_first")
            self.assertEqual(name, "Ep_1 reupload fits")             # tried again after the fansub failed
            name, _ = self.run_pick(subs, {"wrong timing": False, "fits": True}, mode="fansub")
            self.assertIsNone(name)                                  # strict fansub mode never takes it

    def test_manual_shows_never_pick(self):
        self.assertIsNone(fansub.pick("v", "tt1", 1, 1, os.path.join(self.d, "o.srt"), "manual"))


class Worker(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d)

    def prep(self, meta, embedded, picked):
        folder = self.d / str(abs(hash(json.dumps(meta))))
        folder.mkdir()
        (folder / "metadata.json").write_text(json.dumps({"type": "series", "season": 1, "episode": 1, "meta": meta}))
        (folder / "raw.media").write_bytes(b"raw")

        def fetch(metadata, destination, mode, media):
            if picked:
                Path(destination).write_text(srt(), encoding="utf-8")
            return picked
        with mock.patch.object(agent, "tracks", return_value=[]), mock.patch.object(agent, "target_track", return_value=embedded), \
                mock.patch.object(agent, "process", return_value=True) as process, \
                mock.patch.object(worker, "fetch_subtitle", side_effect=fetch) as f, \
                mock.patch.object(fansub, "POLICY_FILE", FIXTURE_POLICY):
            ready = worker.prepare(folder)
        return ready, process, f

    def test_fansub_show_uses_picked_fansub_not_embedded(self):
        ready, process, f = self.prep({"name": "Bleach"}, embedded=3, picked="[Pirates] Bleach EP 1")
        self.assertTrue(ready)
        self.assertEqual(f.call_args.args[2], "fansub_first")
        self.assertTrue(process.call_args.kwargs["subtitle_path"].endswith("arabic.srt"))

    def test_fansub_first_uses_embedded_official_as_last_resort(self):
        ready, process, f = self.prep({"name": "Bleach"}, embedded=3, picked=None)
        self.assertTrue(ready)
        self.assertEqual(f.call_args.args[2], "fansub_first")
        self.assertIsNone(process.call_args.kwargs["subtitle_path"])   # the file's own Arabic track

    def test_fansub_first_waits_when_nothing_at_all(self):
        ready, process, f = self.prep({"name": "Bleach"}, embedded=None, picked=None)
        self.assertFalse(ready)
        process.assert_not_called()

    def test_official_show_keeps_embedded_official_arabic(self):
        ready, process, f = self.prep({"name": "Blue Box"}, embedded=3, picked=None)
        self.assertTrue(ready)
        f.assert_not_called()
        self.assertIsNone(process.call_args.kwargs["subtitle_path"])

    def test_official_show_without_arabic_asks_the_picker(self):
        ready, process, f = self.prep({"name": "Blue Box"}, embedded=None, picked="Blue Box fan")
        self.assertEqual(f.call_args.args[2], "official_ok")

    def test_manual_show_never_processed(self):
        ready, process, f = self.prep({"name": "Detective Conan"}, embedded=None, picked="x")
        self.assertFalse(ready)
        process.assert_not_called()
        f.assert_not_called()


class Identify(unittest.TestCase):
    """Real case 2026-11-15: 'Bleach' matched tt0434665 (2004) AND tt33957030, so the picker never ran."""

    def fake_stremio(self, metas):
        s = mock.Mock()
        s.search.return_value = {"results": [{"name": m["name"], "key": i} for i, m in enumerate(metas)]}
        s.item.side_effect = lambda key, kind: {"meta": metas[key]}
        return s

    def test_year_resolves_shared_names(self):
        s = self.fake_stremio([{"name": "Bleach", "id": "tt0434665", "releaseInfo": "2004–2012"},
                               {"name": "Bleach", "id": "tt33957030"}])
        self.assertEqual(worker.identify(s, {"name": "Bleach", "releaseInfo": "2004"}, "series"), "tt0434665")
        self.assertIsNone(worker.identify(s, {"name": "Bleach"}, "series"))          # no year: ambiguous

    def test_policy_pins_whole_title_only(self):
        with mock.patch.object(fansub, "POLICY_FILE", FIXTURE_POLICY):
            self.assertEqual(fansub.imdb_for("Bleach"), "tt0434665")
            self.assertEqual(fansub.imdb_for("المطهر"), "tt0434665")
            self.assertIsNone(fansub.imdb_for("Bleach: Thousand-Year Blood War"))
            self.assertIsNone(fansub.imdb_for("Blue Box"))

    def test_unidentified_show_is_logged_not_silent(self):
        with mock.patch.object(fansub, "POLICY_FILE", FIXTURE_POLICY), \
                mock.patch.object(worker, "identify", return_value=None), \
                mock.patch.object(agent, "log") as log, mock.patch.object(fansub, "pick") as pick:
            self.assertIsNone(worker.fetch_subtitle({"type": "series", "season": 1, "episode": 1,
                                                   "meta": {"name": "Some Show"}}, "o.srt", "fansub", "v.mkv"))
        pick.assert_not_called()
        self.assertTrue(any("could not identify" in str(c) for c in log.call_args_list))


class AgentFallback(unittest.TestCase):
    def test_weekly_episode_uses_clean_show_title_and_policy(self):
        with mock.patch.object(worker, "fetch_subtitle", return_value="fan") as f, mock.patch.object(agent, "log"):
            self.assertTrue(agent.addon_subtitle("[DKB] Tokyo Revengers Santen Sensou-hen - S04E08 [1080p].mkv",
                                               "v.mkv", "out.srt"))
        meta, out, mode, video = f.call_args.args
        self.assertEqual(meta, {"type": "series", "season": 4, "episode": 8, "meta": {"name": "Tokyo Revengers"}})
        self.assertEqual(mode, "official_ok")


if __name__ == "__main__":
    unittest.main()
