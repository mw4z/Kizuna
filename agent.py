"""Kizuna agent: give every new episode in the anime folder styled Arabic subs, unattended.

Started by watcher.py when a new file lands in the folder. For each .mkv without our "العربية" track:
  wait until the file is stable -> official Arabic SRT from the file (or, if missing,
  from a Judas/ToonsHub/VARYG multi-sub release on Nyaa) -> Cairo ASS without the
  translator credit -> timing checked against the episode's own audio (a consistent
  offset is corrected; an inconsistent one blocks the mux) -> mux in place -> verify
  the embedded track -> Windows notification. Conan is skipped (needs hand typesetting).
Log: log.txt next to this file. Usage: agent.py [folder] [--now]   (--now: caller already confirmed the files are complete)
"""
from collections.abc import Mapping
import contextlib
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import audio_fit  # noqa: E402
import mux        # noqa: E402
import srt2ass    # noqa: E402

NOW = "--now" in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
# The watcher passes the ONE file it saw settle; then only that file is processed (audit #1: --now used to
# mark every .mkv in the folder ready, so a second episode still syncing got muxed mid-write).
TARGET = os.path.abspath(ARGS[0]) if ARGS and os.path.isfile(ARGS[0]) else None
FOLDER = os.path.dirname(TARGET) if TARGET else (ARGS[0] if ARGS else r"C:\Users\moody\OneDrive\Videos\أنمي\آخر حلقات الأنمي")
MKV = r"C:\Program Files\MKVToolNix"
WORK = os.path.join(HERE, "work")
STATE = os.path.join(HERE, "state.json")
LOG = os.path.join(HERE, "log.txt")
GROUPS = ("[Judas]", "[ToonsHub]", "VARYG", "[Trix]")   # multi-sub releases that carry NF/DSNP/CR Arabic
NO_CF = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


ALERTS = {"Kizuna: ready": "New episode ready", "Kizuna: movie ready": "New movie ready"}


def notify(title, text):
    if title not in ALERTS:
        log(f"(silent) {title}: {text}")
        return
    try:
        import settings
        quiet = not settings.get("notify_ready")
    except Exception:
        quiet = False
    if quiet:                                   # turned off on the Settings page
        log(f"(notifications off) {title}: {text}")
        return
    log(f"NOTIFY {title}: {text}")
    title = ALERTS[title]
    ps = ("[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
          "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
          "$t=$x.GetElementsByTagName('text');$t.Item(0).AppendChild($x.CreateTextNode($env:T1))>$null;"
          "$t.Item(1).AppendChild($x.CreateTextNode($env:T2))>$null;"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('MyAnimeWatcher').Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env={**os.environ, "T1": title, "T2": text},
                   capture_output=True, creationflags=NO_CF)


PFILE = os.path.join(HERE, "progress.json")


def write_atomic(path, text, tries=40):
    """Write via a private temp file, then swap it in, so readers never see half a file. Windows refuses the swap
    while another process/thread has the target open (the status page polls these files every few seconds), so
    retry for up to ~2 s; then give up with the original error and leave the old file intact."""
    path = str(path)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == tries - 1:
                with contextlib.suppress(OSError):
                    os.remove(tmp)
                raise
            time.sleep(0.05)


def show(episode, stage, pct, status="run"):
    """Record live progress in progress.json for the status page (no pop-up window: user asked for none).
    Cosmetic: a failed write must never fail (or be mistaken for a locked episode in) the real work."""
    try:
        write_atomic(PFILE, json.dumps({"episode": episode, "stage": stage, "pct": pct, "status": status,
                                        "t": time.time()}, ensure_ascii=False))
    except OSError:
        pass


def run(*a, **k):
    return subprocess.run(a, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_CF, **k)


def tracks(path):
    return mux.info(path)["tracks"]


def has_arabic_audio(track_list):
    for track in track_list:
        if track.get("type") != "audio":
            continue
        p = track.get("properties", {})
        name = p.get("track_name", "")
        if p.get("flag_commentary") or re.search(r"commentary|تعليق", name, re.I):
            continue
        languages = (p.get("language", ""), p.get("language_ietf", ""))
        if any(str(lang).lower().split("-")[0] in ("ar", "ara") for lang in languages) \
                or re.match(r"^(?:Arabic|العربية|عربي)(?:$|[\s(/-])", name, re.I):
            return True
    return False


