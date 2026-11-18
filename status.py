"""Local status page for the Kizuna agent: http://localhost:8097

Started by watcher.py (child) as a daemon thread, so it lives and restarts with the agent.
Read-only: it looks at progress.json, state.json, log.txt, the inbox folder, the Weekly Anime
folders and Jellyfin's database (posters/watch data). `python status.py` runs it standalone;
`python status.py --check` validates the data once.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agent  # noqa: E402  (paths, SHOWS map, tracks(), parse_name())

PORT = 8097
JF = "http://localhost:8096"
_cache = {}
_lock = threading.Lock()


def _json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def nice(fn):
    """'[DKB] Ao no Hako - S02E05 [1080p]...mkv' -> 'Blue Box · S02E05' (falls back to a cleaned file name)."""
    name, ep = agent.parse_name(os.path.basename(fn or ""))
    if not name:
        base = re.sub(r"\[[^\]]*\]", "", os.path.splitext(os.path.basename(fn or ""))[0])
        return re.sub(r"\s+", " ", base).strip(" -") or fn
    folder = next((v for k, v in agent.SHOWS.items() if name.startswith(k)), name)
    return f"{re.sub(r'\s*\(\d{4}\)$', '', folder)} · {ep}"


def _procs():
    import psutil
    out = {"supervisor": None, "watcher": None, "agent": None}
    for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
        cmd = " ".join(p.info["cmdline"] or [])
        if "watcher.py" in cmd:
            out["watcher" if "--child" in cmd else "supervisor"] = {"pid": p.info["pid"], "since": p.info["create_time"]}
        elif os.path.normcase(os.path.join(agent.HERE, "agent.py")) in os.path.normcase(cmd):   # ours, whatever the folder is called
            out["agent"] = {"pid": p.info["pid"], "since": p.info["create_time"]}
    return out


def _has_arabic(path):
    st = os.stat(path)
    with _lock:
        c = _cache.get(path)
        if c and c[:2] == (st.st_mtime, st.st_size):
            return c[2]
    try:
        video_tracks = agent.tracks(path)
        ok = agent.has_arabic_audio(video_tracks) or any(t["properties"].get("track_name") == "العربية" for t in video_tracks)
    except Exception:
        ok = None
    with _lock:
        _cache[path] = (st.st_mtime, st.st_size, ok)
    return ok


_ids = {"t": 0, "data": {}}


def _series_ids():
    """Weekly Anime folder name -> Jellyfin series id (for posters). Cached 10 min: each lookup copies Jellyfin's
    whole database, and the page polls every few seconds."""
    if time.time() - _ids["t"] < 600:
        return _ids["data"]
    _ids.update(t=time.time(), data=_series_ids_now())
    return _ids["data"]


def _series_ids_now():
    try:
        with agent.jellyfin_db() as c:
            rows = c.execute("select Id, Path from BaseItems where Type like '%.Series' and Path like ?",
                             (agent.LIBRARY + "%",)).fetchall()
        return {os.path.basename(p): i.replace("-", "").lower() for i, p in rows}
    except Exception:
        return {}


def _shows(lines=()):
    ids = _series_ids()
    done = [agent.parse_name(m.group(1)) for l in lines if (m := re.search(r" DONE (.*?):\s*ok", l))]
    rows = []
    import shows
    for show in shows.load():
        prefix, folder = show["release"], show["folder"]
        eps = []
        for _, _, fs in os.walk(os.path.join(agent.LIBRARY, folder)):
            eps += [f for f in fs if f.lower().endswith(".mkv")]
        seen = [m.group(1) for f in eps if (m := re.search(r"(S\d+E\d+)", f))]
        seen += [ep for name, ep in done if name and name.startswith(prefix)]   # also episodes done before the library existed
        latest = max(seen, key=lambda e: tuple(map(int, re.findall(r"\d+", e))), default=None)
        sid = ids.get(folder)
        air = _airing().get(show.get("anilist"), {})
        rows.append({"title": re.sub(r"\s*\(\d{4}\)$", "", folder), "latest": latest, "count": len(eps),
                     "next": air.get("next"), "at": air.get("at"), "total": air.get("total"),
                     "poster": f"{JF}/Items/{sid}/Images/Primary?maxHeight=420" if sid else None,
                     "manual": show.get("policy") == "manual"})
    return rows


def _keep_hours():
    import cleanup
    return cleanup.keep_seconds() // 3600


def _log_lines():
    try:
        return open(agent.LOG, encoding="utf-8", errors="replace").read().splitlines()
    except Exception:
        return []


def _deletions():
    try:
        import cleanup
        def label(p):              # Anime-library files are "10.mkv": prefix the show folder
            if cleanup.inside(p, cleanup.ANIME):
                show = os.path.relpath(p, cleanup.ANIME).split(os.sep)[0]
                show = re.sub(r"\s*(\[[^\]]*\]|\(\d{4}\))", "", show).strip(" -")
                _, ep = agent.parse_name(os.path.basename(p))      # "Bleach (2004) - S10E13.mkv" -> "Bleach · S10E13"
                return f"{show} · {ep or os.path.splitext(os.path.basename(p))[0]}"
            return nice(p)
        return [{"name": label(p), "at": d} for p, t, d in cleanup.due()]
    except Exception:
        return []


_air = {"t": 0, "data": {}, "ids": ()}


def _airing():
    """{anilist id: {next, at, total}} from AniList's public schedule, refreshed every 30 min (last good kept)."""
    import shows
    ids = tuple(sorted(s["anilist"] for s in shows.load() if s.get("anilist")))   # AniList id of each show's airing season
    if time.time() - _air["t"] < 1800 and ids == _air["ids"]:
        return _air["data"]
    _air["ids"] = ids
    q = "query($ids:[Int]){Page(perPage:50){media(id_in:$ids){id episodes nextAiringEpisode{episode airingAt}}}}"
    try:
        req = urllib.request.Request("https://graphql.anilist.co",
                                     data=json.dumps({"query": q, "variables": {"ids": list(ids)}}).encode(),
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": "AnimeFYA-status"})
        media = json.load(urllib.request.urlopen(req, timeout=15))["data"]["Page"]["media"]
        _air["data"] = {m["id"]: {"next": (m["nextAiringEpisode"] or {}).get("episode"),
                                  "at": (m["nextAiringEpisode"] or {}).get("airingAt"), "total": m["episodes"]}
                        for m in media}
        _air["t"] = time.time()
    except Exception:
        _air["t"] = time.time() - 1500          # offline: try again in 5 min, keep showing the last schedule
    return _air["data"]


def _jellyfin_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:8096/System/Info/Public", timeout=2).close()
        return True
    except Exception:
        return False


def collect(pending=None, light=False):
    import fansub
    now = time.time()
    procs = _procs()
    prog = _json(agent.PFILE, {})
    state = _json(agent.STATE, {})
    lines = _log_lines()
    finished = []
    for l in reversed(lines):
        m = re.match(r"^(\S+ \S+) DONE (.*?):\s*ok", l)
        if m:
            finished.append({"name": nice(m.group(2)), "at": time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))})
        if len(finished) == 5:
            break

    job = None
    if prog and prog.get("status") == "run":
        age = now - prog.get("t", now)
        job = {"name": nice(prog.get("episode")) if " - S" in (prog.get("episode") or "") else prog.get("episode"),
               "stage": prog.get("stage"), "pct": prog.get("pct") or 0, "stalled": age > 600, "age": age}

    waiting = [nice(p) for p in (pending() if pending else {})]
    if os.path.isdir(agent.FOLDER):
        for fn in sorted(os.listdir(agent.FOLDER)):
            p = os.path.join(agent.FOLDER, fn)
            if (fn.lower().endswith(".mkv") and ".tmp." not in fn and os.path.isfile(p)
                    and fansub.mode_for(agent.parse_name(fn)[0] or fn) != "manual"   # typeset by hand
                    and fn not in (state.get("failed") or {})             # listed under "Needs a look" instead
                    and _has_arabic(p) is False):
                if nice(fn) not in waiting:
                    waiting.append(nice(fn))

    attention = [{"name": nice(fn), "reason": "Failed. Skipped until the file changes", "kind": "failed"}
                 for fn in (state.get("failed") or {}) if os.path.exists(os.path.join(agent.FOLDER, fn))]
    attention += [{"name": nice(fn), "reason": "Hit a problem. Retrying automatically", "kind": "retrying"}
                  for fn in (state.get("errors") or {})
                  if fn not in (state.get("failed") or {}) and os.path.exists(os.path.join(agent.FOLDER, fn))]
    attention += [{"name": nice(fn), "reason": "Arabic added, but copying to Jellyfin failed. Retrying", "kind": "retrying"}
                  for fn in (state.get("copy_failed") or {}) if os.path.exists(os.path.join(agent.FOLDER, fn))]
    pending_items = [{"name": x, "reason": "Waiting for an Arabic subtitle"} for x in waiting]
    problems = [f"{nice(fn)} — failed, skipped until the file changes"
                for fn in (state.get("failed") or {}) if os.path.exists(os.path.join(agent.FOLDER, fn))]
    problems += [f"{nice(fn)} — retrying" for fn in (state.get("errors") or {})
                 if fn not in (state.get("failed") or {}) and os.path.exists(os.path.join(agent.FOLDER, fn))]
    problems += [f"{nice(fn)} — Arabic added, but copying to Jellyfin failed; retrying"
                 for fn in (state.get("copy_failed") or {}) if os.path.exists(os.path.join(agent.FOLDER, fn))]
    if job and job["stalled"]:
        problems.append(f"{job['name']} — no progress for over 10 minutes")
        attention.append({"name": job["name"], "reason": "No progress for over 10 minutes", "kind": "stalled"})
    import library_processing
    try:
        library_jobs = library_processing.status()
        for entry in library_jobs:
            if entry["status"] in ("waiting", "pending"):
                problems.append(f"{nice(entry['destination'])} — {entry['stage']}")
                failed = "failed" in entry["stage"].lower() or "needs" in entry["stage"].lower()
                (attention if failed else pending_items).append(
                    {"name": nice(entry["destination"]), "reason": entry["stage"], "kind": "failed" if failed else "waiting",
                     "retry_at": entry.get("retry_at")})
            elif entry["status"] == "running" and not job:
                job = {"name": nice(entry["destination"]), "stage": entry["stage"], "pct": entry.get("pct", 0),
                       "stalled": now - entry["updated"] > 600, "age": now - entry["updated"]}
    except (OSError, ValueError, KeyError):
        problems.append("The library processing queue could not be read; check its ledger.")

    if not procs["watcher"]:
        headline, tone = "Agent is not running", "bad"
    elif job and not job["stalled"]:
        headline, tone = f"Working on {job['name']}", "busy"
    elif problems:
        headline, tone = "Watching — something needs a look", "warn"
    else:
        headline, tone = "Watching for new episodes", "ok"

    du = shutil.disk_usage(os.path.splitdrive(agent.FOLDER)[0] + "\\")
    return {
        "now": now, "headline": headline, "tone": tone, "job": job, "finished": finished,
        "waiting": waiting, "problems": problems, "deletions": [] if light else _deletions(), "shows": [] if light else _shows(lines),
        "attention": attention, "pending": pending_items, "keep_hours": _keep_hours(),
        "system": {
            "Watcher": "running" if procs["watcher"] else "not running",
            "Supervisor": "running" if procs["supervisor"] else "not running",
            "Jellyfin": "running" if _jellyfin_up() else "not reachable",
            "Disk free": f"{du.free / 1e9:.0f} GB of {du.total / 1e9:.0f} GB",
            "Watched folder": agent.FOLDER,
            "Library folder": agent.LIBRARY,
        },
        "log": lines[-60:],
    }


