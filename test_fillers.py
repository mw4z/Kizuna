import os
os.environ.setdefault("KIZUNA_SUBTITLE_LANGUAGE", "ar")   # this suite tests the Arabic pipeline
import unittest
from unittest.mock import patch
import fillers
import stremio as s


class Tests(unittest.TestCase):
    def test_guide_aliases_do_not_confuse_remakes(self):
        self.assertIn("attackontitan", fillers.guide_names("Attack on Titan (Shingeki no Kyojin)"))
        self.assertNotIn("berserk", fillers.guide_names("Berserk (2016)"))

    def test_guide_parser_and_unambiguous_air_dates(self):
        page = fillers.Guide()
        page.feed('<a href="/shows/example-show">Example Show</a><table>'
                  '<tr><td>1</td><td>Episode</td><td><span>Filler</span></td><td>2020-01-01</td></tr>'
                  '<tr><td>2</td><td>Episode</td><td>Mixed Canon/Filler</td><td>2020-01-08</td></tr>'
                  '<tr><td>3</td><td>Episode</td><td>Manga Canon</td><td>2020-01-15</td></tr></table>')
        self.assertEqual(page.links, [("Example Show", "/shows/example-show")])
        videos = [{"id": str(i), "season": 11, "released": date + "T00:00:00Z"} for i, date in enumerate(
            ("2020-01-01", "2020-01-08", "2020-01-15", "2020-01-22"))]
        self.assertEqual(fillers.classify(page.rows, videos), {"0": "Filler", "1": "Mixed canon/filler", "2": "Canon"})
        videos.append(dict(videos[0], id="duplicate-date"))
        self.assertNotIn("0", fillers.classify(page.rows, videos))
        self.assertNotIn("duplicate-date", fillers.classify(page.rows, videos))
        self.assertEqual(fillers.classify(page.rows, [dict(videos[1], season=0)]), {})
        shifted = [{"id": "shifted", "season": 11, "released": "2020-01-02"}]
        self.assertEqual(fillers.classify(page.rows, shifted), {"shifted": "Filler"})
        self.assertEqual(fillers.classify(page.rows + [["9", "Episode", "Filler", "2020-01-03"]], shifted), {})

    def test_lookup_is_generic_and_does_not_guess_unknown_shows(self):
        index = fillers.Guide()
        index.links = [("Example Show", "/shows/example-show")]
        guide = fillers.Guide()
        guide.rows = [["1", "Episode", "Filler", "2020-01-01"]]
        meta = {"id": "custom-id", "name": "Example Show", "videos": [
            {"id": "ep1", "season": 1, "released": "2020-01-01"}]}
        with patch.object(s, "item", return_value={"meta": meta}), patch.object(fillers, "page", side_effect=[index, guide]):
            self.assertEqual(fillers.lookup("key")["labels"], {"ep1": "Filler"})
        meta["name"] = "Unknown Show"
        with patch.object(s, "item", return_value={"meta": meta}), patch.object(fillers, "page", return_value=index):
            self.assertEqual(fillers.lookup("key")["labels"], {})


if __name__ == "__main__":
    unittest.main()
