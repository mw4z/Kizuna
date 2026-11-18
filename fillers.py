"""Optional AnimeFillerList labels, matched by exact show name and unique broadcast date."""
from collections import Counter
from datetime import date, timedelta
from html.parser import HTMLParser
import re
import time
import unicodedata
import urllib.request

_cache = {}


def normalized(text):
    text = unicodedata.normalize("NFKD", text).casefold()
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c))


def guide_names(title):
    names = {normalized(title)}
    if " (" in title and not re.search(r"\(\d{4}\)", title):
        names.add(normalized(title.split(" (", 1)[0]))
    return names


class Guide(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.rows = [], []
        self.href = None
        self.text = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href", "").startswith("/shows/"):
            self.href, self.text = attrs["href"], []
        if tag == "tr":
            self.row = []
        if tag == "td" and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            if re.fullmatch(r"/shows/[a-z0-9-]+", self.href):
                self.links.append(("".join(self.text).strip(), self.href))
            self.href = None
        if tag == "td" and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def page(path):
    import stremio as s
    cached = _cache.get(path)
    if cached and time.monotonic() - cached[0] < 86400:
        return cached[1]
    url = "https://www.animefillerlist.com" + path
    s.safe_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), s.egress.AlwaysProxy(), s.Redirects())
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "MyAnimeWatcher/1.0"}), timeout=12) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Guide too large")
    parsed = Guide()
    parsed.feed(raw.decode("utf-8"))
    if len(_cache) >= 128:
        _cache.clear()
    _cache[path] = (time.monotonic(), parsed)
    return parsed


def classify(rows, videos):
    guides = {}
    for row in rows:
        if len(row) < 4 or not row[0].isdigit():
            continue
        kind = row[2].casefold()
        label = "Filler" if kind == "filler" else "Mixed canon/filler" if "mixed" in kind and "filler" in kind else "Canon" if "canon" in kind else None
        broadcast_day = row[3][:10]
        if label and re.fullmatch(r"\d{4}-\d{2}-\d{2}", broadcast_day):
            guides.setdefault(broadcast_day, []).append(label)
    matches = {}
    for video in videos:
        if video.get("season", 0) <= 0:
            continue
        try:
            aired = date.fromisoformat(str(video.get("released", ""))[:10])
        except ValueError:
            continue
        # Catalogs can use a different broadcast timezone; ambiguous dates never get a label.
        candidates = [(aired + timedelta(days=n)).isoformat() for n in (-1, 0, 1)
                      if (aired + timedelta(days=n)).isoformat() in guides]
        if len(candidates) == 1 and len(guides[candidates[0]]) == 1:
            matches[str(video["id"])] = candidates[0]
    counts = Counter(matches.values())
    return {key: guides[day][0] for key, day in matches.items() if counts[day] == 1}


def schedule_labels(names, schedule):
    """Weekly episodes: {episode number: "Filler" | "Mixed canon/filler" | "Canon"} for one show, from AniList air
    times ({episode: unix time}) matched to AnimeFillerList by unique air date (±1 day). Unknown shows -> {}."""
    wanted = set()
    for name in names:
        if name:
            wanted |= guide_names(name)
    wanted |= {{"caseclosed": "detectiveconan"}.get(w, w) for w in wanted}
    matches = {path for title, path in page("/shows").links if guide_names(title) & wanted}
    if len(matches) != 1:
        return {}
    from datetime import datetime, timezone
    videos = [{"id": str(ep), "season": 1, "released": datetime.fromtimestamp(at, timezone.utc).date().isoformat()}
              for ep, at in schedule.items()]
    return {int(ep): label for ep, label in classify(page(matches.pop()).rows, videos).items()}


def lookup(key):
    import stremio as s
    selected = s.item(key, "title")
    meta = selected["meta"]
    try:
        canonical = meta
        if re.fullmatch(r"tt\d+", str(meta.get("id", ""))):
            result = s.get_json("https://v3-cinemeta.strem.io/meta/series/" + meta["id"] + ".json", timeout=12)
            canonical = result.get("meta") or meta
        name = normalized(canonical.get("name", ""))
        name = {"caseclosed": "detectiveconan"}.get(name, name)
        matches = {path for title, path in page("/shows").links if name in guide_names(title)}
        if len(matches) != 1:
            return {"labels": {}, "source": "", "message": "Filler status unavailable for this show."}
        path = matches.pop()
        canonical_dates = {v["id"]: v.get("released", "") for v in canonical.get("videos", []) if v.get("id")}
        videos = [dict(v, released=canonical_dates.get(v.get("id")) or v.get("released", "")) for v in meta.get("videos", [])]
        labels = classify(page(path).rows, videos)
        return {"labels": labels, "source": "https://www.animefillerlist.com" + path,
                "message": "AnimeFillerList · unique air-date matches (±1 broadcast day). Unmatched episodes are unclassified."}
    except Exception:
        return {"labels": {}, "source": "", "message": "Filler guide unavailable. Episodes remain unclassified."}