# The pages (dashboard.html, stremio.html, weekly.html, settings.html) share kizuna.css and kizuna.js.


PAGES = {"/": "dashboard.html", "/index.html": "dashboard.html", "/stremio": "stremio.html", "/weekly": "weekly.html",
         "/settings": "settings.html", "/news": "news.html"}
ASSETS = {"/kizuna.css": ("kizuna.css", "text/css; charset=utf-8"), "/kizuna.js": ("kizuna.js", "text/javascript; charset=utf-8"),
          "/fonts/Cairo-SemiBold.ttf": ("fonts/Cairo-SemiBold.ttf", "font/ttf"),
          "/fonts/Cairo-Bold.ttf": ("fonts/Cairo-Bold.ttf", "font/ttf")}


def make_handler(pending=None):
    """Request handler for the status app. Every request must be addressed to this machine by name
    (blocks DNS-rebinding pages from reading the status/log or the Stremio page; audit #9)."""
    import hmac
    import shows
    import stremio
    import stremio_batch

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def local_host(self):
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"localhost:{port}", f"127.0.0.1:{port}")

        def stremio_api(self, post=False):
            from urllib.parse import urlsplit, parse_qs
            path = urlsplit(self.path)
            host = self.headers.get("Host", "")
            if (not hmac.compare_digest(self.headers.get("X-MyAnimeWatcher", "").encode(), stremio.TOKEN.encode())
                    or self.headers.get("Origin", "http://" + host) != "http://" + host):
                self.send_error(403)
                return
            try:
                if post:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise stremio.UserError("Invalid request body.")
                    params = json.loads(self.rfile.read(length))
                else:
                    params = {k: v[0] for k, v in parse_qs(path.query).items()}
                if not isinstance(params, dict) or any(not isinstance(v, str) or len(v) > (12000 if k == "episodes" else 500) for k, v in params.items()):
                    raise stremio.UserError("Invalid selection.")
                action = path.path.removeprefix("/api/stremio/")
                if not post and action == "connection":
                    data = stremio.connection()
                elif not post and action == "job":
                    data = {"job": stremio.job_status(), "batch": stremio_batch.status()}
                elif not post and action == "search":
                    data = stremio.search(params.get("q", ""), params.get("type", ""))
                elif not post and action == "details":
                    data = stremio.details(params.get("key"))
                elif not post and action == "fillers":
                    import fillers
                    data = fillers.lookup(params.get("key"))
                elif not post and action == "streams":
                    data = stremio.streams(params.get("key"))
                elif post and action == "sync":
                    data = stremio.sync()
                elif post and action == "inspect":
                    data = stremio.inspect(params.get("key"))
                elif post and action == "subtitles":
                    data = stremio.subtitle_options(params.get("key"))
                elif post and action == "download":
                    data = stremio.start_download(params.get("key"), params.get("audio"),
                                                 params.get("subtitle"), params.get("library"))
                elif post and action == "download-cancel":
                    data = stremio.cancel_download()
                elif post and action == "batch":
                    data = {"batch": stremio_batch.start(params.get("key"), params.get("audio"),
                            params.get("subtitle"), params.get("library"), params.get("scope"), params.get("episodes", ""))}
                elif post and action == "batch-stop":
                    data = {"batch": stremio_batch.stop()}
                elif post and action == "batch-resume":
                    data = {"batch": stremio_batch.resume()}
                elif post and action == "download-pause":
                    data = {"job": stremio.pause_download()}
                elif post and action == "download-resume":
                    data = {"job": stremio.resume_download()}
                elif post and action == "download-dismiss":
                    data = {"job": stremio.dismiss_download()}
                elif post and action == "batch-dismiss":
                    data = {"batch": stremio_batch.dismiss()}
                elif not post and action == "shows":
                    data = shows.overview()
                elif not post and action == "shows-recommend":
                    data = shows.recommendations()
                elif not post and action == "shows-search":
                    data = shows.search(params.get("q", ""))
                elif post and action == "shows-link":
                    data = shows.link(shows._int(params.get("anilist"), 1, 10**7, "AniList id"))
                elif post and action == "shows-add":
                    data = {"show": shows.add(params)}
                elif post and action == "shows-edit":
                    data = {"show": shows.edit(params)}
                elif post and action == "shows-remove":
                    data = shows.remove(params.get("release", ""))
                elif post and action == "shows-check":
                    data = shows.check_now()
                elif not post and action == "news":
                    import news
                    kind = params.get("kind", "all")
                    if kind not in ("all", "anime", "screen"):
                        raise stremio.UserError("Unknown news section.")
                    data = news.latest(kind)
                elif not post and action == "settings":
                    import settings
                    data = settings.page()
                elif post and action == "settings":
                    import settings
                    data = settings.update(params)
                else:
                    self.send_error(404)
                    return
                code = 200
            except Exception as e:
                import settings
                data = {"error": str(e) if isinstance(e, (stremio.UserError, shows.ShowError, settings.SettingsError))
                        else "The request could not be completed. Try another source or sync your Stremio add-ons."}
                code = 400
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")                       # no other site may frame these pages
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def do_POST(self):
            if not self.local_host():
                self.send_error(403)
                return
            if self.path.startswith("/api/stremio/"):
                self.stremio_api(post=True)
            else:
                self.send_error(404)

        def do_GET(self):
            if not self.local_host():
                self.send_error(403)
                return
            if self.path.startswith("/api/stremio/"):
                self.stremio_api()
                return
            route = self.path.split("?", 1)[0].rstrip("/") or "/"
            if route in PAGES:
                page = open(os.path.join(HERE, PAGES[route]), encoding="utf-8").read()
                body, ctype = page.replace("__STREMIO_TOKEN__", stremio.TOKEN).encode("utf-8"), "text/html; charset=utf-8"
            elif route in ASSETS:
                body, ctype = open(os.path.join(HERE, *ASSETS[route][0].split("/")), "rb").read(), ASSETS[route][1]
            elif self.path.startswith("/api/status"):
                try:
                    # ?light=1 (the navigation's activity indicator): no Jellyfin database copy, no artwork lookups
                    body = json.dumps(collect(pending, light="light=1" in self.path), ensure_ascii=False).encode("utf-8")
                except Exception as e:
                    body = json.dumps({"headline": f"Status error: {e!r}", "tone": "bad", "now": time.time(),
                                       "finished": [], "waiting": [], "problems": [], "deletions": [], "shows": [],
                                       "system": {}, "log": [], "job": None}).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            elif self.path in ("/app.png", "/favicon.ico"):
                body, ctype = open(os.path.join(HERE, "app.png"), "rb").read(), "image/png"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")                       # no other site may frame these pages
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


def serve(pending=None, port=PORT):
    handler = make_handler(pending)
    while True:                         # port busy (old watcher still exiting): retry quietly
        try:
            ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
        except OSError as e:
            agent.log(f"status page: port {port} unavailable ({e}), retrying in 30 s")
            time.sleep(30)


if __name__ == "__main__":
    if "--check" in sys.argv:
        d = collect()
        for k in ("headline", "tone", "shows", "finished", "waiting", "problems", "deletions", "system", "log"):
            assert k in d, k
        assert len(d["shows"]) == len(agent.SHOWS)
        print("ok:", d["headline"], "|", [s["title"] for s in d["shows"]], "|", d["finished"][:1], "|", d["problems"])
    else:
        print(f"http://localhost:{PORT}")
        serve()
