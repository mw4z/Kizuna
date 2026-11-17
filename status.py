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
                return f"{show} · {os.path.splitext(os.path.basename(p))[0]}"
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


def collect(pending=None):
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
                    and "Detective Conan" not in fn                       # typeset by hand, never auto-processed
                    and fn not in (state.get("failed") or {})             # listed under "Needs a look" instead
                    and _has_arabic(p) is False):
                if nice(fn) not in waiting:
                    waiting.append(nice(fn))

    problems = [f"{nice(fn)} — failed, skipped until the file changes"
                for fn in (state.get("failed") or {}) if os.path.exists(os.path.join(agent.FOLDER, fn))]
    problems += [f"{nice(fn)} — retrying" for fn in (state.get("errors") or {})
                 if fn not in (state.get("failed") or {}) and os.path.exists(os.path.join(agent.FOLDER, fn))]
    problems += [f"{nice(fn)} — Arabic added, but copying to Jellyfin failed; retrying"
                 for fn in (state.get("copy_failed") or {}) if os.path.exists(os.path.join(agent.FOLDER, fn))]
    if job and job["stalled"]:
        problems.append(f"{job['name']} — no progress for over 10 minutes")
    import library_processing
    try:
        library_jobs = library_processing.status()
        for entry in library_jobs:
            if entry["status"] in ("waiting", "pending"):
                problems.append(f"{nice(entry['destination'])} — {entry['stage']}")
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
        "waiting": waiting, "problems": problems, "deletions": _deletions(), "shows": _shows(lines),
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


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MyAnimeWatcher</title>
<link rel="icon" type="image/png" href="/app.png">
<style>
:root{--bg:#101113;--fg:#ececee;--mute:#8d9097;--line:#24262b;--ok:#46c37b;--busy:#5b9cff;--warn:#e6a23c;--bad:#ef5b5b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:40px 24px 64px}
.app{color:var(--mute);font-size:13px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.discover{display:inline-flex;align-items:center;justify-content:center;min-height:42px;padding:10px 18px;border:1px solid #79afff;border-radius:8px;background:var(--busy);color:#101b2b;font-size:14px;font-weight:600;text-decoration:none;text-align:center;transition:background .15s,border-color .15s}
.discover:hover{background:#8ab9ff;border-color:#a6caff}
.discover:focus-visible{outline:2px solid var(--fg);outline-offset:4px}
h1{font-size:28px;font-weight:600;margin:0;display:flex;align-items:center;gap:12px}
.dot{width:12px;height:12px;border-radius:50%;flex:none}
.sub{color:var(--mute);margin-top:6px}
.job{margin-top:22px}.job .stage{color:var(--mute);font-size:14px;display:flex;justify-content:space-between}
.track{height:6px;background:var(--line);border-radius:3px;margin-top:8px;overflow:hidden}
.track div{height:100%;background:var(--busy);border-radius:3px;transition:width .8s}
h2{font-size:17px;font-weight:600;margin:44px 0 14px}
.shows{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:20px}
.show img,.show .ph{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:8px;background:#1a1b1f;display:block}
.show .t{margin-top:8px;font-weight:600;font-size:14px;line-height:1.3}
.show .m{color:var(--mute);font-size:13px}
.next{margin-top:8px;padding-top:8px;border-top:1px solid var(--line);font-size:13px}
.next .ep{color:var(--fg)}.next .cd{color:var(--busy);font-weight:600;font-variant-numeric:tabular-nums}
.next .cd.soon{color:var(--ok)}.next .left{color:var(--mute)}
ul.rows{list-style:none;margin:0;padding:0}
ul.rows li{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-bottom:1px solid var(--line)}
ul.rows li span:last-child{color:var(--mute);white-space:nowrap}
.warn li span:first-child{color:var(--warn)}
details{margin-top:48px;color:var(--mute)}summary{cursor:pointer;font-size:14px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;margin:16px 0}dt{color:var(--mute)}dd{margin:0;color:var(--fg);word-break:break-all}
pre{background:#0b0c0d;border:1px solid var(--line);border-radius:8px;padding:12px;max-height:320px;overflow:auto;font:12px/1.55 Consolas,monospace;color:#b9bcc2;white-space:pre-wrap}
/* motion */
@keyframes up{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes breathe{0%{box-shadow:0 0 0 0 var(--glow)}70%{box-shadow:0 0 0 10px transparent}100%{box-shadow:0 0 0 0 transparent}}
@keyframes shimmer{from{background-position:-200% 0}to{background-position:200% 0}}
.reveal{opacity:0;animation:up .6s cubic-bezier(.2,.7,.2,1) forwards}
.dot{--glow:rgba(70,195,123,.55);animation:breathe 2.6s ease-out infinite}
.dot.busy{--glow:rgba(91,156,255,.6);animation-duration:1.1s}
.dot.warn{--glow:rgba(230,162,60,.55)}.dot.bad{--glow:rgba(239,91,91,.6);animation-duration:1.4s}
#headline{transition:opacity .25s}#headline.swap{opacity:0}
.track div{background:linear-gradient(90deg,var(--busy) 0%,#9cc3ff 50%,var(--busy) 100%);background-size:200% 100%;
 animation:shimmer 1.8s linear infinite;transition:width .9s cubic-bezier(.2,.7,.2,1)}
.job{animation:up .45s ease forwards}
.show{opacity:0;animation:up .55s cubic-bezier(.2,.7,.2,1) forwards}
.show .pic{border-radius:8px;transition:transform .35s cubic-bezier(.2,.7,.2,1),box-shadow .35s}
.show:hover .pic{transform:translateY(-6px) scale(1.03);box-shadow:0 14px 32px rgba(0,0,0,.55)}
.show img{opacity:0;transition:opacity .6s}.show img.in{opacity:1}
ul.rows li{animation:up .4s ease both}
ul.rows li:hover{background:rgba(255,255,255,.025)}
details[open] dl,details[open] pre{animation:up .35s ease both}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;opacity:1!important}}
</style></head><body><div class="wrap">
<div class="app reveal"><span>MyAnimeWatcher</span><a class="discover" href="/stremio">Find movies &amp; shows</a></div>
<h1 class="reveal" style="animation-delay:.05s"><span class="dot" id="dot"></span><span id="headline">Loading…</span></h1>
<div class="sub reveal" id="sub" style="animation-delay:.12s"></div>
<div class="job" id="job" hidden><div class="stage"><span id="stage"></span><span id="pct"></span></div><div class="track"><div id="bar"></div></div></div>
<div id="waitingBox" hidden><h2>Waiting</h2><ul class="rows" id="waiting"></ul></div>
<div id="problemsBox" hidden><h2>Needs a look</h2><ul class="rows warn" id="problems"></ul></div>
<h2 class="reveal" style="animation-delay:.18s;display:flex;justify-content:space-between;align-items:baseline">Your shows<a href="/weekly" style="font-size:13px;font-weight:600;color:var(--busy);text-decoration:none">Add or manage shows →</a></h2><div class="shows" id="shows"></div>
<div id="finishedBox" hidden><h2>Recently finished</h2><ul class="rows" id="finished"></ul></div>
<div id="delBox" hidden><h2>Deleting after watching</h2><ul class="rows" id="del"></ul></div>
<details><summary>Details</summary><dl id="sys"></dl><pre id="log"></pre></details>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ago=s=>{s=Math.max(0,s);return s<90?"just now":s<3600?Math.round(s/60)+" min ago":s<86400?Math.round(s/3600)+" h ago":Math.round(s/86400)+" days ago"};
const when=t=>{const d=new Date(t*1000),n=new Date();return (d.toDateString()===n.toDateString()?"today":d.toLocaleDateString([], {weekday:"short",day:"numeric",month:"short"}))+" "+d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})};
const TONE={ok:"var(--ok)",busy:"var(--busy)",warn:"var(--warn)",bad:"var(--bad)"};
const rows=(el,box,items,fmt)=>{$(box).hidden=!items.length;const h=items.map(fmt).join("");if($(el).dataset.h!==h){$(el).dataset.h=h;$(el).innerHTML=h;[...$(el).children].forEach((li,i)=>li.style.animationDelay=(i*.05)+"s")}};
let shown="";
const airDay=t=>new Date(t*1000).toLocaleString([], {weekday:"short",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"});
function countdown(){document.querySelectorAll(".cd").forEach(el=>{let s=Math.round(+el.dataset.at-Date.now()/1000);
 if(s<=0){el.textContent="airing now";el.classList.add("soon");return}
 const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),sec=s%60;
 el.textContent="in "+(d?`${d}d ${h}h ${m}m`:h?`${h}h ${m}m ${sec}s`:`${m}m ${sec}s`);el.classList.toggle("soon",s<86400)})}
setInterval(countdown,1000);
async function tick(){
 let d;try{d=await (await fetch("/api/status",{cache:"no-store"})).json()}catch(e){$("headline").textContent="Status page can't reach the agent";$("dot").style.background="var(--bad)";return}
 const hl=$("headline");
 if(hl.textContent!==d.headline){if(hl.textContent==="Loading…")hl.textContent=d.headline;else{hl.classList.add("swap");setTimeout(()=>{hl.textContent=d.headline;hl.classList.remove("swap")},250)}}
 $("dot").style.background=TONE[d.tone];$("dot").className="dot "+d.tone;
 const f=d.finished[0];$("sub").textContent=f?`Last finished: ${f.name}, ${ago(d.now-f.at)}`:"";
 $("job").hidden=!d.job;
 if(d.job){$("stage").textContent=d.job.stage;$("pct").textContent=Math.round(d.job.pct)+"%";$("bar").style.width=d.job.pct+"%"}
 rows("waiting","waitingBox",d.waiting,x=>`<li><span>${esc(x)}</span><span>waiting for Arabic</span></li>`);
 rows("problems","problemsBox",d.problems,x=>`<li><span>${esc(x)}</span><span></span></li>`);
 rows("finished","finishedBox",d.finished,x=>`<li><span>${esc(x.name)}</span><span>${ago(d.now-x.at)}</span></li>`);
 rows("del","delBox",d.deletions,x=>`<li><span>${esc(x.name)}</span><span>${x.at<=d.now?"within the hour":when(x.at)}</span></li>`);
 const sh=JSON.stringify(d.shows);
 if(sh!==shown){shown=sh;$("shows").innerHTML=d.shows.map((s,i)=>`<div class="show" style="animation-delay:${.22+i*.06}s"><div class="pic">${s.poster?`<img src="${esc(s.poster)}" alt="" onload="this.classList.add('in')">`:'<div class="ph"></div>'}</div>
  <div class="t">${esc(s.title)}</div><div class="m">${s.latest?`Latest ${esc(s.latest)}`:"No episodes yet"}${s.manual?" · Arabic by hand":""}</div>
  ${s.at?`<div class="next"><div class="ep">Ep ${s.next}${s.total?` of ${s.total}`:""} · ${esc(airDay(s.at))}</div><div><span class="cd" data-at="${s.at}"></span>${s.total?`<span class="left"> · ${s.total-s.next+1} left this season</span>`:""}</div></div>`:'<div class="next left">No date announced</div>'}</div>`).join("")}
 countdown();
 $("sys").innerHTML=Object.entries(d.system).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
 const lg=$("log"),bottom=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-8;lg.textContent=d.log.join("\n");if(bottom)lg.scrollTop=lg.scrollHeight;
}
tick();setInterval(tick,3000);
</script></body></html>"""


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
                else:
                    self.send_error(404)
                    return
                code = 200
            except Exception as e:
                data = {"error": str(e) if isinstance(e, (stremio.UserError, shows.ShowError))
                        else "The request could not be completed. Try another source or sync your Stremio add-ons."}
                code = 400
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
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
            if self.path in ("/stremio", "/stremio/", "/weekly", "/weekly/"):
                name = "weekly.html" if self.path.startswith("/weekly") else "stremio.html"
                page = open(os.path.join(HERE, name), encoding="utf-8").read()
                body, ctype = page.replace("__STREMIO_TOKEN__", stremio.TOKEN).encode("utf-8"), "text/html; charset=utf-8"
            elif self.path.startswith("/api/status"):
                try:
                    body = json.dumps(collect(pending), ensure_ascii=False).encode("utf-8")
                except Exception as e:
                    body = json.dumps({"headline": f"Status error: {e!r}", "tone": "bad", "now": time.time(),
                                       "finished": [], "waiting": [], "problems": [], "deletions": [], "shows": [],
                                       "system": {}, "log": [], "job": None}).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            elif self.path in ("/", "/index.html"):
                body, ctype = PAGE.encode("utf-8"), "text/html; charset=utf-8"
            elif self.path in ("/app.png", "/favicon.ico"):
                body, ctype = open(os.path.join(HERE, "app.png"), "rb").read(), "image/png"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
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
