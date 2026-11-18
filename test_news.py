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


class Arabic(unittest.TestCase):
    MIXED = """<rss><channel>
<item><title>لعبة جديدة</title><link>https://me.ign.com/ar/g</link><category>أخبار</category><category>PlayStation 5</category></item>
<item><title>مراجعة فيلم</title><link>https://me.ign.com/ar/f</link><category>Entertainment</category><category>افلام</category></item>
<item><title>موسم جديد من الأنمي</title><link>https://me.ign.com/ar/a</link><category>أنمي</category></item>
</channel></rss>""".encode("utf-8")

    def test_mixed_site_keeps_only_film_tv_and_anime(self):
        items = news.parse(self.MIXED, "IGN عربي", "mixed", "ar")
        self.assertEqual([(i["title"], i["kind"], i["lang"]) for i in items],
                         [("مراجعة فيلم", "screen", "ar"), ("موسم جديد من الأنمي", "anime", "ar")])

    def test_arabic_interface_puts_arabic_stories_first(self):
        saved = dict(news._state)
        try:
            news._state.update(items=[{"title": "en", "link": "https://e/1", "kind": "anime", "lang": "en", "at": 9},
                                      {"title": "ar", "link": "https://e/2", "kind": "anime", "lang": "ar", "at": 1}],
                               updated=time.time(), refreshing=False, failed=[])
            self.assertEqual([i["title"] for i in news.latest("all", "ar")["items"]], ["ar", "en"])
            self.assertEqual([i["title"] for i in news.latest("all", "en")["items"]], ["en", "ar"])
        finally:
            news._state.clear()
            news._state.update(saved)


class Slow(unittest.TestCase):
    def test_a_trickling_feed_hits_its_deadline(self):
        class Trickle:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, n): return b" "
        clock = iter(range(0, 1000, 5))
        with mock.patch.object(news.urllib.request, "urlopen", return_value=Trickle()), \
                mock.patch.object(news.time, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(TimeoutError):
                news.fetch("Slow", "anime", "https://slow.example/feed")

    def test_odd_dates_do_not_break_a_feed(self):
        rss = b"<rss><channel><item><title>Old</title><link>https://e.com/o</link><pubDate>Mon, 01 Jan 1900 00:00:00</pubDate></item></channel></rss>"
        self.assertEqual(len(news.parse(rss, "S", "anime")), 1)


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
        def fetch(source, kind, url, language="en"):
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
        with mock.patch.object(news, "fetch", side_effect=lambda s, k, u, lg: [dict(self.item("same", 1, k), source=s)]):
            d = news.latest()
        self.assertEqual(len(d["items"]), 1)
        with mock.patch.object(news, "fetch", side_effect=lambda s, k, u, lg: [self.item(s.replace(" ", ""), 1, k)]):
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
