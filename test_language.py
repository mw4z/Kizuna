"""Subtitle language other than Arabic (Settings > Subtitles > Language): detection, conversion, naming, the add-on
picker, a real mkvmerge mux in English, and libass previews for every language. The rest of the suite pins Arabic."""
import base64
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import agent
import fansub
import hardsub
import language
import mux
import settings
import shows
import srt2ass
import subtitle_style as st
from test_reliability import make_video

EN = {"KIZUNA_SUBTITLE_LANGUAGE": "en"}
SAMPLES = {
    "en": "What are you doing here? I don't know what you mean. This is the end of it.",
    "es": "¿Qué estás haciendo aquí? No sé lo que quieres decir. Es el fin de esto.",
    "pt": "O que você está fazendo aqui? Eu não sei o que isso quer dizer. É o fim.",
    "fr": "Qu'est-ce que vous faites ici ? Je ne sais pas ce que tu veux dire. C'est la fin.",
    "de": "Was machst du hier? Ich weiß nicht, was du meinst. Das ist das Ende und wir gehen.",
    "it": "Cosa stai facendo qui? Non so che cosa intendi. Questo è la fine per me.",
    "tr": "Burada ne yapıyorsun? Ne demek istediğini bilmiyorum. Bu bir son ve çok değil.",
    "pl": "Co tu robisz? Nie wiem, co masz na myśli. To jest koniec, ale tak.",
    "nl": "Wat doe je hier? Ik weet niet wat je bedoelt. Het is een einde van dat.",
    "id": "Apa yang kau lakukan di sini? Aku tidak tahu apa maksudmu. Ini akhir dari itu.",
    "ru": "Что ты здесь делаешь? Я не знаю, что ты имеешь в виду. Это конец.",
    "he": "מה אתה עושה כאן? אני לא יודע מה אתה מתכוון. זה הסוף של זה.",
    "ar": "ماذا تفعل هنا؟ أنا لا أعرف ماذا تقصد. هذا هو النهاية.",
}


def srt(lines):
    return "\n\n".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{text}" for i, text in enumerate(lines, 1)) + "\n"


class Detection(unittest.TestCase):
    def test_each_language_is_recognised_as_itself_only(self):
        for code, text in SAMPLES.items():
            accepted = [c for c in SAMPLES if language.text_check(text, c)[0]]
            self.assertEqual(accepted, [code], code)

    def test_codes_names_and_our_own_track(self):
        with mock.patch.dict(os.environ, EN):
            self.assertTrue(language.is_code("eng") and language.is_code("en-US") and language.is_code("English"))
            self.assertFalse(language.is_code("ara"))
            self.assertEqual((language.own_name(), language.iso2()), ("English (Kizuna)", "eng"))
            self.assertTrue(language.is_own("English (Kizuna)") and language.is_own("العربية"))   # old Arabic tracks too
            self.assertFalse(language.is_own("English"))                    # a release's own track is never ours
        self.assertEqual(language.own_name("ar"), "العربية")

    def test_dub_and_text_track_in_the_subtitle_language(self):
        tracks = [{"id": 1, "type": "audio", "properties": {"language": "jpn"}},
                  {"id": 2, "type": "audio", "properties": {"language": "eng", "track_name": "Commentary"}},
                  {"id": 3, "type": "subtitles", "codec": "SubRip/SRT", "properties": {"language": "eng", "track_name": "English"}},
                  {"id": 4, "type": "subtitles", "codec": "SubRip/SRT", "properties": {"language": "ara"}}]
        with mock.patch.dict(os.environ, EN), mock.patch.object(agent, "tracks", return_value=tracks):
            self.assertFalse(agent.has_target_audio(tracks))               # commentary is not a dub
            self.assertTrue(agent.has_target_audio(tracks + [{"type": "audio", "properties": {"language": "eng"}}]))
            self.assertEqual(agent.target_track("x.mkv"), 3)

    def test_burned_in_check_never_guesses_for_latin_letters(self):
        with mock.patch.dict(os.environ, EN), mock.patch("subprocess.run") as run:
            self.assertEqual(hardsub.burned_in_subtitles("x.mkv"), (False, 0, 0))
            run.assert_called_once()                                      # only the duration probe


