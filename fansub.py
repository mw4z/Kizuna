"""Pick a human Arabic subtitle from the user's Stremio subtitle add-ons (AIOStreams), for any show.

For each episode: ask every subtitle add-on (by season:episode, and by absolute episode number for shows
that subtitle sites index as one long season, e.g. Bleach 206 = TMDB S11E01), then
  * drop machine/AI translations by name, and official/streaming releases for "fansub" shows,
  * rank fansub groups first,
  * download each candidate (text only, through the egress proxy), check it is real Arabic dialogue,
  * check its timing against THIS file's audio (agent.timing) and take the first one that fits.
Nothing acceptable -> None (the caller leaves the episode under "Needs a look"; never a bad fallback).

Per-show rules live in subtitle_policy.json next to this file:
  mode "fansub"      human fan translation only (no Shahid/Netflix/... and no machine translation)
  mode "fansub_first" fan translation first; only when no fansub fits anywhere, official human Arabic (level 2)
  mode "official_ok" official streaming Arabic is fine (weekly shows); machine translation still rejected
  mode "manual"      never automatic (Detective Conan is typeset by hand)
"""
import json
import os
import re
import shutil
import subprocess

import agent

HERE = os.path.dirname(os.path.abspath(__file__))
POLICY_FILE = os.path.join(HERE, "subtitle_policy.json")
DEFAULT_POLICY = {
    "default": "official_ok",
    "shows": [
        {"name": "Bleach", "match": r"\bBleach\b|المطهر|بليتش", "imdb": "tt0434665", "mode": "fansub_first"},
        {"name": "Detective Conan", "match": r"Detective Conan|Case Closed|المحقق كونان", "imdb": "tt0131179",
         "mode": "manual"},
    ],
}
OFFICIAL = re.compile(r"shahid|شاهد|netflix|\bNF\b|crunchyroll|\bCR\b|amazon|\bAMZN\b|prime ?video|\bOSN\b|disney|"
                      r"\bDSNP\b|hidive|anghami|starz|\bTOD\b|weyyak|yango|\bviu\b|iqiyi|\bWAKANIM\b", re.I)
MACHINE = re.compile(r"\bAI\b|machine|auto[ ._-]?trans|google|deepl|chat ?gpt|\bGPT\b|\bMT\b|ترجمة آلية|آلياً?\b|مترجم آلي",
                     re.I)
FANSUB = re.compile(r"kurai|mangetsu|pirates|3asq|عاشق|arabic ?anime ?team|anime ?island|witanime|anime ?4 ?up|"
                    r"blkom|fansub|فانساب|ترجمة احترافية|فريق", re.I)