def arabic_track(path, include_named=False):
    """id of an Arabic text subtitle track that is not ours, else None."""
    for t in tracks(path):
        p = t["properties"]
        if t["type"] == "subtitles" and str(p.get("language_ietf") or p.get("language", "")).lower().split("-")[0] in ("ar", "ara") and (include_named or p.get("track_name") != "العربية") \
                and not p.get("forced_track") and not re.search(r"forced|signs|songs", p.get("track_name", ""), re.I) \
                and t["codec"] in ("SubRip/SRT", "SubStationAlpha", "WebVTT"):
            return t["id"]
    return None


def extract_srt(path, tid, out):
    codec = [t["codec"] for t in tracks(path) if t["id"] == tid][0]
    if codec == "SubRip/SRT":                       # byte-exact, keeps {\an8} and RTL marks
        r = run(MKV + r"\mkvextract.exe", path, "tracks", f"{tid}:{out}")
    else:
        r = run("ffmpeg", "-v", "error", "-y", "-i", path, "-map", f"0:{tid}", "-c:s", "srt", out)
    return r.returncode == 0 and os.path.getsize(out) > 1000


def parse_name(fn):
    """'[DKB] Ao no Hako - S02E04 [...]' -> ('Ao no Hako', 'S02E04')"""
    m = re.search(r"^(?:\[[^\]]*\]\s*)?(.+?)\s*-\s*(S\d+E\d+)", fn)
    if not m:
        m = re.search(r"^(?:\[[^\]]*\]\s*)?(.+?)[ ._-]+(S\d+E\d+)", fn, re.I)
    return (m.group(1).strip(), m.group(2).upper()) if m else (None, None)


# Jellyfin "Weekly Anime" library: finished episodes are copied to <show folder>\Season NN (OneDrive copy stays).
LIBRARY = r"C:\Users\moody\Downloads\Weekly Anime"
class _Shows(Mapping):
    """Release name prefix -> Jellyfin folder (English title + year, for TMDB metadata), read live from the private
    shows.json that the Weekly shows page edits (no restart needed)."""
    def _data(self):
        import shows
        return shows.folders()

    def __getitem__(self, key):
        return self._data()[key]

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())


SHOWS = _Shows()


JELLYFIN_DB = r"C:\ProgramData\Jellyfin\Server\data\jellyfin.db"


@contextlib.contextmanager
def jellyfin_db():
    """Read-only connection to a temporary COPY of Jellyfin's database (db + pending -wal).

    Never open the live file: even a mode=ro reader recreates jellyfin.db-shm/-wal when they are missing,
    owned by this user, and the Jellyfin service (NetworkService) then can't write -> "attempt to write a
    readonly database", failed library scans and lost watched state (2026-11-16)."""
    tmp = tempfile.mkdtemp(prefix="jfdb_")
    try:
        for suffix in ("", "-wal"):
            if os.path.exists(JELLYFIN_DB + suffix):
                shutil.copy2(JELLYFIN_DB + suffix, os.path.join(tmp, "jellyfin.db" + suffix))
        c = sqlite3.connect(os.path.join(tmp, "jellyfin.db"), timeout=10)
        try:
            yield c
        finally:
            c.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def library_target(fn):
    """'[DKB] Ao no Hako - S02E05 [..].mkv' -> LIBRARY\\Blue Box (2024)\\Season 02\\<fn>; None if unparsable."""
    name, ep = parse_name(fn)
    if not name:
        return None
    show = next((v for k, v in SHOWS.items() if name.startswith(k)), re.sub(r'[<>:"/\\|?*]', "", name))
    return os.path.join(LIBRARY, show, f"Season {int(ep[1:ep.index('E')]):02d}", fn)


def copy_to_library(video):
    """True when the episode is in the library (or has no library place); False when the copy failed.
    Never raises: a failed copy must not undo the Arabic work, but it must be visible and retried (audit #4)."""
    dst = None
    try:
        dst = library_target(os.path.basename(video))
        if not dst or os.path.exists(dst):
            return True
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(video, dst + ".part")      # .part: Jellyfin never scans a half-copied file
        os.replace(dst + ".part", dst)
        log(f"library: copied to {dst}")
        return True
    except Exception as e:
        log(f"library: copy failed for {os.path.basename(video)}: {e!r}")
        if dst:
            try:
                os.remove(dst + ".part")
            except OSError:
                pass
        return False