class Conversion(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_english_has_no_rtl_marks_and_loses_credit_cues(self):
        (self.d / "a.srt").write_text(srt(["Subtitles by SomeGroup", "- Are you coming?", "Translated by Bob",
                                           "We have to wait."]), encoding="utf-8")
        with mock.patch.dict(os.environ, EN):
            n = srt2ass.convert(str(self.d / "a.srt"), str(self.d / "a.ass"), "T", style=st.default_for("en"))
        text = (self.d / "a.ass").read_text(encoding="utf-8-sig")
        self.assertEqual(n, 2)
        self.assertNotIn(language.RTL_MARK, text)
        self.assertIn("Style: Default,Noto Sans,70,", text)
        self.assertIn(",- Are you coming?", text)

    def test_arabic_keeps_its_right_to_left_wrapping(self):
        (self.d / "a.srt").write_text(srt(["ترجمة فلان", "- هل ستأتي؟"]), encoding="utf-8")
        srt2ass.convert(str(self.d / "a.srt"), str(self.d / "a.ass"), "T", lang="ar")
        text = (self.d / "a.ass").read_text(encoding="utf-8-sig")
        self.assertEqual(text.count("Dialogue:"), 1)
        self.assertIn(language.RTL_MARK + "- هل ستأتي؟" + language.RTL_MARK, text)

    def test_quality_check_catches_a_credit_left_in_english(self):
        ass = self.d / "a.ass"
        ass.write_text(srt2ass.head("T", st.default_for("en")) + "".join(
            f"Dialogue: 0,0:00:{i // 60:02d}.{i % 60:02d},0:00:{i // 60:02d}.{i % 60:02d},Default,,0,0,0,,Line {i}\n"
            for i in range(120)) + "Dialogue: 0,0:01:00.00,0:01:01.00,Default,,0,0,0,,Subtitles by Bob\n", encoding="utf-8-sig")
        with mock.patch.dict(os.environ, EN):
            self.assertEqual(agent.qc(str(ass)), ["credit line left"])


class Picker(unittest.TestCase):
    def test_only_subtitles_in_the_language_are_ranked_and_fan_groups_first(self):
        subs = [{"id": "a|Netflix", "lang": "eng", "url": "u1"}, {"id": "b|Some Fansub", "lang": "en", "url": "u2"},
                {"id": "c|Arabic group", "lang": "ara", "url": "u3"}, {"id": "d|Machine translated", "lang": "eng", "url": "u4"}]
        with mock.patch.dict(os.environ, EN):
            self.assertEqual([n for _, n, _ in fansub.ranked(subs, "official_ok")], ["Some Fansub", "Netflix"])

    def test_text_check_rejects_a_mislabelled_file(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "s.srt").write_text(srt([SAMPLES["es"]] * 120), encoding="utf-8")
            with mock.patch.dict(os.environ, EN):
                ok, why = fansub.text_ok(str(d / "s.srt"))
            self.assertFalse(ok)
            self.assertIn("Spanish", why)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Saved(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.p = [mock.patch.object(settings, "FILE", self.d / "settings.json"),
                  mock.patch.object(shows, "RESETS", self.d / "shows-pending.json"),
                  mock.patch.dict(os.environ, {"KIZUNA_SUBTITLE_LANGUAGE": ""})]
        for p in self.p:
            p.start()
        settings._cache.update(stamp="reset", data=dict(settings.DEFAULTS), damaged=False)

    def tearDown(self):
        for p in self.p:
            p.stop()
        settings._cache.update(stamp="reset", data=dict(settings.DEFAULTS), damaged=False)
        shutil.rmtree(self.d, ignore_errors=True)

    def test_new_installs_are_english_and_the_choice_is_saved(self):
        self.assertEqual(language.code(), "en")
        self.assertEqual(settings.get("sub_style_en")["font"], "Noto Sans")
        settings.update({"subtitle_language": "es", "folder_inbox": str(self.d / "in")})
        self.assertEqual((language.code(), settings.get("folder_inbox")), ("es", str(self.d / "in")))
        with self.assertRaises(settings.SettingsError):
            settings.update({"subtitle_language": "xx"})
        with self.assertRaises(settings.SettingsError):
            settings.update({"folder_inbox": "relative\\path"})


class ReviewFixes(unittest.TestCase):
    """Cases the independent review of the worldwide change found (2026-11-18)."""
    AR = {"KIZUNA_SUBTITLE_LANGUAGE": "ar"}

    def test_arabic_burned_in_check_ignores_digits_and_punctuation(self):
        with mock.patch.dict(os.environ, self.AR):
            self.assertEqual(hardsub.dialogue_words("٢٠٢٤ ١٢:٣٠ ؟؟ ،،"), [])
            self.assertEqual(hardsub.dialogue_words("هل ستأتي الليلة"), ["هل", "ستأتي", "الليلة"])

    def test_arabic_addon_labels_match_as_before(self):
        subs = [{"id": f"x|{n}", "lang": n, "url": n} for n in ("Arabic (Egypt)", "arb", "ar-SA", "eng")]
        with mock.patch.dict(os.environ, self.AR):
            self.assertEqual(sorted(n for _, n, _ in fansub.ranked(subs, "official_ok")), ["Arabic (Egypt)", "ar-SA", "arb"])

    def test_damaged_settings_never_switch_the_language(self):
        with mock.patch.dict(os.environ, {"KIZUNA_SUBTITLE_LANGUAGE": ""}), mock.patch.object(settings, "damaged", return_value=True):
            with self.assertRaises(language.SettingsUnreadable):
                language.require()
            with mock.patch.object(agent, "lock_held", return_value=False), mock.patch.object(agent, "load_state") as load, \
                    mock.patch.object(agent, "log"), mock.patch("builtins.open", mock.mock_open()), mock.patch("os.remove"), \
                    mock.patch("os.makedirs"):
                agent.main()
            load.assert_not_called()                                    # nothing processed, nothing marked failed
        with mock.patch.dict(os.environ, self.AR), mock.patch.object(settings, "damaged", return_value=True):
            self.assertEqual(language.require(), "ar")                  # an explicit override still works

    def test_hand_edited_folders_are_validated(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "settings.json").write_text(json.dumps({"folder_inbox": "C:\\ok\\in", "folder_weekly": "rel\\path",
                                                         "folder_anime": "C:\\bad|x"}), encoding="utf-8")
            with mock.patch.object(agent, "HERE", str(d)):
                self.assertEqual(agent.folder_setting("folder_inbox"), "C:\\ok\\in")
                self.assertEqual(agent.folder_setting("folder_weekly"), agent.FOLDER_DEFAULTS["folder_weekly"])
                self.assertEqual(agent.folder_setting("folder_anime"), agent.FOLDER_DEFAULTS["folder_anime"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_new_install_serves_the_pages_before_waiting_for_the_inbox(self):
        import status
        import watcher
        started = []

        class Stop(Exception):
            pass

        def isdir(path):
            raise Stop                                                  # the inbox wait: pages must already be up
        with mock.patch.object(watcher, "single_instance"), mock.patch.object(status, "serve") as serve, \
                mock.patch("threading.Thread", side_effect=lambda target=None, **k: started.append(target) or mock.Mock()), \
                mock.patch("os.path.isdir", side_effect=isdir), self.assertRaises(Stop):
            watcher.main()
        self.assertIn(serve, started)

    def test_wizard_folder_change_restarts_kizuna(self):
        import cleanup
        import setup
        d = Path(tempfile.mkdtemp())
        folders = {k: str(d / k) for k in settings.FOLDERS}
        try:
            with mock.patch.object(settings, "get", return_value=folders), mock.patch.object(setup, "check", return_value={}), \
                    mock.patch.object(agent, "FOLDER", folders["folder_inbox"]), mock.patch.object(agent, "LIBRARY", "C:\\old"), \
                    mock.patch.object(cleanup, "ANIME", folders["folder_anime"]), mock.patch("threading.Timer") as timer, \
                    mock.patch.object(agent, "log"), mock.patch("sys.argv", ["watcher.py", "--child"]):
                self.assertTrue(setup.make_folders()["restarting"])
            timer.assert_called_once()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class RealTools(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_english_mux_writes_our_track_and_the_font(self):
        video = make_video(self.d / "Show - S01E01.mkv", seconds=8, rate=2)
        (self.d / "a.srt").write_text(srt(["Hello there.", "Where are you going?"]), encoding="utf-8")
        with mock.patch.dict(os.environ, EN):
            srt2ass.convert(str(self.d / "a.srt"), str(self.d / "a.ass"), "T", style=st.default_for("en"))
            mux.mux(video, str(self.d / "a.ass"))
            info = mux.info(video)
            ours = [t for t in info["tracks"] if t["type"] == "subtitles"]
            self.assertEqual([(t["properties"]["track_name"], t["properties"]["language"], t["properties"]["default_track"])
                              for t in ours], [("English (Kizuna)", "eng", True)])
            self.assertEqual({a["file_name"] for a in info["attachments"]}, {"NotoSans-Regular.ttf", "NotoSans-Bold.ttf"})
            self.assertTrue(any(language.is_own(t["properties"].get("track_name")) for t in info["tracks"]))

    def test_every_language_previews_with_its_own_font(self):
        images = set()
        for code in language.TABLE:
            img = st.preview(st.default_for(code), lang=code)
            self.assertTrue(base64.b64decode(img.split(",", 1)[1]).startswith(b"\x89PNG"), code)
            images.add(img)
        self.assertEqual(len(images), len(language.TABLE))


if __name__ == "__main__":
    unittest.main()