def policy():
    try:
        return json.load(open(POLICY_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_POLICY


def mode_for(name="", imdb=""):
    import shows
    chosen = shows.policy_for(name)               # set per show on the Weekly shows page
    if chosen:
        return chosen
    p = policy()
    for show in p.get("shows", []):
        if (imdb and imdb == show.get("imdb")) or (name and re.search(show.get("match", "^$"), name, re.I)):
            return show.get("mode", p.get("default", "official_ok"))
    return p.get("default", "official_ok")


def imdb_for(name):
    """IMDb id pinned for a show in the policy (skips the name search), else None."""
    for show in policy().get("shows", []):
        # whole-title match only: "Bleach" pins tt0434665, "Bleach: Thousand-Year Blood War" does not
        if show.get("imdb") and name and re.fullmatch(f"(?:{show.get('match', '^$')})", name.strip(), re.I):
            return show["imdb"]
    return None


def release_name(sub):
    """add-on subtitle ids look like 'v3+|13208864|[Pirates] Bleach EP 001' -> '[Pirates] Bleach EP 001'."""
    return str(sub.get("id", "")).split("|")[-1].strip()


def ranked(subs, mode):
    """Arabic candidates allowed by the policy, best first: [(score, name, sub)]."""
    out = []
    for sub in subs:
        if not str(sub.get("lang", "")).lower().startswith(("ar", "ara")) or not sub.get("url"):
            continue
        name = release_name(sub)
        if MACHINE.search(name):
            continue
        official = bool(OFFICIAL.search(name))
        if official and mode == "fansub":
            continue
        score = 3 if FANSUB.search(name) else (2 if official else 1)   # official only reachable in official_ok
        out.append((score, name, sub))
    seen, unique = set(), []
    for score, name, sub in sorted(out, key=lambda x: -x[0]):           # stable: keeps add-on order inside a score
        if sub["url"] not in seen:
            seen.add(sub["url"])
            unique.append((score, name, sub))
    return unique


def episode_ids(meta_id, season, episode):
    """Video ids to ask for: TMDB-style season:episode, then the absolute episode as season 1 (Bleach-style)."""
    ids = [f"{meta_id}:{season}:{episode}"]
    import stremio as s
    try:
        meta = s.get_json(f"https://v3-cinemeta.strem.io/meta/series/{meta_id}.json", timeout=20)["meta"]
        eps = sorted((v["season"], v["episode"]) for v in meta.get("videos", []) if v.get("season", 0) > 0)
        absolute = eps.index((season, episode)) + 1
        if (season, episode) != (1, absolute):
            ids.append(f"{meta_id}:1:{absolute}")
    except (Exception,):                         # no Cinemeta data: season:episode only
        pass
    return ids


def text_ok(path):
    """Real Arabic dialogue: enough cues, mostly Arabic letters. -> (ok, reason)."""
    try:
        t = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return False, "unreadable"
    cues = t.count("-->")
    letters = re.findall(r"[^\W\d_]", t)
    arabic = sum(1 for c in letters if "؀" <= c <= "ۿ")
    if cues < 100:
        return False, f"only {cues} lines"
    if not letters or arabic / len(letters) < 0.6:
        return False, "not mostly Arabic"
    if MACHINE.search(t[:3000]) and re.search(r"ترجم|translat", t[:3000], re.I):
        return False, "credits mention machine translation"
    return True, ""


MARKS = re.compile(r"<[^>]+>|\{[^}]*\}|[‎‏‪-‮⁦-⁩؜]|[^\w\s]")


def normalized_lines(path):
    """Arabic dialogue lines with tags, direction marks and punctuation removed (for copy detection)."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return set()
    out = set()
    for line in text.splitlines():
        if re.search(r"[؀-ۿ]", line):
            n = re.sub(r"\s+", " ", MARKS.sub("", line)).strip()
            if len(n) > 6:
                out.add(n)
    return out


def official_lines(video, work):
    """Lines of the Arabic subtitle tracks that came inside the file (official releases), except our own."""
    lines = set()
    try:
        for t in agent.tracks(video):
            p = t.get("properties", {})
            # every Arabic track that came with the release (official subs are often ASS too, e.g. DKB/Crunchyroll);
            # files carrying a styled fansub never reach the picker (library_processing leaves them untouched)
            if (t.get("type") == "subtitles" and str(p.get("language", "")).lower() in ("ara", "ar")
                    and p.get("track_name") != "العربية"):
                out = os.path.join(work, f"official{t['id']}.srt")
                subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", video, "-map", f"0:{t['id']}",
                                "-c:s", "srt", out], timeout=120, capture_output=True, creationflags=agent.NO_CF)
                lines |= normalized_lines(out)
    except Exception:
        pass
    return lines


def copies_official(candidate, official):
    """True when most of the candidate's lines are the official subtitle's lines (a re-upload)."""
    mine = normalized_lines(candidate)
    return bool(mine and official) and len(mine & official) / len(mine) > 0.5


def download(sub, out):
    import stremio as s
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-protocol_whitelist", s.egress.PROTOCOLS,
                        "-rw_timeout", "30000000", "-i", s.safe_url(sub["url"]), "-map", "0:s:0", "-c:s", "srt", out],
                       timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=agent.NO_CF, env=s.egress.ffmpeg_env())
    return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000


def pick(video, meta_id, season, episode, destination, mode, kind="series"):
    """-> chosen release name (subtitle written to destination as SRT), or None. kind: "series" or "movie"."""
    import srt2ass
    import stremio as s
    if mode == "manual":
        return None
    work = os.path.join(os.path.dirname(os.path.abspath(destination)), "fansub-candidates")
    os.makedirs(work, exist_ok=True)
    subs = []
    ids = [meta_id] if kind == "movie" else episode_ids(meta_id, season, episode)   # both numberings
    for vid in ids:                                       # ranked() drops duplicate links
        results, _ = s.parallel([(s.resource, (a, "subtitles", kind, vid, {})) for a in s.addons()
                                 if s.supports(a, "subtitles", kind, vid)], timeout=20)
        subs += [x for r in results for x in (r.get("subtitles") or [])]
    label = meta_id if kind == "movie" else f"{meta_id} S{season:02d}E{episode:02d}"
    official = official_lines(video, work) if mode in ("fansub", "fansub_first") else set()
    first = ranked(subs, "fansub" if mode == "fansub_first" else mode)
    queue = [(c, 1) for c in first]
    if mode == "fansub_first":                   # level 2: official human Arabic, only after every fansub failed
        queue += [(c, 2) for c in ranked(subs, "official_ok") if c not in first]
    tried = 0
    for (score, name, sub), level in queue:      # the queue may grow: official re-uploads move to level 2
        tried += 1
        cand = os.path.join(work, f"c{tried}.srt")
        if not download(sub, cand):
            agent.log(f"fansub: {name}: download failed")
            continue
        ok, why = text_ok(cand)
        if ok and level == 1 and copies_official(cand, official):
            ok, why = False, "a re-upload of the official subtitle"
            if mode == "fansub_first":
                queue.append(((score, name, sub), 2))
        if not ok:
            agent.log(f"fansub: {name}: rejected ({why})")
            continue
        ass = os.path.join(work, f"c{tried}.ass")
        srt2ass.convert(cand, ass, name)
        try:
            off, fits = agent.timing(video, ass)
        except Exception as e:                       # undecodable audio etc.
            agent.log(f"fansub: {name}: timing check failed ({type(e).__name__})")
            continue
        if not fits:
            agent.log(f"fansub: {name}: timing does not fit this file")
            continue
        shutil.copy2(cand, destination)
        agent.log(f"fansub: chose {name} ({'fansub' if level == 1 else 'level 2 official'}, rank {score}, "
                  f"offset {off:+.2f}s) for {label}")
        shutil.rmtree(work, ignore_errors=True)
        return name
    shutil.rmtree(work, ignore_errors=True)
    agent.log(f"fansub: no acceptable Arabic for {label} ({tried} candidates)")
    return None