def record_copy(video, state, ok):
    """Remember a failed library copy so the watcher retries it and the status page shows it."""
    key = os.path.basename(video)
    if ok:
        state.get("copy_failed", {}).pop(key, None)
    else:
        state.setdefault("copy_failed", {})[key] = os.path.getsize(video)


LABEL = ""


def nyaa_arabic(fn, out):
    """Fetch the episode from a multi-sub group on Nyaa just for its Arabic track."""
    name, ep = parse_name(fn)
    if not name:
        name = re.sub(r"\[[^\]]*\]", "", os.path.splitext(fn)[0]).strip(" .-")
        if len(name) < 3:
            return False
    base = re.sub(r"\s*\(\d{4}\)", "", name).split(":")[0].strip()
    words = base.split()
    names = [name] + [" ".join(words[:k]) for k in range(len(words), 1, -1)]   # groups name shows differently
    for q in dict.fromkeys(f"{n} {ep or ''}".strip() for n in (names if ep else [name])):
        url = "https://nyaa.si/?page=rss&c=1_2&q=" + urllib.parse.quote(q)
        try:
            rss = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60).read().decode()
        except Exception as e:
            log(f"nyaa search failed: {e}")
            return False
        items = re.findall(r"<title>(.*?)</title>\s*<link>(https://nyaa.si/download/\d+\.torrent)</link>", rss)
        items = [(t, l) for t, l in items if any(g in t for g in GROUPS) and (not ep or ep in t) and "1080p" in t
                 and (ep or all(word.casefold() in t.casefold() for word in re.findall(r"\w{3,}", name)))]
        items.sort(key=lambda x: [g in x[0] for g in GROUPS].index(True))
        for title, link in items[:3]:
            d = os.path.join(WORK, "dl")
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d)
            tf = os.path.join(d, "t.torrent")
            urllib.request.urlretrieve(link, tf)
            log(f"downloading {title}")
            pr = subprocess.Popen([sys.executable, os.path.join(HERE, "torrent.py"), tf, d, "40", r"\.mkv$"],
                                  stdout=subprocess.PIPE, text=True, errors="replace", creationflags=NO_CF)
            for ln in pr.stdout:
                m = re.search(r"([\d.]+)%.*?([\d.]+) MB/s", ln)
                if m and LABEL:
                    show(LABEL, f"No Arabic in this release, fetching it from {title.split(']')[0].strip('[')} ({m.group(2)} MB/s)",
                         10 + float(m.group(1)) * 0.4)
            pr.wait()
            for f in glob.glob(os.path.join(d, "**", "*.mkv"), recursive=True):
                tid = arabic_track(f)
                ok = tid is not None and extract_srt(f, tid, out)
                if ok:
                    log(f"Arabic taken from {title}")
                    shutil.rmtree(d, ignore_errors=True)
                    return True
            shutil.rmtree(d, ignore_errors=True)
    return False


def addon_arabic(fn, video, out):
    """Human Arabic from the user's Stremio subtitle add-ons (fansub.pick: show policy, no machine translation,
    timing checked against this file), when no official release carries Arabic. -> True when written to out."""
    name, ep = parse_name(fn)
    if not name:
        return False
    folder = next((v for k, v in SHOWS.items() if name.startswith(k)), name)   # clean English title for Stremio
    title = re.sub(r"\s*\(\d{4}\)$", "", folder)
    season, episode = map(int, re.findall(r"\d+", ep))
    try:
        import fansub
        import processing_worker
        return bool(processing_worker.fetch_arabic({"type": "series", "season": season, "episode": episode,
                                                    "meta": {"name": title}}, out, fansub.mode_for(title), video))
    except Exception as error:
        log(f"fansub: add-on subtitles unavailable ({type(error).__name__})")
        return False


