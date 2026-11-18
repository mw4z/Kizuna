"""Followed weekly shows: the private list (shows.json), AniList recommendations, DKB/Stremio linking, and the Stremio
source for new episodes.

Each new episode can come from DKB (dkb.py) or from the user's Stremio add-ons (stremio_poll below). Both check every
20 minutes; whichever has the episode first downloads it. claim() and have_episode() make sure the other source then
skips it, so an episode is never fetched twice.

shows.json (git-ignored, the user's own list):
  {"shows": [{"release": "<DKB/release name prefix>", "folder": "<Jellyfin folder, 'English Title (Year)'>",
              "season": <season number used in the library>, "anilist": <AniList id of the airing season>,
              "dkb": true|false, "policy": "official_ok"|"fansub_first"|"fansub"|"manual",
              "stremio": null | {"id": "<Stremio/IMDb id>", "type": "series"|"anime", "season": <Stremio season>,
                                 "offset": <library episode - Stremio episode>, "on": true|false},
              "from": <first library episode to fetch, or absent: from the next new one>}]}
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.request

import agent

FILE = Path(agent.HERE) / "shows.json"
STATE = Path(agent.HERE) / "weekly-state.json"       # Stremio side: baselines, finished and failed episodes
POLICIES = ("official_ok", "fansub_first", "fansub", "manual")
MAX_FAILS = 6                                       # Stremio attempts per episode before giving up on it
_lock = threading.RLock()
_cache = {"stamp": None, "shows": []}
_claims = {}                                        # "release|SxxEyy" -> source currently fetching it
wake_dkb, wake_stremio = threading.Event(), threading.Event()
_resets = {"dkb": set(), "stremio": set()}         # releases whose "following after" point must be recomputed


class ShowError(ValueError):
    """A message the page shows as-is."""


# ---------------------------------------------------------------- the list
def load():
    with _lock:
        try:
            stamp = FILE.stat().st_mtime_ns
        except FileNotFoundError:
            return []
        if stamp != _cache["stamp"]:
            try:
                data = json.loads(FILE.read_text(encoding="utf-8"))
                _cache.update(stamp=stamp, shows=[s for s in data.get("shows", [])
                                                  if isinstance(s, dict) and s.get("release") and s.get("folder")])
            except (OSError, ValueError, AttributeError):
                _cache["stamp"] = stamp                    # damaged by hand: keep the last good list, say so once
                agent.log("shows: shows.json could not be read; using the last good list")
        return [dict(s) for s in _cache["shows"]]


def save(items):
    with _lock:
        agent.write_atomic(FILE, json.dumps({"shows": items}, ensure_ascii=False, indent=1))
        _cache["stamp"] = None


def match(name):
    """The followed show a release/file name belongs to (longest release prefix wins), or None."""
    if not name:
        return None
    hits = [s for s in load() if re.match(re.escape(s["release"]) + r"(?:$|[ :._-])", name, re.I)]
    return max(hits, key=lambda s: len(s["release"]), default=None)


def by_title(name):
    """Show whose Jellyfin folder title (without the year) or release name is `name`."""
    key = re.sub(r"\s*\(\d{4}\)$", "", str(name or "")).casefold().strip()
    return next((s for s in load() if key and key in (re.sub(r"\s*\(\d{4}\)$", "", s["folder"]).casefold(),
                                                      s["release"].casefold())), None)


def folders():
    return {s["release"]: s["folder"] for s in load()}


def dkb_names():
    return tuple(s["release"] for s in load() if s.get("dkb"))


def policy_for(name):
    show = match(name) or by_title(name)
    return show.get("policy") if show and show.get("policy") in POLICIES else None


def tag(season, episode):
    return f"S{int(season):02d}E{int(episode):02d}"


# ---------------------------------------------------------------- one episode, one source
def claim(release, episode_tag, source):
    """Reserve an episode for one source; False when the other source is already fetching it."""
    key = f"{release}|{episode_tag}"
    with _lock:
        if _claims.get(key, source) != source:
            return False
        _claims[key] = source
        return True


RESETS = Path(agent.HERE) / "shows-pending.json"   # resets / renames not yet applied by a poller (survives restarts)


def _pending():
    """Pending requests; a missing file is empty, an unreadable one raises (never treated as empty and rewritten)."""
    try:
        data = json.loads(RESETS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("shows-pending.json is damaged")
    return data


def request_reset(release, renamed_to=None):
    """Ask both pollers to restart a show from its (new) starting point; with renamed_to, also carry its history
    (finished episodes) over to the new release name so nothing is fetched twice."""
    with _lock:
        _resets["dkb"].add(release)
        _resets["stremio"].add(release)
        data = _pending()
        target = renamed_to or release
        for source in ("dkb", "stremio"):
            pending = data.setdefault(source, {})
            for old in list(pending):              # A→B then B→C becomes A→C; renaming back collapses to A→A
                if pending[old] == release:
                    pending[old] = target
            pending.pop(release, None)
            pending[release] = target
        agent.write_atomic(RESETS, json.dumps(data, ensure_ascii=False))


def take_resets(source):
    """{old release name: current name} this poller must apply (memory + the persisted file). They stay recorded
    until the poller confirms with ack_resets() after saving its own state, so a crash in between loses nothing."""
    with _lock:
        try:
            out = dict(_pending().get(source, {}) or {})
        except (OSError, ValueError):
            out = {}                               # unreadable right now: use memory only, touch nothing
        out.update({r: out.get(r, r) for r in _resets[source]})
        return out


def ack_resets(source, applied):
    with _lock:
        _resets[source] -= set(applied)
        try:
            data = _pending()
        except (OSError, ValueError):
            return
        pending = data.get(source, {})
        for old, new in applied.items():
            if pending.get(old) == new:            # unchanged since it was taken
                pending.pop(old)
        if RESETS.exists():
            agent.write_atomic(RESETS, json.dumps(data, ensure_ascii=False))


def release_claim(release, episode_tag):
    with _lock:
        _claims.pop(f"{release}|{episode_tag}", None)


def have_episode(show, episode_tag):
    """True when the episode is already in the library, the inbox, on its way (Arabic preparation queue), or was
    fetched/skipped by either source."""
    import cleanup
    pattern = re.compile(re.escape(episode_tag) + r"(?!\d)", re.I)
    folder = Path(agent.LIBRARY) / show["folder"]
    for place in (folder, Path(cleanup.ANIME) / show["folder"]):
        if place.is_dir() and any(pattern.search(p.name) and p.suffix.lower() in (".mkv", ".mp4", ".m4v")
                                  for p in place.rglob("*")):
            return True
    try:
        for p in Path(agent.FOLDER).glob("*.mkv"):
            name, ep = agent.parse_name(p.name)
            if ep == episode_tag.upper() and match(name) and match(name)["release"] == show["release"]:
                return True
    except OSError:
        pass
    try:
        import library_processing
        with library_processing._guard:
            records = library_processing.read()
        root = os.path.normcase(str(folder)) + os.sep
        if any(r.get("status") != "done" and os.path.normcase(r.get("destination", "")).startswith(root)
               and pattern.search(os.path.basename(r.get("destination", ""))) for r in records.values()):
            return True
    except (OSError, ValueError):
        pass
    key = f"{show['release']}|{episode_tag.upper()}"
    for path in (Path(agent.HERE) / "dkb-state.json", STATE):
        try:
            if key in json.loads(path.read_text(encoding="utf-8")).get("done", {}):
                return True
        except (OSError, ValueError):
            pass
    return False


# ---------------------------------------------------------------- AniList
ANILIST_URL = "https://graphql.anilist.co"
FIELDS = ("id title{romaji english native} synonyms format season seasonYear episodes status "
          "nextAiringEpisode{episode airingAt} coverImage{large} bannerImage description(asHtml:false) genres "
          "startDate{year}")
_recommend = {"t": 0, "data": None}


def anilist(query, variables):
    req = urllib.request.Request(ANILIST_URL, data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "Kizuna"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read(4 * 1024 * 1024))
    if data.get("errors"):
        raise ShowError("AniList didn't answer. Try again in a minute.")
    return data["data"]


def current_season(now=None):
    d = datetime.fromtimestamp(now or time.time(), timezone.utc)
    if d.month == 12:
        return "WINTER", d.year + 1
    return ("WINTER", "WINTER", "SPRING", "SPRING", "SPRING", "SUMMER", "SUMMER", "SUMMER", "FALL", "FALL",
            "FALL")[d.month - 1], d.year


JELLYFIN = "http://localhost:8096"
_local = {"t": 0, "data": None}


def local_series():
    """Your Jellyfin library's own series names and posters (in the library's metadata language, e.g. Arabic), by
    folder name and by original title. Read from a temp copy of the database, cached 10 minutes."""
    if _local["data"] is not None and time.time() - _local["t"] < 600:
        return _local["data"]
    data = {"folder": {}, "original": {}}
    try:
        with agent.jellyfin_db() as c:
            try:
                rows = c.execute("select Id, Name, OriginalTitle, Path, Overview from BaseItems where Type like '%.Series'").fetchall()
            except Exception:                  # a Jellyfin version without Overview: titles and posters still work
                rows = [r + (None,) for r in c.execute("select Id, Name, OriginalTitle, Path from BaseItems where Type like '%.Series'").fetchall()]
        for item_id, name, original, path, overview in rows:
            if not name or not item_id:
                continue
            about = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(overview or ""))).strip()
            if len(about) > 320:
                about = about[:320].rsplit(" ", 1)[0].rstrip(".,;:، ") + "…"
            entry = {"title": name, "about": about,
                     "poster": f"{JELLYFIN}/Items/{str(item_id).replace('-', '').lower()}/Images/Primary?maxHeight=480"}
            if path:
                data["folder"][os.path.basename(str(path).rstrip("\\/")).casefold()] = entry
            if original:
                data["original"][str(original).casefold()] = entry
    except Exception:
        pass
    _local.update(t=time.time(), data=data)
    return data


def local_for(folder=None, native=None):
    data = local_series()
    return data["folder"].get(str(folder or "").casefold()) or data["original"].get(str(native or "").casefold())


def card(m, followed, new_season):
    nxt = m.get("nextAiringEpisode") or {}
    poster = (m.get("coverImage") or {}).get("large") or ""
    banner = m.get("bannerImage") or ""
    about = re.sub(r"\s+", " ", re.sub(r"<[^>]+>|\(Source:[^)]*\)", " ", __import__("html").unescape(m.get("description") or ""))).strip()
    if len(about) > 320:
        about = about[:320].rsplit(" ", 1)[0].rstrip(".,;: ") + "…"
    local = local_for(followed.get(m["id"]) if isinstance(followed, dict) else None, m["title"].get("native")) or {}
    return {"anilist": m["id"], "title": m["title"].get("english") or m["title"]["romaji"], "romaji": m["title"]["romaji"],
            "local_title": local.get("title", ""), "local_poster": local.get("poster", ""), "local_about": local.get("about", ""),
            "banner": banner if banner.startswith("https://s4.anilist.co/") else "", "about": about,
            "genres": [g for g in (m.get("genres") or []) if isinstance(g, str)][:3],
            "year": (m.get("startDate") or {}).get("year"), "episodes": m.get("episodes"), "next": nxt.get("episode"),
            "at": nxt.get("airingAt"), "poster": poster if poster.startswith("https://s4.anilist.co/") else "",
            "new": new_season, "followed": m["id"] in followed}


def recommendations():
    """Anime airing now: this season's new shows first, then continuing ones (cached 30 min)."""
    if _recommend["data"] is None or time.time() - _recommend["t"] > 1800:
        season, year = current_season()
        q = ("query($s:MediaSeason,$y:Int){new:Page(perPage:50){media(type:ANIME,season:$s,seasonYear:$y,isAdult:false,"
             "status_in:[RELEASING,NOT_YET_RELEASED],format_in:[TV,TV_SHORT,ONA],sort:POPULARITY_DESC){" + FIELDS + "}}"
             "old:Page(perPage:40){media(type:ANIME,status:RELEASING,isAdult:false,format_in:[TV,TV_SHORT,ONA],"
             "sort:POPULARITY_DESC){" + FIELDS + "}}}")
        data = anilist(q, {"s": season, "y": year})
        _recommend.update(t=time.time(), data=data, season=f"{season.title()} {year}")
    data, followed = _recommend["data"], {s.get("anilist"): s["folder"] for s in load() if s.get("anilist")}
    new = [card(m, followed, True) for m in data["new"]["media"]]
    seen = {c["anilist"] for c in new}
    old = [card(m, followed, False) for m in data["old"]["media"] if m["id"] not in seen]
    return {"season": _recommend["season"], "shows": new + old}


