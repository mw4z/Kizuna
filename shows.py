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
INTERVAL = 20 * 60
MAX_FAILS = 6                                       # Stremio attempts per episode before giving up on it
_lock = threading.RLock()
_cache = {"stamp": None, "shows": []}
_claims = {}                                        # "release|SxxEyy" -> source currently fetching it
wake_dkb, wake_stremio = threading.Event(), threading.Event()


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
            data = json.loads(FILE.read_text(encoding="utf-8"))
            _cache.update(stamp=stamp, shows=[s for s in data.get("shows", [])
                                              if isinstance(s, dict) and s.get("release") and s.get("folder")])
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


def release_claim(release, episode_tag):
    with _lock:
        _claims.pop(f"{release}|{episode_tag}", None)


def have_episode(show, episode_tag):
    """True when the episode is already in the library, the inbox, on its way (Arabic preparation queue), or was
    fetched/skipped by either source."""
    pattern = re.compile(re.escape(episode_tag) + r"(?!\d)", re.I)
    folder = Path(agent.LIBRARY) / show["folder"]
    if folder.is_dir() and any(pattern.search(p.name) and p.suffix.lower() in (".mkv", ".mp4", ".m4v")
                               for p in folder.rglob("*")):
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
        root = os.path.normcase(str(folder))
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
FIELDS = ("id title{romaji english} synonyms format season seasonYear episodes status "
          "nextAiringEpisode{episode airingAt} coverImage{large} startDate{year}")
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