def timing(video, ass):
    """-> (offset to apply, ok). Song-only windows give junk, so trust the consensus of windows."""
    r = audio_fit.fit(video, ass)
    windows = [(float(x["offset"]), float(x.get("strength", 0))) for x in r["windows"]]
    w = [o for o, _ in windows]
    g = float(r["global"])
    log(f"timing windows {w} global {g} strength {float(r.get('strength', 0)):.1f}")
    # Silence / undecodable audio / weak overall fit: never confirm or change timing.
    if float(r.get("strength", 0)) < 3:
        return 0.0, False
    # Agreement uses EVERY window: per-window scores are naturally low on quiet shows (Black Clover's
    # correct 0.0 windows score 2-3), so filtering them first blocked well-timed episodes.
    # Biggest group within 0.3 s of each other; OP/ED/preview songs give scattered junk.
    good = max(([o for o in w if abs(o - c) <= 0.3] for c in w), key=len, default=[])
    off = statistics.median(good) if good else 0.0   # measured lag is what must be added (No Escape: +2.4 -> +2.35)
    if len(good) < 3 or abs(g - off) > 0.3:          # the fit over all dialogue must confirm the group
        return 0.0, False
    if abs(off) <= 0.25:
        return 0.0, True                             # already in sync: nothing to change
    # Shifting subtitles needs strong evidence: at least 3 confidently measured windows agreeing on it.
    if sum(1 for o, s in windows if s >= 3 and abs(o - off) <= 0.3) < 3:
        return 0.0, False
    return off, True


def cue_starts(srt):
    t = open(srt, encoding="utf-8-sig", errors="replace").read()
    return sorted(int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
                  for h, m, s, ms in re.findall(r"(\d+):(\d\d):(\d\d)[,.](\d{3}) -->", t))


def cue_shift(reference, srt, parts=6):
    """Offset that lines `srt` up with a reference subtitle timed to the same video (cue starts follow the same
    scene changes). Measured per section; -> median offset when >= 4 sections agree within 0.3 s, else None."""
    import bisect
    ref, cues = cue_starts(reference), cue_starts(srt)
    if len(ref) < 100 or len(cues) < 100:
        return None
    end = max(ref[-1], cues[-1])
    found = []
    for i in range(parts):
        part = [c for c in cues if end * i / parts <= c < end * (i + 1) / parts]
        if len(part) < 30:
            continue

        def hit(shift):
            n = 0
            for c in part:
                j = bisect.bisect_left(ref, c + shift)
                if any(0 <= k < len(ref) and abs(ref[k] - c - shift) < 0.1 for k in (j - 1, j)):
                    n += 1
            return n / len(part)
        score, shift = max((hit(s / 20), s / 20) for s in range(-300, 301))   # +-15 s in 0.05 s steps
        if score >= 0.35:
            found.append(shift)
    if len(found) < 4:
        return None
    found.sort()
    best = max((found[i:i + k] for k in range(len(found), 3, -1) for i in range(len(found) - k + 1)
                if found[i + k - 1] - found[i] <= 0.3), key=len, default=[])
    return round(statistics.median(best), 2) if len(best) >= 4 else None


def reference_offset(video, srt):
    """cue_shift() against the video's own non-Arabic text subtitle track (Collateral: English). -> offset or None."""
    for t in tracks(video):
        p = t.get("properties", {})
        if t["type"] == "subtitles" and str(p.get("codec_id", "")).startswith("S_TEXT") and p.get("language") != "ara":
            ref = os.path.join(WORK, "reference.srt")
            if extract_srt(video, t["id"], ref):
                shift = cue_shift(ref, srt)
                if shift is not None:             # a short Signs/Songs track says nothing; try the next track
                    return shift
    return None


def qc(ass):
    t = open(ass, encoding="utf-8-sig").read()
    n = t.count("\nDialogue:")
    bad = []
    if n < 100:
        bad.append(f"only {n} lines")
    if re.search("[\x00-\x08\x0b-\x1f]", t):
        bad.append("control characters")
    if re.search(r"</?(font|i|b|u)\b[^>]*>", t):
        bad.append("HTML tags left")
    if re.search(r"\{[^}]*\}\s*\u200f?(ترجمة|تمت الترجمة)", t):
        bad.append("credit line left")
    return bad


