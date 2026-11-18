"""News page: feed parsing treats everything as untrusted; one broken feed never hides the others."""
import time
import unittest
from unittest import mock

import news

RSS = b"""<?xml version="1.0"?><rss xmlns:media="http://search.yahoo.com/mrss/"><channel>
<item><title>Big &lt;b&gt;premiere&lt;/b&gt; &amp;amp; more</title><link>https://example.com/a?x=1</link>
 <pubDate>Tue, 17 Nov 2026 18:00:00 -0500</pubDate><description>&lt;p&gt;Hello &lt;script&gt;alert(1)&lt;/script&gt;world&lt;/p&gt;</description>
 <media:thumbnail url="https://img.example.com/a.jpg"/></item>
<item><title>Insecure image</title><link>https://example.com/b</link><pubDate>Tue, 17 Nov 2026 17:00:00 -0500</pubDate>
 <enclosure url="http://img.example.com/b.jpg" type="image/jpeg"/></item>
<item><title>Scripted link</title><link>javascript:alert(1)</link></item>
<item><title>A game story</title><link>https://example.com/g</link><category>Games</category></item>
<item><title></title><link>https://example.com/empty</link></item>
</channel></rss>"""


class Parse(unittest.TestCase):
    def test_untrusted_feed_content_becomes_safe_plain_data(self):
        items = news.parse(RSS, "Example", "anime")
        self.assertEqual([i["title"] for i in items], ["Big premiere & more", "Insecure image"])
        a, b = items
        self.assertEqual(a["summary"], "Hello alert(1) world")                 # tags gone, text only
        self.assertEqual(a["image"], "https://img.example.com/a.jpg")
        self.assertEqual(b["image"], "")                                        # https images only
        self.assertGreater(a["at"], b["at"])
        self.assertEqual((a["source"], a["kind"]), ("Example", "anime"))

    def test_long_titles_are_cut_at_a_word(self):
        long = "<rss><channel><item><title>" + "word " * 100 + "</title><link>https://e.com/x</link></item></channel></rss>"
        title = news.parse(long.encode(), "S", "screen")[0]["title"]
        self.assertLessEqual(len(title), 221)
        self.assertTrue(title.endswith("…"))


class Refresh(unittest.TestCase):
    def setUp(self):
        self.saved = dict(news._state)
        news._state.update(items=[], updated=0, failed=[], refreshing=False)

    def tearDown(self):
        news._state.clear()
        news._state.update(self.saved)

    def item(self, source, n, kind="anime"):
        return {"title": f"{source} {n}", "link": f"https://{source}.example/{n}?utm=1", "source": source, "kind": kind,
                "at": 1000 + n, "summary": "", "image": ""}

    def test_one_broken_feed_never_hides_the_others_and_busy_sites_are_capped(self):
        def fetch(source, kind, url):
            if source == "Deadline":
                raise OSError("down")
            return [self.item(source, n, kind) for n in range(40)]
        with mock.patch.object(news, "fetch", side_effect=fetch):
            d = news.latest()
        self.assertEqual(d["failed"], ["Deadline"])
        counts = {}
        for i in d["items"]:
            counts[i["source"]] = counts.get(i["source"], 0) + 1
        self.assertTrue(all(c <= news.PER_SOURCE for c in counts.values()))
        self.assertNotIn("Deadline", counts)
        self.assertEqual(d["items"], sorted(d["items"], key=lambda i: -i["at"]))

    def test_all_feeds_down_keeps_the_last_good_list(self):
        news._state.update(items=[self.item("X", 1)], updated=0)
        with mock.patch.object(news, "fetch", side_effect=OSError("offline")):
            news.refresh()
        self.assertEqual(len(news._state["items"]), 1)

    def test_duplicates_across_feeds_shown_once_and_sections_filter(self):
        with mock.patch.object(news, "fetch", side_effect=lambda s, k, u: [dict(self.item("same", 1, k), source=s)]):
            d = news.latest()
        self.assertEqual(len(d["items"]), 1)
        with mock.patch.object(news, "fetch", side_effect=lambda s, k, u: [self.item(s.replace(" ", ""), 1, k)]):
            news._state.update(items=[], updated=0)
            self.assertTrue(all(i["kind"] == "screen" for i in news.latest("screen")["items"]))

    def test_stale_list_is_refreshed_in_the_background_without_waiting(self):
        news._state.update(items=[self.item("X", 1)], updated=time.time() - news.REFRESH - 5)
        started = []
        with mock.patch.object(news.threading, "Thread", side_effect=lambda **k: started.append(k) or mock.Mock()):
            d = news.latest()
        self.assertEqual(len(d["items"]), 1)
        self.assertEqual(len(started), 1)


if __name__ == "__main__":
    unittest.main()