def card(m, followed, new_season):
    nxt = m.get("nextAiringEpisode") or {}
    poster = (m.get("coverImage") or {}).get("large") or ""
    return {"anilist": m["id"], "title": m["title"].get("english") or m["title"]["romaji"], "romaji": m["title"]["romaji"],
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
    data, followed = _recommend["data"], {s.get("anilist") for s in load()}
    new = [card(m, followed, True) for m in data["new"]["media"]]
    seen = {c["anilist"] for c in new}
    old = [card(m, followed, False) for m in data["old"]["media"] if m["id"] not in seen]
    return {"season": _recommend["season"], "shows": new + old}


def search(query):
    query = str(query or "").strip()
    if not 2 <= len(query) <= 100:
        raise ShowError("Type at least two characters.")
    q = "query($q:String){Page(perPage:12){media(search:$q,type:ANIME,isAdult:false,sort:SEARCH_MATCH){" + FIELDS + "}}}"
    followed = {s.get("anilist") for s in load()}
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
        if dkb_found and stremio_found["latest"]:
            # library numbering follows DKB (per season); Stremio may count episodes differently
            stremio_found["offset"] = (aired or dkb_found["latest"]) - stremio_found["latest"]
        else:
            stremio_found["offset"] = 0
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
    elif existing and existing.get("from"):
        show["from"] = existing["from"]
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
        if show["folder"] != current["folder"]:
            old, new = Path(agent.LIBRARY) / current["folder"], Path(agent.LIBRARY) / show["folder"]
            if new.exists():
                raise ShowError("A folder with the new name already exists in Weekly Anime.")
            if old.is_dir():
                old.rename(new)
            else:
                new.mkdir(parents=True)
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
        save([x for x in items if x is not show])
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


def check_now():
    wake_dkb.set()
    wake_stremio.set()
    return {"checking": True}


# ---------------------------------------------------------------- Stremio as a weekly source
def read_state():
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    for k in ("baseline", "done", "fails"):
        state.setdefault(k, {})
    return state


def write_state(state):
    agent.write_atomic(STATE, json.dumps(state, ensure_ascii=False, indent=1))


PREFERENCES = [{"audio": "ja", "subtitle": "none", "resolution": res, "hdr": "Unspecified", "provider": ""}
               for res in ("1080p", "720p")]


def aired(show):
    """[(library episode tag, Stremio video)] for the linked Stremio season, released episodes only."""
    import stremio as s
    st = show["stremio"]
    key = s.remember({"kind": "title", "type": st["type"], "meta": {"id": st["id"], "name": show["folder"]}})
    s.details(key)
    meta = s.item(key, "title")["meta"]
    out = []
    for e in released(meta, st["season"]):
        number = e["episode"] + st.get("offset", 0)
        if number > 0:
            out.append((tag(show["season"], number), number, e, meta))
    return out


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
    for prefs in PREFERENCES:
        chosen = b.choose({"preferences": prefs}, video)
        if chosen:
            return s.start_download(*chosen, library["id"], _batch=True)
    return {"status": "unavailable", "stage": "No 1080p/720p Japanese-audio source yet"}


def stremio_poll():
    import stremio as s
    import stremio_batch as b
    state = read_state()
    for show in load():
        st = show.get("stremio") or {}
        if not st.get("on"):
            continue
        try:
            episodes = aired(show)
        except Exception as e:
            agent.log(f"Stremio weekly: {show['folder']}: check failed ({type(e).__name__})")
            continue
        release = show["release"]
        if release not in state["baseline"]:
            # like DKB: start after the newest aired episode unless a start episode was chosen (no backfill)
            state["baseline"][release] = (show["from"] - 1) if show.get("from") else max((n for _, n, _, _ in episodes), default=0)
            write_state(state)
            agent.log(f"Stremio weekly: following {show['folder']} after episode {state['baseline'][release]}")
        for ep_tag, number, episode, meta in sorted(episodes, key=lambda x: x[1]):
            key = f"{release}|{ep_tag}"
            if number <= state["baseline"][release] or key in state["done"] or state["fails"].get(key, 0) >= MAX_FAILS:
                continue
            if have_episode(show, ep_tag):
                state["done"][key] = "already here"
                write_state(state)
                continue
            if s._job_lock.locked() or b.active():
                return state                       # the user is downloading something: next check
            if not claim(release, ep_tag, "stremio"):
                continue                           # DKB is fetching it right now
            try:
                agent.log(f"Stremio weekly: looking for {show['folder']} {ep_tag}")
                job = fetch(show, ep_tag, number, episode, meta)
                if job["status"] in ("done", "waiting"):
                    state["done"][key] = "stremio"
                    agent.log(f"Stremio weekly: downloaded {show['folder']} {ep_tag}")
                elif job["status"] == "cancelled":
                    state["done"][key] = "cancelled"     # the user stopped it: don't fetch it again by itself
                else:
                    state["fails"][key] = state["fails"].get(key, 0) + 1
                    agent.log(f"Stremio weekly: {show['folder']} {ep_tag} not downloaded ({job.get('stage', '')[:80]})")
            except s.UserError as e:
                if "already exists" in str(e) or "already downloaded" in str(e):
                    state["done"][key] = "already here"
                else:
                    state["fails"][key] = state["fails"].get(key, 0) + 1
                    agent.log(f"Stremio weekly: {show['folder']} {ep_tag}: {e}")
            except Exception as e:
                state["fails"][key] = state["fails"].get(key, 0) + 1
                agent.log(f"Stremio weekly: {show['folder']} {ep_tag} failed ({type(e).__name__})")
            finally:
                release_claim(release, ep_tag)
                write_state(state)
    return state


def run_stremio():
    while True:
        try:
            stremio_poll()
        except Exception as e:
            agent.log(f"Stremio weekly: check failed ({type(e).__name__})")
        wake_stremio.wait(INTERVAL)
        wake_stremio.clear()


def overview():
    """The followed list for the page, with each source's state."""
    dkb_state, weekly = {}, read_state()
    try:
        dkb_state = json.loads((Path(agent.HERE) / "dkb-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    out = []
    for show in load():
        folder = Path(agent.LIBRARY) / show["folder"]
        eps = sorted({m.group(0).upper() for p in folder.rglob("*") if p.suffix.lower() in (".mkv", ".mp4")
                      for m in [re.search(r"S\d+E\d+", p.name, re.I)] if m}) if folder.is_dir() else []
        out.append(dict(show, episodes=len(eps), latest=eps[-1] if eps else None,
                        dkb_after=(dkb_state.get("baseline") or {}).get(show["release"]),
                        stremio_after=weekly["baseline"].get(show["release"])))
    return {"shows": out, "library": agent.LIBRARY}