def process(video, state, *, publish=True, subtitle_path=None, search_name=None, addons=True):
    """addons=False: the caller already asked the subtitle add-ons for this file (processing_worker)."""
    fn = os.path.basename(video)
    key = fn
    if has_arabic_audio(tracks(video)):
        if publish:
            record_copy(video, state, copy_to_library(video))
        state["errors"].pop(key, None)
        state["failed"].pop(key, None)
        log(f"SKIP subtitles {fn}: Arabic audio already present; video unchanged")
        show(fn, "Arabic dub already present. Subtitle processing skipped", 100, "done")
        return True
    global LABEL
    srt = os.path.join(WORK, "ar.srt")
    ass = os.path.join(WORK, "ar.ass")
    show_, ep_ = parse_name(fn)
    LABEL = f"{show_} {ep_}" if show_ else fn
    tid = arabic_track(video)
    if subtitle_path:
        shutil.copy2(subtitle_path, srt)
    if not subtitle_path and tid is None and time.time() - state["nyaa_tries"].get(key, 0) < 3600:
        # Nyaa retry at most hourly; say so instead of leaving a stale "working" state (audit #10)
        show(LABEL, "Waiting for the official Arabic subtitle; checking again within the hour", 100, "wait")
        return
    show(LABEL, "Looking for the official Arabic subtitle", 5)
    if subtitle_path:
        pass
    elif tid is not None:
        if not extract_srt(video, tid, srt):
            raise RuntimeError("could not extract Arabic track")
    else:
        state["nyaa_tries"][key] = time.time()
        if not nyaa_arabic(search_name or fn, srt) and not (addons and addon_arabic(search_name or fn, video, srt)):
            show(LABEL, "No official Arabic released yet. Will retry hourly", 100, "fail")
            if not state["told"].get(key + "|noarabic"):
                notify("Kizuna: waiting", f"No official Arabic for {fn[:60]} yet. Will keep checking hourly.")
                state["told"][key + "|noarabic"] = 1
            return
    title = f"{show_} {ep_} - Arabic" if show_ else fn
    show(LABEL, "Converting to Cairo style, removing credits", 55)
    srt2ass.convert(srt, ass, title)
    show(LABEL, "Checking timing against the episode audio", 62)
    off, ok = timing(video, ass)
    if not ok:
        # Audio alone was unsure (Collateral: 5 of 6 windows said +2.35 but the fit was weak). If the video has its
        # own subtitle track, line up against it, then let the audio check confirm the result is in sync.
        ref = reference_offset(video, srt)
        if ref is not None:
            srt2ass.convert(srt, ass, title, ref)
            off2, ok2 = timing(video, ass)
            log(f"reference-track offset {ref:+.2f}s; audio re-check {'confirms' if ok2 and not off2 else 'rejects'} it")
            if ok2 and not off2:
                off, ok = ref, True
    if not ok:
        show(LABEL, "Timing inconsistent. Not muxed, needs a look", 100, "fail")
        notify("Kizuna: needs a look", f"Timing of {fn[:60]} is inconsistent. Not muxed.")
        state["told"][key + "|timing"] = 1
        state["failed"][key] = os.path.getsize(video)
        return
    if off:
        srt2ass.convert(srt, ass, title, off)
        log(f"applied offset {off:+.2f}s")
    bad = qc(ass)
    if bad:
        show(LABEL, "Quality check failed. Not muxed, needs a look", 100, "fail")
        notify("Kizuna: needs a look", f"{fn[:60]}: {', '.join(bad)}. Not muxed.")
        state["failed"][key] = os.path.getsize(video)
        return
    need = os.path.getsize(video) * 1.05 + 500e6
    if shutil.disk_usage(FOLDER).free < need:    # the .tmp copy needs room next to the original
        raise RuntimeError(f"not enough disk space ({shutil.disk_usage(FOLDER).free / 1e9:.1f} GB free)")
    state.setdefault("delivery", {})[key] = dialogue_hash(ass)
    save_state(state)  # Journal before the in-place swap so restart can verify and finish delivery.
    dl = lambda p: [l for l in open(p, encoding="utf-8-sig").read().splitlines() if l.startswith("Dialogue")]  # noqa: E731

    def embedded_ok(path):                        # the built file carries exactly our dialogue, before it replaces anything
        tid = [t["id"] for t in tracks(path) if t["properties"].get("track_name") == "العربية"][0]
        emb = os.path.join(WORK, "emb.ass")
        with contextlib.suppress(FileNotFoundError):
            os.remove(emb)
        r = run(MKV + r"\mkvextract.exe", path, "tracks", f"{tid}:{emb}")
        return r.returncode in (0, 1) and os.path.exists(emb) and dl(emb) == dl(ass)   # 1 = warnings only

    remove_stale_tmp(video)                       # only a .tmp this run built and verified may ever be swapped in
    verified = None
    for i in range(120):                          # player / OneDrive may hold the file
        try:
            if verified:
                os.replace(verified, video)
                res = "ok (after wait)"
                break
            show(LABEL, "Adding the Arabic track to the episode", 70)
            res = mux.mux(video, ass, lambda p: show(LABEL, "Adding the Arabic track to the episode", 70 + p * 0.25),
                          check=embedded_ok)
            break
        except mux.Locked as e:
            verified = e.tmp
        except mux.VerifyFailed:
            show(LABEL, "Verification failed, needs a look", 100, "fail")
            notify("Kizuna: needs a look", f"{fn[:60]}: built file failed verification; original untouched.")
            state["failed"][key] = os.path.getsize(video)
            state["errors"].pop(key, None)
            return
        except PermissionError:                   # source locked while reading, or the verified swap refused again
            if not verified:
                remove_stale_tmp(video)
        time.sleep(15)
    else:
        remove_stale_tmp(video)
        log(f"{fn}: file locked for 30 min, will retry next run")
        return
    show(LABEL, "Verifying the finished file", 97)
    tid = [t["id"] for t in tracks(video) if t["properties"].get("track_name") == "العربية"][0]
    emb = os.path.join(WORK, "emb.ass")
    run(MKV + r"\mkvextract.exe", video, "tracks", f"{tid}:{emb}")
    if dl(emb) != dl(ass):
        show(LABEL, "Verification failed, needs a look", 100, "fail")
        notify("Kizuna: needs a look", f"{fn[:60]}: embedded track differs from the built one.")
        state["failed"][key] = os.path.getsize(video)   # visible under "Needs a look" (audit #3)
        state["errors"].pop(key, None)
        return
    n = len(dl(ass))
    state["errors"].pop(key, None)
    log(f"DONE {fn}: {res}; {n} lines; offset {off:+.2f}s")
    if not publish:
        state["delivery"].pop(key, None)
        show(LABEL, f"Arabic verified ({n} lines), ready for library delivery", 100, "done")
        return True
    copied = copy_to_library(video)
    record_copy(video, state, copied)
    if copied:
        state["delivery"].pop(key, None)
        show(LABEL, f"Done. Arabic added ({n} lines), ready to watch", 100, "done")
        notify("Kizuna: ready", f"{LABEL[:50]}: Arabic added ({n} lines).")
    else:
        show(LABEL, "Arabic added, but copying to Jellyfin failed. Retrying", 100, "fail")
        notify("Kizuna: needs a look", f"{fn[:60]}: Arabic added, Jellyfin copy failed; retrying.")


