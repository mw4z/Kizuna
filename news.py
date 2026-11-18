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

FEEDS = [  # (source, kind, url)   kind: "anime" | "screen" (movies & series)
    ("Crunchyroll News", "anime", "https://cr-news-api-service.prd.crunchyrollsvc.com/v1/en-US/rss"),
    ("Anime News Network", "anime", "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=w"),
    ("Anime Corner", "anime", "https://animecorner.me/feed/"),
    ("MyAnimeList", "anime", "https://myanimelist.net/rss/news.xml"),
    ("Deadline", "screen", "https://deadline.com/feed/"),
    ("Variety", "screen", "https://variety.com/feed/"),
    ("Collider", "screen", "https://collider.com/feed/"),
]
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
    except (TypeError, ValueError, IndexError):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0


def parse(data, source, kind):
    root = ET.fromstring(data)
    out = []
    for item in root.iter("item"):
        cats = {(c.text or "").strip().lower() for c in item.findall("category")}
        if cats & SKIP_CATEGORIES:
            continue
        title, link = _text(item.findtext("title"), 220), _url(item.findtext("link"))
        if not title or not link:
            continue
        out.append({"title": title, "link": link, "source": source, "kind": kind, "at": _when(item),
                    "summary": _text(item.findtext("description"), 260), "image": _image(item)})
    return out


def fetch(source, kind, url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Kizuna news reader)",
                                               "Accept": "application/rss+xml, application/xml;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("feed too large")
    return parse(data, source, kind)


def refresh():
    items, failed = [], []
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        futures = {pool.submit(fetch, *feed): feed[0] for feed in FEEDS}
        for future, source in futures.items():
            try:
                items += future.result(timeout=40)
            except Exception:
                failed.append(source)
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


def latest(kind="all"):
    """Cached stories (newest first). The first call fetches; later calls start a background refresh when stale."""
    with _lock:
        empty, stale = not _state["items"], time.time() - _state["updated"] > REFRESH
        start = stale and not _state["refreshing"] and not empty
        if start:
            _state["refreshing"] = True
    if empty:
        refresh()
    elif start:
        threading.Thread(target=refresh, daemon=True).start()
    with _lock:
        items = [i for i in _state["items"] if kind == "all" or i["kind"] == kind]
        return {"items": items, "updated": _state["updated"], "failed": list(_state["failed"]),
                "sources": [{"name": n, "kind": k} for n, k, _ in FEEDS]}
