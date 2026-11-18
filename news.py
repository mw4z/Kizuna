"""Anime and movie / TV news from public RSS feeds, for the News page.

Feeds are re-read in the background every few minutes (the page asks every minute), so the page never waits on a slow
site. Everything in a feed is untrusted: titles and summaries become plain text, links must be http(s), images https,
and each feed is size-limited; one broken feed never hides the others."""
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from datetime import datetime
import html
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FEEDS = [  # (source, kind, url, language)   kind: "anime" | "screen" (movies & series) | "mixed" (sorted by category)
    ("Crunchyroll News", "anime", "https://cr-news-api-service.prd.crunchyrollsvc.com/v1/en-US/rss", "en"),
    ("Anime News Network", "anime", "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=w", "en"),
    ("Anime Corner", "anime", "https://animecorner.me/feed/", "en"),
    ("MyAnimeList", "anime", "https://myanimelist.net/rss/news.xml", "en"),
    ("Deadline", "screen", "https://deadline.com/feed/", "en"),
    ("Variety", "screen", "https://variety.com/feed/", "en"),
    ("Collider", "screen", "https://collider.com/feed/", "en"),
    # Arabic: IGN Arabia (games news is dropped; only its film, TV and anime stories) and Youm7's art section
    ("IGN عربي", "mixed", "https://me.ign.com/ar/feed.xml", "ar"),
    ("اليوم السابع - فن", "screen", "https://www.youm7.com/rss/SectionRss?SectionID=48", "ar"),
]
MIXED_ANIME = {"anime", "انمي", "أنمي", "الأنمي", "الانمي"}
MIXED_SCREEN = {"entertainment", "افلام", "أفلام", "tv", "مسلسلات", "movies", "تلفزيون"}
SKIP_CATEGORIES = {"games", "game", "video games", "podcast", "music"}   # off-topic sections of mixed feeds
REFRESH = 180            # seconds between background refreshes
MAX_BYTES = 4 * 1024 * 1024
KEEP = 160
PER_SOURCE = 25
MEDIA = "{http://search.yahoo.com/mrss/}"
_lock = threading.Lock()
_state = {"items": [], "updated": 0, "failed": [], "refreshing": False}


def _text(value, limit):
    """Plain text from feed HTML/entities, collapsed and cut at a word boundary."""
    value = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(value or "")))
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        value = value[:limit].rsplit(" ", 1)[0].rstrip(".,;:–—- ") + "…"
    return value


def _url(value, schemes=("http", "https")):
    value = (value or "").strip()
    p = urllib.parse.urlsplit(value)
    return value if p.scheme in schemes and p.hostname and not p.username and len(value) < 600 else ""


def _image(item):
    for tag in (MEDIA + "thumbnail", MEDIA + "content"):
        for el in item.iter(tag):
            url = _url(el.get("url"), ("https",))
            if url and el.get("medium", "image") == "image":
                return url
    enc = item.find("enclosure")
    if enc is not None and str(enc.get("type", "")).startswith("image/"):
        return _url(enc.get("url"), ("https",))
    return ""


def _when(item):
    raw = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError, IndexError, OSError, OverflowError):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (ValueError, OSError, OverflowError):
            return 0


def parse(data, source, kind, language="en"):
    root = ET.fromstring(data)
    out = []
    for item in root.iter("item"):
        cats = {(c.text or "").strip().lower() for c in item.findall("category")}
        if cats & SKIP_CATEGORIES:
            continue
        this = kind
        if kind == "mixed":                       # a site that also covers games: keep only film, TV and anime
            this = "anime" if cats & MIXED_ANIME else "screen" if cats & MIXED_SCREEN else None
            if not this:
                continue
        title, link = _text(item.findtext("title"), 220), _url(item.findtext("link"))
        if not title or not link:
            continue
        out.append({"title": title, "link": link, "source": source, "kind": this, "lang": language, "at": _when(item),
                    "summary": _text(item.findtext("description"), 260), "image": _image(item)})
    return out


def fetch(source, kind, url, language="en"):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Kizuna news reader)",
                                               "Accept": "application/rss+xml, application/xml;q=0.9"})
    deadline = time.monotonic() + 30          # whole feed, not per read: a server trickling bytes can't hold us
    with urllib.request.urlopen(req, timeout=15) as r:
        chunks, size = [], 0
        while size <= MAX_BYTES:
            if time.monotonic() > deadline:
                raise TimeoutError("feed too slow")
            chunk = r.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b"".join(chunks)
    if len(data) > MAX_BYTES:
        raise ValueError("feed too large")
    return parse(data, source, kind, language)


def refresh():
    items, failed = [], []
    pool = ThreadPoolExecutor(max_workers=len(FEEDS))
    try:
        futures = {pool.submit(fetch, *feed): feed[0] for feed in FEEDS}
        for future, source in futures.items():
            try:
                items += future.result(timeout=40)
            except Exception:
                failed.append(source)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # never wait on a stuck feed
    seen, unique, per_source = set(), [], {}
    for item in sorted(items, key=lambda i: i["at"], reverse=True):
        key = item["link"].split("?")[0].rstrip("/")
        if key in seen or per_source.get(item["source"], 0) >= PER_SOURCE:   # one busy site must not drown the rest
            continue
        seen.add(key)
        per_source[item["source"]] = per_source.get(item["source"], 0) + 1
        unique.append(item)
    with _lock:
        if unique or not _state["items"]:      # all feeds down: keep showing the last good list
            _state["items"] = unique[:KEEP]
        _state.update(updated=time.time(), failed=failed, refreshing=False)


def latest(kind="all", language="en"):
    """Cached stories (newest first; with language="ar", Arabic stories first, then the rest marked by their own
    language). The first call fetches; later calls start a background refresh when stale."""
    with _lock:
        empty, stale = not _state["items"], time.time() - _state["updated"] > REFRESH
        start = stale and not _state["refreshing"]
        if start:
            _state["refreshing"] = True          # one refresh at a time, also while the list is still empty
    if start and empty and not _state["updated"]:
        refresh()                                # the very first call waits once (a few seconds)
    elif start:
        threading.Thread(target=refresh, daemon=True).start()
    with _lock:
        items = [i for i in _state["items"] if kind == "all" or i["kind"] == kind]
        if language != "en":
            items = [i for i in items if i.get("lang") == language] + [i for i in items if i.get("lang") != language]
        return {"items": items, "updated": _state["updated"], "failed": list(_state["failed"]),
                "sources": [{"name": n, "kind": k, "lang": lg} for n, k, _, lg in FEEDS]}