def dialogue_hash(path):
    with open(path, encoding="utf-8-sig") as f:
        lines = [line for line in f.read().splitlines() if line.startswith("Dialogue:")]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def retry_copy(video, video_tracks, state):
    """A finished episode whose library copy failed earlier: copy again; the ready alert only fires now."""
    fn = os.path.basename(video)
    expected = state.get("delivery", {}).get(fn)
    if expected:
        tid = next(t["id"] for t in video_tracks if t["type"] == "subtitles" and t["properties"].get("track_name") == "العربية")
        embedded = os.path.join(WORK, "recovered.ass")
        result = run(MKV + r"\mkvextract.exe", video, "tracks", f"{tid}:{embedded}")
        if result.returncode or dialogue_hash(embedded) != expected:
            raise RuntimeError("Recovered subtitle failed verification; not copied to Jellyfin")
    if not copy_to_library(video):
        log(f"library: copy still failing for {fn}, will retry")
        return
    record_copy(video, state, True)
    state.get("delivery", {}).pop(fn, None)
    show_, ep_ = parse_name(fn)
    label = f"{show_} {ep_}" if show_ else fn
    log(f"library: copy recovered for {fn}")
    show(label, "Done. Copied to Jellyfin, ready to watch", 100, "done")
    if not has_arabic_audio(video_tracks):            # dubs never had a ready alert
        notify("Kizuna: ready", f"{label[:50]}: Arabic added.")


def save_state(state):
    write_atomic(STATE, json.dumps(state, ensure_ascii=False, indent=1))   # a crash never leaves half a JSON file


