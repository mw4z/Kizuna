"""Subtitle style (Settings > Arabic subtitles): validation, the ASS it writes, the fonts embedded with it (real
mkvmerge) and the libass preview (real ffmpeg)."""
import base64
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import mux
import settings
import shows
import srt2ass
import subtitle_style as st
from test_reliability import make_video

HOUSE = "Style: Default,Cairo,82,&H00FFFFFF,&H000000FF,&H002F2C2B,&H9E000000,-1,0,0,0,95,100,0,0,1,2.6,1.2,2,180,180,42,1"
SRT = "1\n00:00:01,000 --> 00:00:03,000\nمرحبا\n\n2\n00:00:04,000 --> 00:00:06,000\nكيف حالك؟\n"


class Style(unittest.TestCase):
    def test_default_is_exactly_the_old_house_style(self):
        self.assertEqual(st.style_line(st.DEFAULT), HOUSE)

    def test_every_option_reaches_the_ass_style(self):
        line = st.style_line(dict(st.DEFAULT, font="Tajawal", size=70, bold=False, color="#FFEE00", position="top",
                                  margin=60, shadow=0, shadow_opacity=100, shadow_color="#112233"))
        self.assertEqual(line, "Style: Default,Tajawal,70,&H0000EEFF,&H000000FF,&H002F2C2B,&H00332211,0,0,0,0,95,100,0,0,"
                               "1,2.6,0,8,180,180,60,1")
        box = st.style_line(dict(st.DEFAULT, box=True, box_color="#102030", box_opacity=50, box_padding=12))
        self.assertIn(",&H80302010,", box)                                 # box colour, half transparent
        self.assertIn(",3,12,1.2,2,", box)                                  # opaque box, padding 12

    def test_bad_values_are_refused_with_a_reason(self):
        for bad, why in (({"font": "Comic Sans"}, "font"), ({"color": "red"}, "#RRGGBB"), ({"size": 500}, "between"),
                         ({"position": "middle"}, "bottom or top"), ({"outline": "thick"}, "numbers")):
            with self.assertRaisesRegex(ValueError, why):
                st.clean(bad)
        self.assertEqual(st.clean({"unknown": 1, "size": "90"})["size"], 90)

    def test_conversion_uses_the_given_or_saved_style(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "a.srt").write_text(SRT, encoding="utf-8")
            srt2ass.convert(str(d / "a.srt"), str(d / "a.ass"), "T", style=dict(st.DEFAULT, font="Amiri"))
            text = (d / "a.ass").read_text(encoding="utf-8-sig")
            self.assertIn("Style: Default,Amiri,", text)
            self.assertEqual(text.count("\nDialogue:"), 2)
            self.assertEqual([Path(f).name for f in st.font_files(str(d / "a.ass"))], ["Amiri-Regular.ttf", "Amiri-Bold.ttf"])
            self.assertEqual([Path(f).name for f in st.font_files(str(d / "missing.ass"))], list(st.FONTS["Cairo"]))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_every_listed_font_file_is_shipped(self):
        for family, files in st.FONTS.items():
            for f in files:
                self.assertTrue(Path(st.FONT_DIR, f).stat().st_size > 10000, f)


class Saved(unittest.TestCase):
    def setUp(self):                                    # private settings file, like test_settings.Prefs
        self.d = Path(tempfile.mkdtemp())
        self.p = mock.patch.object(settings, "FILE", self.d / "settings.json")
        self.r = mock.patch.object(shows, "RESETS", self.d / "shows-pending.json")
        self.p.start()
        self.r.start()
        settings._cache.update(stamp="reset", data=dict(settings.DEFAULTS), damaged=False)

    def tearDown(self):
        self.p.stop()
        self.r.stop()
        settings._cache.update(stamp="reset", data=dict(settings.DEFAULTS), damaged=False)
        shutil.rmtree(self.d, ignore_errors=True)

    def test_style_saves_and_a_damaged_one_keeps_the_last_good(self):
        mine = dict(st.DEFAULT, font="Almarai", box=True)
        settings.update({"sub_style_ar": json.dumps(mine)})
        self.assertEqual(settings.get("sub_style_ar"), mine)
        self.assertEqual(st.current("ar"), mine)                            # what new subtitles are built with
        with self.assertRaises(settings.SettingsError):
            settings.update({"sub_style_ar": json.dumps({"font": "Nope"})})
        raw = json.loads(settings.FILE.read_text(encoding="utf-8"))
        raw["sub_style_ar"] = {"size": "huge"}
        settings.FILE.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(settings.get("sub_style_ar"), mine)


class RealTools(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_preview_is_drawn_by_libass_and_follows_the_style(self):
        a = st.preview(st.DEFAULT)
        b = st.preview(dict(st.DEFAULT, box=True, color="#FFEE00"))
        c = st.preview(st.DEFAULT, bright=True)
        for img in (a, b, c):
            self.assertTrue(base64.b64decode(img.split(",", 1)[1]).startswith(b"\x89PNG"))
        self.assertEqual(len({a, b, c}), 3)

    def test_mux_embeds_the_chosen_font(self):
        video = make_video(self.d / "Show - S01E01.mkv", seconds=8, rate=2)   # longer than the subtitles
        (self.d / "a.srt").write_text(SRT, encoding="utf-8")
        srt2ass.convert(str(self.d / "a.srt"), str(self.d / "a.ass"), "T", style=dict(st.DEFAULT, font="Tajawal"))
        mux.mux(video, str(self.d / "a.ass"))
        names = {a["file_name"] for a in mux.info(video).get("attachments", [])}
        self.assertEqual(names, {"Tajawal-Regular.ttf", "Tajawal-Bold.ttf"})


if __name__ == "__main__":
    unittest.main()