def search(query):
    query = str(query or "").strip()
    if not 2 <= len(query) <= 100:
        raise ShowError("Type at least two characters.")
    q = "query($q:String){Page(perPage:12){media(search:$q,type:ANIME,isAdult:false,sort:SEARCH_MATCH){" + FIELDS + "}}}"
    followed = {s.get("anilist"): s["folder"] for s in load() if s.get("anilist")}
    return {"shows": [card(m, followed, False) for m in anilist(q, {"q": query})["Page"]["media"]]}


def same_series(a, b):
    """'Kusuriya no Hitorigoto 3rd Season' and 'Kusuriya no Hitorigoto' are one series; 'ONE PIECE' and its
    'MONSTERS' one-shot (listed by AniList as a prequel) are not."""
    words = lambda t: re.findall(r"[a-z0-9]+", str(t or "").casefold())      # noqa: E731
    a, b = words(a), words(b)
    return bool(a and b) and a[:2] == b[:2]


def released(meta, season=None, now=None):
    """Episodes with a release date in the past (catalogs list future episodes, some without dates)."""
    now = time.time() if now is None else now
    out = []
    for v in meta.get("videos", []):
        try:
            sn, en = int(v["season"]), int(v["episode"])
            when = datetime.fromisoformat(str(v["released"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if v.get("id") and (season is None or sn == season) and when.replace(tzinfo=when.tzinfo or timezone.utc).timestamp() <= now:
            out.append({"id": v["id"], "season": sn, "episode": en})
    return sorted({(e["season"], e["episode"]): e for e in out}.values(), key=lambda e: (e["season"], e["episode"]))


def series_root(anilist_id):
    """(first-season media, season number of this entry): follows PREQUEL links back to season 1."""
    q = ("query($id:Int){Media(id:$id){" + FIELDS + " relations{edges{relationType node{id format title{romaji}}}}}}")
    chain, current = [], anilist(q, {"id": int(anilist_id)})["Media"]
    if not current:
        raise ShowError("That show wasn't found on AniList.")
    first = current
    for _ in range(12):
        prequel = next((e["node"]["id"] for e in (first.get("relations") or {}).get("edges", [])
                        if e["relationType"] == "PREQUEL" and e["node"].get("format") in ("TV", "TV_SHORT", "ONA")
                        and same_series(first["title"]["romaji"], (e["node"].get("title") or {}).get("romaji"))), None)
        if not prequel:
            break
        first = anilist(q, {"id": prequel})["Media"]
        chain.append(first)
    return current, first, len(chain) + 1


# ---------------------------------------------------------------- linking a show to DKB and Stremio
def dkb_lookup(names):
    """Newest DKB 1080p weekly release whose title matches one of `names` -> {release, season, latest} or None."""
    import dkb
    for name in [n for n in names if n][:4]:
        try:
            titles = dkb.search_titles(name)
        except Exception:
            continue
        found = {}
        for title in titles:
            show, ep = agent.parse_name(title)
            if show and ep:
                found.setdefault(show, []).append(ep)
        if found:
            release = max(found, key=lambda k: len(found[k]))
            latest = max(found[release], key=lambda e: tuple(map(int, re.findall(r"\d+", e))))
            season, episode = map(int, re.findall(r"\d+", latest))
            return {"release": release, "season": season, "latest": episode}
    return None


def stremio_lookup(names, year):
    """The show in the user's Stremio catalogs -> {id, type, name, season, latest} for its newest aired season."""
    import stremio as s
    wanted = {n.casefold() for n in names if n}
    for media_type in ("series", "anime"):
        for name in [n for n in names if n][:3]:
            try:
                results = s.search(name, media_type)["results"]
            except Exception:
                continue
            exact = [r for r in results if r["name"].casefold() in wanted]
            pick = next((r for r in exact if str(year) in str(r.get("year", ""))), None) or (exact or [None])[0]
            if not pick:
                continue
            details = s.details(pick["key"])
            meta = s.item(pick["key"], "title")["meta"]
            aired = released(meta)
            regular = [e for e in aired if e["season"] > 0] or aired
            if not regular:
                return {"id": meta["id"], "type": media_type, "name": details["name"], "season": 1, "latest": 0}
            last = max(regular, key=lambda e: (e["season"], e["episode"]))
            return {"id": meta["id"], "type": media_type, "name": details["name"], "season": last["season"],
                    "latest": last["episode"]}
    return None


def link(anilist_id):
    """Everything the Add form needs, filled in automatically: folder name, season, DKB and Stremio links."""
    current, first, number = series_root(anilist_id)
    root_title = first["title"].get("english") or first["title"]["romaji"]
    year = (first.get("startDate") or {}).get("year")
    import stremio as s
    folder = s.clean_name(f"{root_title} ({year})" if year else root_title)
    names = [current["title"]["romaji"], current["title"].get("english"), first["title"]["romaji"],
             first["title"].get("english")] + list(current.get("synonyms") or [])[:2]
    names = list(dict.fromkeys(n for n in names if n))
    dkb_found = dkb_lookup(names)
    stremio_found = stremio_lookup([first["title"].get("english"), first["title"]["romaji"], root_title], year)
    nxt = (current.get("nextAiringEpisode") or {}).get("episode")
    aired = (nxt - 1) if nxt else current.get("episodes")
    season = dkb_found["season"] if dkb_found else stremio_found["season"] if stremio_found else number
    if stremio_found:
        offset = 0
        if dkb_found and stremio_found["latest"]:
            # library numbering follows DKB; a real numbering difference (a continuing count, a split cour) is large,
            # while a gap of one or two only means one source is a day ahead of the other
            offset = dkb_found["latest"] - stremio_found["latest"]
            if abs(offset) <= 2:
                offset = 0
        stremio_found["offset"] = offset
    return {"anilist": current["id"], "title": current["title"].get("english") or current["title"]["romaji"],
            "folder": folder, "season": season, "aired": aired,
            "release": dkb_found["release"] if dkb_found else current["title"]["romaji"],
            "dkb": dkb_found, "stremio": stremio_found,
            "policy": "official_ok", "exists": (Path(agent.LIBRARY) / folder).is_dir()}


# ---------------------------------------------------------------- add / edit / remove
def _int(value, low, high, label):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ShowError(f"{label} must be a number.") from None
    if not low <= n <= high:
        raise ShowError(f"{label} must be between {low} and {high}.")
    return n


def _flag(value):
    return str(value).lower() in ("1", "true", "yes", "on")


def validate(form, existing=None):
    import stremio as s
    release = re.sub(r"\s+", " ", str(form.get("release", ""))).strip()
    if not 2 <= len(release) <= 80 or re.search(r'[<>:"/\\|?*\x00-\x1f]', release):
        raise ShowError("Enter the release name (as in the episode file names).")
    try:
        folder = s.clean_name(form.get("folder", ""))
    except s.UserError:
        raise ShowError("Enter a valid Jellyfin folder name, e.g. 'Blue Box (2024)'.") from None
    show = {"release": release, "folder": folder, "season": _int(form.get("season", 1), 0, 99, "Season"),
            "anilist": _int(form.get("anilist", 0), 0, 10**7, "AniList id") or None,
            "dkb": _flag(form.get("dkb")), "policy": form.get("policy") if form.get("policy") in POLICIES else "official_ok"}
    if form.get("stremio_id"):
        sid = str(form["stremio_id"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{2,60}", sid):
            raise ShowError("The Stremio link looks wrong. Link it again.")
        show["stremio"] = {"id": sid, "type": "anime" if form.get("stremio_type") == "anime" else "series",
                           "season": _int(form.get("stremio_season", 1), 0, 99, "Stremio season"),
                           "offset": _int(form.get("stremio_offset", 0), -5000, 5000, "Episode offset"),
                           "on": _flag(form.get("stremio_on", "true"))}
    poster = str(form.get("poster") or (existing or {}).get("poster") or "")
    if poster.startswith("https://s4.anilist.co/") and len(poster) < 300:
        show["poster"] = poster
    start = str(form.get("from", "")).strip()
    if start:
        show["from"] = _int(start, 1, 5000, "Start episode")
    others = [x for x in load() if not existing or x["release"] != existing["release"]]
    if any(x["release"].casefold() == release.casefold() for x in others):
        raise ShowError("That release name is already followed.")
    if any(x["folder"].casefold() == folder.casefold() for x in others):
        raise ShowError("Another show already uses that folder.")
    return show


def add(form):
    with _lock:
        show = validate(form)
        show["added"] = time.time()
        (Path(agent.LIBRARY) / show["folder"]).mkdir(parents=True, exist_ok=True)
        save(load() + [show])
    wake_dkb.set()
    wake_stremio.set()
    agent.log(f"shows: now following {show['folder']}")
    return show


def edit(form):
    with _lock:
        items = load()
        current = next((x for x in items if x["release"] == form.get("original")), None)
        if not current:
            raise ShowError("That show isn't followed any more. Reload the page.")
        show = validate(form, current)
        show["added"] = current.get("added", time.time())
        renamed = show["release"] != current["release"]
        if (show["folder"] != current["folder"] or renamed) and busy(current):
            raise ShowError("An episode of this show is still being downloaded or prepared. Change its name once that has finished.")
        restart = renamed or any(show.get(k) != current.get(k) for k in ("season", "from")) or \
            (show.get("stremio") or {}).get("season") != (current.get("stremio") or {}).get("season")
        if show["folder"] != current["folder"]:
            old, new = Path(agent.LIBRARY) / current["folder"], Path(agent.LIBRARY) / show["folder"]
            if new.exists():
                raise ShowError("A folder with the new name already exists in Weekly Anime.")
            try:
                if old.is_dir():
                    old.rename(new)
                else:
                    new.mkdir(parents=True)
            except OSError:
                raise ShowError("The show's folder is in use (a player or Jellyfin has a file open). Try again in a moment.") from None
        if restart:                                # only now: a failed edit above must leave no pending rename behind
            request_reset(current["release"], renamed_to=show["release"])
        save([show if x is current else x for x in items])
    wake_dkb.set()
    wake_stremio.set()
    return show


def remove(release):
    """Stop following; the show's folder moves from Weekly Anime to the Anime library (nothing is deleted)."""
    import cleanup
    with _lock:
        items = load()
        show = next((x for x in items if x["release"] == release), None)
        if not show:
            raise ShowError("That show isn't followed any more. Reload the page.")
        if busy(show):
            raise ShowError("An episode of this show is still being downloaded or prepared. Remove it once that has finished.")
        save([x for x in items if x is not show])
        request_reset(release)                         # following it again later starts fresh
    source, target = Path(agent.LIBRARY) / show["folder"], Path(cleanup.ANIME) / show["folder"]
    note = "Its folder stays in Weekly Anime."
    if source.is_dir() and any(source.iterdir()):
        if target.exists():
            note = f"An Anime folder named '{show['folder']}' already exists, so its Weekly Anime folder was left in place."
        else:
            try:
                shutil.move(str(source), str(target))
                note = "Its episodes moved to the Anime library."
            except OSError:
                note = "An episode is in use, so its folder stays in Weekly Anime for now."
    elif source.is_dir():
        source.rmdir()
        note = ""
    agent.log(f"shows: stopped following {show['folder']}")
    return {"removed": show["folder"], "note": note}


def busy(show):
    """True while an episode of this show is being fetched or waits in the Arabic preparation queue."""
    with _lock:
        if any(k.startswith(show["release"] + "|") for k in _claims):
            return True
    try:
        import library_processing
        with library_processing._guard:
            records = library_processing.read()
    except (OSError, ValueError):
        return True
    root = os.path.normcase(str(Path(agent.LIBRARY) / show["folder"])) + os.sep
    return any(r.get("status") != "done" and os.path.normcase(r.get("destination", "")).startswith(root)
               for r in records.values())


def check_now():
    wake_dkb.set()
    wake_stremio.set()
    return {"checking": True}


# ---------------------------------------------------------------- Stremio as a weekly source
GIVE_UP_DAYS = 3        # an episode no Stremio source offers this long after it aired is left to DKB / the user


def read_state():
    """The Stremio ledger. Missing = a fresh start; damaged = fail closed (raise): rewriting it from scratch would
    re-download episodes that were already watched and recycled."""
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    if not isinstance(state, dict):
        raise ValueError("weekly-state.json is damaged")
    for k in ("baseline", "done", "fails", "skipped"):
        state.setdefault(k, {})
    return state


def write_state(state):
    agent.write_atomic(STATE, json.dumps(state, ensure_ascii=False, indent=1))


def preferences():
    """Source choices for weekly anime episodes, best first: each preferred anime audio language at each resolution
    (Settings → Anime)."""
    import settings
    s = settings.get()
    out = []
    for audio in s["anime_audio"]:                # a preferred language beats a higher resolution
        for res in s["anime_resolutions"]:
            for sub in dict.fromkeys((s["anime_subtitle"], "none")):   # the extra subtitle is kept when offered, never required
                out.append({"audio": audio, "subtitle": sub, "resolution": res, "hdr": "Unspecified", "provider": ""})
    return out


def catalog_meta(st):
    """The show's catalog entry straight from the add-ons. Not registered in the page's selection cache, so a weekly
    check never pushes out what the user has open on the Discover page."""
    import stremio as s
    results, _ = s.parallel([(s.resource, (a, "meta", st["type"], st["id"])) for a in s.addons()
                             if s.supports(a, "meta", st["type"], st["id"])])
    metas = [r["meta"] for r in results if isinstance(r.get("meta"), dict) and r["meta"].get("videos")]
    if not metas:
        raise RuntimeError("no episode list from the add-ons")
    return max(metas, key=lambda m: len(m.get("videos", [])))


def aired(show):
    """[(library episode tag, library number, Stremio video, meta)] for the linked Stremio season, released only."""
    st = show["stremio"]
    meta = catalog_meta(st)
    out = []
    for e in released(meta, st["season"]):
        number = e["episode"] + st.get("offset", 0)
        if number > 0:
            out.append((tag(show["season"], number), number, e, meta))
    return out


class Busy(Exception):
    """The user is downloading something: try again next check (not counted as a failed attempt)."""


def fetch(show, ep_tag, number, episode, meta):
    """Download one episode from the best matching Stremio source into Weekly Anime (Arabic preparation follows)."""
    import stremio as s
    import stremio_batch as b
    library = next((lib for lib in s.libraries()
                    if os.path.normcase(os.path.abspath(lib["path"])) == os.path.normcase(os.path.abspath(agent.LIBRARY))), None)
    if not library:
        raise RuntimeError("Weekly Anime isn't a Jellyfin library")
    title = re.match(r"^(.*?)(?:\s*\((\d{4})\))?$", show["folder"])
    video = {"kind": "video", "type": show["stremio"]["type"], "id": episode["id"], "season": show["season"],
             "episode": number, "meta": {"id": meta.get("id"), "name": title.group(1), "releaseInfo": title.group(2) or "",
                                         "runtime": meta.get("runtime")}}
    own = threading.Event()        # never the season queue's Stop flag: stopping a batch must not stop weekly fetches
    for prefs in preferences():
        chosen = b.choose({"preferences": prefs}, video, cancel=own)
        if chosen:
            if s._job_lock.locked() or b.active():
                raise Busy()
            try:
                return s.start_download(*chosen, library["id"], _batch=True)
            except s.UserError as e:
                if "already running" in str(e) or "season/show download is active" in str(e):
                    raise Busy() from None
                raise
    return {"status": "unavailable", "stage": "No source in your preferred languages and resolutions yet"}


def _season_key(show):
    return f"{show['release']}|S{int(show['season']):02d}"


def stremio_poll():
    import stremio as s
    import stremio_batch as b
    try:
        state = read_state()
    except ValueError:
        agent.log("Stremio weekly: weekly-state.json is damaged; not checking until it is repaired or removed")
        return None
    resets = take_resets("stremio")
    for old, new in resets.items():
        for k in [k for k in state["baseline"] if k.split("|")[0] == old]:
            del state["baseline"][k]
        for bucket in ("fails", "skipped"):
            for k in [k for k in state[bucket] if k.split("|")[0] == old]:
                del state[bucket][k]
        if new != old:                             # renamed: finished episodes keep counting as finished
            for k in [k for k in state["done"] if k.split("|")[0] == old]:
                state["done"][new + "|" + k.split("|", 1)[1]] = state["done"].pop(k)
    if resets:
        write_state(state)
        ack_resets("stremio", resets)
    for show in load():
        st = show.get("stremio") or {}
        if not st.get("on"):
            continue
        try:
            episodes = aired(show)
        except Exception as e:
            agent.log(f"Stremio weekly: {show['folder']}: check failed ({type(e).__name__}); trying again next check")
            continue
        release, base_key = show["release"], _season_key(show)
        if base_key not in state["baseline"]:
            if show.get("from"):
                state["baseline"][base_key] = show["from"] - 1
            elif episodes:                         # never start from an empty list: that would fetch the whole season
                state["baseline"][base_key] = max(n for _, n, _, _ in episodes)
            else:
                continue
            write_state(state)
            agent.log(f"Stremio weekly: following {show['folder']} {base_key.split('|')[1]} after episode {state['baseline'][base_key]}")
        for ep_tag, number, episode, meta in sorted(episodes, key=lambda x: x[1]):
            key = f"{release}|{ep_tag}"
            if number <= state["baseline"][base_key] or key in state["done"] or key in state["skipped"]:
                continue
            if have_episode(show, ep_tag):
                state["done"][key] = "already here"
                write_state(state)
                continue
            if s._job_lock.locked() or b.active():
                return state                       # the user is downloading something: next check
            if not any(x["release"] == release for x in load()):
                break                              # stopped following it during this check
            if not claim(release, ep_tag, "stremio"):
                continue                           # DKB is fetching it right now
            try:
                agent.log(f"Stremio weekly: looking for {show['folder']} {ep_tag}")
                job = fetch(show, ep_tag, number, episode, meta)
                if job["status"] in ("done", "waiting"):
                    state["done"][key] = "stremio"
                    state["fails"].pop(key, None)
                    agent.log(f"Stremio weekly: downloaded {show['folder']} {ep_tag}")
                elif job["status"] == "cancelled":
                    state["skipped"][key] = "cancelled"   # the user stopped it; DKB may still bring it
                else:
                    failed(state, key, show, ep_tag, job.get("stage", ""))
            except Busy:
                return state
            except s.UserError as e:
                if "already exists" in str(e) or "already downloaded" in str(e):
                    state["done"][key] = "already here"
                else:
                    failed(state, key, show, ep_tag, str(e))
            except Exception as e:
                failed(state, key, show, ep_tag, type(e).__name__)
            finally:
                release_claim(release, ep_tag)
                write_state(state)
    return state


def failed(state, key, show, ep_tag, why):
    first = state["fails"].get(key, {}).get("first", time.time()) if isinstance(state["fails"].get(key), dict) else time.time()
    state["fails"][key] = {"first": first, "why": str(why)[:120]}
    if time.time() - first > GIVE_UP_DAYS * 86400:
        state["skipped"][key] = "not found"
        state["fails"].pop(key, None)
        agent.log(f"Stremio weekly: gave up on {show['folder']} {ep_tag} after {GIVE_UP_DAYS} days ({why})")
    else:
        agent.log(f"Stremio weekly: {show['folder']} {ep_tag} not downloaded yet ({str(why)[:80]}); trying again next check")


def run_stremio():
    import settings
    while True:
        try:
            stremio_poll()
        except Exception as e:
            agent.log(f"Stremio weekly: check failed ({type(e).__name__})")
        wake_stremio.wait(settings.minutes())
        wake_stremio.clear()


def overview():
    """The followed list for the page, with each source's state."""
    dkb_state = {}
    try:
        weekly = read_state()
    except ValueError:
        weekly = {"baseline": {}, "done": {}, "fails": {}, "skipped": {}}
    try:
        dkb_state = json.loads((Path(agent.HERE) / "dkb-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    out = []
    for show in load():
        folder = Path(agent.LIBRARY) / show["folder"]
        eps = sorted({m.group(0).upper() for p in folder.rglob("*") if p.suffix.lower() in (".mkv", ".mp4")
                      for m in [re.search(r"S\d+E\d+", p.name, re.I)] if m}) if folder.is_dir() else []
        mine = lambda bucket: sorted(k.split("|")[1] for k in weekly[bucket] if k.split("|")[0] == show["release"])  # noqa: E731
        local = local_for(show["folder"]) or {}
        out.append(dict(show, episodes=len(eps), latest=eps[-1] if eps else None,
                        local_title=local.get("title", ""), local_poster=local.get("poster", ""),
                        dkb_after=(dkb_state.get("baseline") or {}).get(show["release"]),
                        stremio_after=weekly["baseline"].get(_season_key(show)),
                        not_found=[k for k in mine("skipped") if weekly["skipped"][f"{show['release']}|{k}"] == "not found"],
                        retrying=mine("fails")))
    return {"shows": out, "library": agent.LIBRARY}