def load_state():
    try:
        state = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        state = {}
    for k in ("sizes", "nyaa_tries", "told", "failed", "errors", "copy_failed", "delivery"):
        state.setdefault(k, {})
    return state


def lock_held(lock):
    """True if another agent is really running (a lock left by a crash is ignored)."""
    import psutil
    try:
        pid = int(open(lock).read() or 0)
        return psutil.pid_exists(pid) and "agent.py" in " ".join(psutil.Process(pid).cmdline())
    except Exception:
        return False


def remove_stale_tmp(video):
    tmp = video[:-4] + ".tmp.mkv"
    try:
        os.remove(tmp)
        log(f"removed unverified {os.path.basename(tmp)}")
    except FileNotFoundError:
        pass
    except OSError as e:          # e.g. antivirus still scanning it: harmless, it is never swapped in unverified
        log(f"could not remove {os.path.basename(tmp)} yet ({type(e).__name__})")


def clean_tmp():
    """remove .tmp.mkv leftovers of an interrupted mux (the original is never touched until the swap), and
    write_atomic temp files of processes killed mid-write."""
    for t in glob.glob(os.path.join(HERE, "*.json.*.tmp")):
        with contextlib.suppress(OSError):
            if time.time() - os.path.getmtime(t) > 3600:
                os.remove(t)
    for t in glob.glob(os.path.join(FOLDER, "*.tmp.mkv")):
        if os.path.exists(t.replace(".tmp.mkv", ".mkv")) and time.time() - os.path.getmtime(t) > 3600:
            try:
                os.remove(t)
                log(f"removed leftover {os.path.basename(t)}")
            except OSError:
                pass


def main():
    import fansub                                # per-show policy (manual shows are typeset by hand)
    os.makedirs(WORK, exist_ok=True)
    lock = os.path.join(HERE, "running.lock")
    if lock_held(lock):
        return
    with open(lock, "w") as f:
        f.write(str(os.getpid()))
    try:
        clean_tmp()
        state = load_state()
        for video in [TARGET] if TARGET else sorted(glob.glob(os.path.join(FOLDER, "*.mkv"))):
            fn = os.path.basename(video)
            if ".tmp." in fn:
                continue
            size = os.path.getsize(video)
            stable = NOW or state["sizes"].get(fn) == size and time.time() - os.path.getmtime(video) > 120
            state["sizes"][fn] = size
            if not stable:
                continue
            try:
                video_tracks = tracks(video)
                if fn in state.get("copy_failed", {}):
                    retry_copy(video, video_tracks, state)
                    save_state(state)
                    continue
                dubbed = has_arabic_audio(video_tracks)
                if not dubbed and state["failed"].get(fn) == size:
                    continue
                if not dubbed and any(t["properties"].get("track_name") == "العربية" for t in video_tracks):
                    destination = library_target(fn)
                    if fn in state.get("delivery", {}) or destination and not os.path.exists(destination):
                        retry_copy(video, video_tracks, state)
                        save_state(state)
                    continue
                if not dubbed and fansub.mode_for(parse_name(fn)[0] or fn) == "manual":
                    if not state["told"].get(fn + "|conan"):   # key kept: existing state stays valid
                        notify("Kizuna: manual", f"{fn[:60]} needs hand typesetting. Ask Claude.")
                        state["told"][fn + "|conan"] = 1
                    continue
                log(f"processing {fn}")
                process(video, state)
            except Exception as e:                   # transient (lock, disk, network) -> retried; 3 strikes -> stop
                n = state["errors"][fn] = state["errors"].get(fn, 0) + 1
                log(f"ERROR {fn} (attempt {n}/3): {e!r}")
                if n >= 3:
                    state["failed"][fn] = size
                    state["errors"].pop(fn, None)   # a replaced file starts again with 3 tries (audit #5)
                    show(fn[:60], f"Failed 3 times: {e}"[:80], 100, "fail")
                    notify("Kizuna: error", f"{fn[:60]}: {e}"[:200])
                else:
                    show(fn[:60], f"Problem, retrying in 20 min: {e}"[:80], 100, "fail")
            save_state(state)
        for k in ("sizes", "errors", "failed", "copy_failed", "delivery"):
            state[k] = {f: v for f, v in state.get(k, {}).items() if os.path.exists(os.path.join(FOLDER, f))}
        save_state(state)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


if __name__ == "__main__":
    main()
