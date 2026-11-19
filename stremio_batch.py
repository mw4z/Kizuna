"""Sequential season/show downloads using the currently selected media preferences."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

import stremio as s

FILE = Path(s.agent.HERE) / "stremio-batch.json"
_busy = threading.Lock()
_guard = threading.RLock()
_cancel = threading.Event()
_state = None


def active():
    return _busy.locked()


def save(data):
    global _state
    with _guard:
        data["updated"] = time.time()
        s.agent.write_atomic(FILE, json.dumps(data, ensure_ascii=False))
        _state = data


def status():
    global _state
    with _guard:
        if _state is None and FILE.exists():
            _state = json.loads(FILE.read_text(encoding="utf-8"))
        if not _state or (_state.get("dismissed") and not active()):   # dismissed stays hidden on every device
            return None
        if any(e["status"] == "waiting" for e in _state["episodes"]):
            import library_processing
            with library_processing._guard:
                records = library_processing.read()
            library = s.find_library(_state["library_id"])
            changed = False
            for episode in _state["episodes"]:
                if episode["status"] != "waiting" or not library:
                    continue
                video = {"type": _state["type"], "meta": _state["meta"], **episode}
                record = records.get(library_processing.key_for(s.target(video, library)))
                if record and record["status"] == "done":
                    episode.update(status="done", reason=record["stage"])
                    changed = True
            if changed:
                if _state["status"] == "waiting" and not any(e["status"] == "waiting" for e in _state["episodes"]):
                    _state.update(status="done", stage="Subtitle preparation complete. Review any failed or unavailable episodes.")
                save(_state)
        out = {k: v for k, v in _state.items() if k not in ("episodes", "meta", "preferences")}
        counts = {name: 0 for name in ("pending", "running", "done", "existing", "unavailable", "failed", "waiting")}
        for episode in _state["episodes"]:
            counts[episode["status"]] += 1
        out["counts"] = counts
        out["total"] = len(_state["episodes"])
        out["problems"] = [{"episode": f"S{e['season']:02d}E{e['episode']:02d}", "reason": e.get("reason", "")}
                           for e in _state["episodes"] if e["status"] in ("failed", "unavailable", "waiting")][-20:]
        if out["status"] in ("running", "stopping") and not active():
            out.update(status="interrupted", stage="The app restarted. Resume to continue remaining episodes.")
        return out


def language(value):
    value = str(value or "und").lower().strip().replace("_", "-").split("-")[0]
    return next((code for code, names in s.AUDIO_LANGUAGES.items()
                 if value in [code] + [name.lower() for name in names]), value)


def resolution(video):
    width, height = video.get("width", 0), video.get("height", 0)
    for w, h, label in ((3500, 2000, "2160p"), (2500, 1400, "1440p"),
                         (1800, 1000, "1080p"), (1200, 700, "720p"), (950, 550, "576p")):
        if width >= w or height >= h:
            return label
    return "480p"


def episode_list(meta, scope, season, now=None):
    if scope not in ("season", "show"):
        raise s.UserError("Choose the selected season or whole show.")
    now = time.time() if now is None else now
    episodes = {}
    for raw in meta.get("videos", []):
        try:
            sn, en = int(raw["season"]), int(raw["episode"])
            if sn < 0 or en < 0 or not raw.get("id") or (scope == "season" and sn != season):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if raw.get("released"):
            try:
                date = datetime.fromisoformat(raw["released"].replace("Z", "+00:00"))
                if date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp() > now:
                    continue
            except (ValueError, TypeError):
                pass
        episodes[(sn, en)] = {"id": raw["id"], "season": sn, "episode": en, "status": "pending"}
    return [episodes[k] for k in sorted(episodes)]


def preferences(source, audio, subtitle):
    streams = source.get("probe", {}).get("streams", [])
    video = next((v for v in streams if v.get("codec_type") == "video"), None)
    if video is None:
        raise s.UserError("Check a release's tracks before downloading a season/show.")
    track = next((v for v in streams if v.get("codec_type") == "audio" and str(v["index"]) == audio), None)
    code = language(track.get("tags", {}).get("language")) if track else "none"
    if track is None and (audio != "none" or any(v.get("codec_type") == "audio" for v in streams)):
        raise s.UserError("Choose an available audio track.")
    if code == "und":
        raise s.UserError("Bulk downloads need a tagged audio language. Select a release with a named language first.")
    sub_code = "none"
    sub_role = s.track_role({})
    if subtitle != "none":
        sub = next((v for v in streams if v.get("codec_type") == "subtitle" and str(v["index"]) == subtitle), None)
        ext = source.get("external", {}).get(subtitle)
        if not sub and not ext:
            raise s.UserError("Choose an available subtitle track or no subtitles.")
        sub_code = language(sub.get("tags", {}).get("language")) if sub else language(ext.get("lang"))
        sub_role = s.track_role(sub or ext)
        if sub_code == "und":
            raise s.UserError("Choose a tagged subtitle language or no subtitles for bulk downloads.")
    return {"audio": code, "subtitle": sub_code, "audio_role": s.track_role(track or {}), "subtitle_role": sub_role, "resolution": resolution(video),
            "hdr": s.verified_range(video), "provider": source.get("provider", "")}


def start(key, audio, subtitle, library_id, scope, selected_episodes=""):
    source = s.item(key, "source")
    video = source["video"]
    if video["type"] == "movie":
        raise s.UserError("Season/show downloads are available for series and anime.")
    profile = preferences(source, audio, subtitle)
    library = s.find_library(library_id)
    if not library:
        raise s.UserError("Choose an existing Jellyfin library.")
    episodes = episode_list(video["meta"], "season" if scope == "selected" else scope, video["season"])
    if scope == "selected":
        try:
            chosen = json.loads(selected_episodes)
            if not isinstance(chosen, list) or not 0 < len(chosen) <= 2000 or any(type(n) is not int or n < 0 for n in chosen):
                raise ValueError()
            if not set(chosen).issubset({e["episode"] for e in episodes}):
                raise ValueError()
        except (ValueError, TypeError):
            raise s.UserError("Select available episodes from this season.") from None
        episodes = [e for e in episodes if e["episode"] in chosen]
    if not episodes:
        raise s.UserError("No released episodes were returned for this season/show.")
    meta = {k: video["meta"].get(k) for k in ("id", "name", "releaseInfo", "runtime")}
    data = {"name": meta["name"], "meta": meta, "type": video["type"], "scope": scope,
            "library": library["name"], "library_id": library_id, "preferences": profile,
            "episodes": episodes, "status": "running", "stage": "Finding matching releases", "created": time.time()}
    launch(data)
    return status()


def launch(data):
    if s._job_lock.locked():
        raise s.UserError("Wait for the current download to finish before starting a batch.")
    if not _busy.acquire(blocking=False):
        raise s.UserError("A season/show download is already running.")
    try:
        _cancel.clear()
        data["status"] = "running"
        save(data)
        threading.Thread(target=worker, args=(data,), daemon=True).start()
    except Exception:
        _busy.release()
        raise


def stop():
    if active():
        _cancel.set()
        with _guard:
            _state.update(status="stopping", stage="Stopping after the current episode/check finishes")
            save(_state)
    return status()


def resume():
    status()  # Load a saved queue if the app restarted.
    if not _state or active():
        raise s.UserError("There is no stopped batch to resume.")
    data = json.loads(json.dumps(_state))
    for episode in data["episodes"]:
        if episode["status"] in ("running", "failed", "unavailable"):
            episode["status"] = "pending"
    if not any(e["status"] == "pending" for e in data["episodes"]):
        raise s.UserError("All episodes are already downloaded or present in the library.")
    data["dismissed"] = False
    launch(data)
    return status()


def dismiss():
    """Hide a finished/stopped batch everywhere (saved in the batch file, so reloads and other devices agree)."""
    global _state
    with _guard:
        if _state is None and FILE.exists():
            _state = json.loads(FILE.read_text(encoding="utf-8"))
    if not _state:
        return None
    if active():
        raise s.UserError("A running batch can't be dismissed; stop it first.")
    save(dict(_state, dismissed=True))
    return None


def source_identity(key):
    source = s.item(key, "source")
    stream = source.get("stream", {})
    return s.hashlib.sha256(json.dumps([stream.get("url"), stream.get("infoHash"), stream.get("fileIdx")], sort_keys=True).encode()).hexdigest()


def choose(data, video, rejected=(), cancel=None):
    cancel = cancel or _cancel          # weekly fetches pass their own flag
    prefs = data["preferences"]
    result = s.streams(s.remember(video))
    if result["failed_addons"]:
        result = s.streams(s.remember(video))  # Reuse late requests rather than dropping the episode immediately.
    candidates = result["sources"]
    candidates.sort(key=lambda r: (prefs["audio"] not in r.get("audio_languages", []),
                                   r.get("provider") != prefs["provider"], "❌" in r.get("description", "")))
    for candidate in candidates:
        if cancel.is_set():
            return None
        langs = candidate.get("audio_languages", [])
        if (prefs["audio"] != "none" and langs and prefs["audio"] not in langs
                or candidate["quality"] not in (prefs["resolution"], "Unspecified")):
            continue
        key = candidate["key"]
        try:
            if source_identity(key) in rejected:
                continue
            info = s.inspect(key)
            source = s.item(key, "source")
            v = next(t for t in source["probe"]["streams"] if t.get("codec_type") == "video")
            if resolution(v) != prefs["resolution"]:
                continue
            if prefs["hdr"] != "Unspecified" and info["dynamic_range"] != prefs["hdr"]:
                continue
            audio = "none" if prefs["audio"] == "none" and not info["audio"] else next(
                (t["id"] for t in info["audio"] if language(t["language"]) == prefs["audio"]
                 and t.get("role", s.track_role(t)) == prefs.get("audio_role", s.track_role({}))), None)
            if audio is None:
                continue
            subtitle = "none" if prefs["subtitle"] == "none" else next(
                (t["id"] for t in info["subtitles"] if language(t["language"]) == prefs["subtitle"]
                 and t.get("role", s.track_role(t)) == prefs.get("subtitle_role", s.track_role({}))), None)
            if subtitle is None:
                external = s.subtitle_options(key)
                subtitle = next((t["id"] for t in external["subtitles"] if language(t["language"]) == prefs["subtitle"]
                                 and t.get("role", s.track_role(t)) == prefs.get("subtitle_role", s.track_role({}))), None)
            if subtitle is not None:
                return key, audio, subtitle
        except Exception:
            continue  # Try another matching source; never log private source URLs.
    return None


def worker(data):
    try:
        for episode in data["episodes"]:
            if episode["status"] != "pending":
                continue
            if _cancel.is_set():
                data.update(status="stopped", stage="Stopped. Remaining episodes can be resumed.")
                break
            label = f"S{episode['season']:02d}E{episode['episode']:02d}"
            video = {"kind": "video", "type": data["type"], "meta": data["meta"],
                     "id": episode["id"], "season": episode["season"], "episode": episode["episode"]}
            library = s.find_library(data["library_id"])
            if not library:
                raise s.UserError("The selected Jellyfin library is unavailable. Restore it before resuming.")
            data["library_id"] = library["id"]          # a batch saved with an old position-based id moves to the stable one
            if s.target(video, library).exists():
                episode.update(status="existing", reason="Already in the library")
                save(data)
                continue
            import library_processing
            with library_processing._guard:
                processing = library_processing.read().get(library_processing.key_for(s.target(video, library)))
            if processing and processing["status"] not in ("done", "cancelled"):
                episode.update(status="waiting", reason=processing["stage"])
                save(data)
                continue
            if s.shutil.disk_usage(library["path"]).free < 2e9:
                data.update(status="paused", stage="Not enough disk space. Free space and resume.")
                break
            episode["status"] = "running"
            data["stage"] = f"Finding a matching release for {label}"
            save(data)
            try:
                rejected = set()
                chosen = choose(data, video)
                if _cancel.is_set():
                    episode["status"] = "pending"
                    data.update(status="stopped", stage="Stopped. Remaining episodes can be resumed.")
                    break
                if not chosen:
                    episode.update(status="unavailable", reason="No available source matched the selected audio, subtitles, resolution and HDR")
                else:
                    data["stage"] = f"Downloading {label}"
                    save(data)
                    while chosen:
                        identity = source_identity(chosen[0])
                        job = s.start_download(*chosen, data["library_id"], _batch=True)
                        if job["status"] == "done" or not job.get("retryable") or _cancel.is_set():
                            break
                        rejected.add(identity)
                        chosen = choose(data, video, rejected)
                    if job["status"] != "done" and "space" in job["stage"].lower():
                        raise s.UserError(job["stage"])
                    if job["status"] != "done" and job.get("retryable") is False:
                        episode["status"] = "pending"
                        data.update(status="paused", stage=job["stage"])
                        break
                    episode.update(status="done" if job["status"] == "done" else "waiting" if job["status"] == "waiting" else "failed", reason=job["stage"])
            except s.UserError as e:
                if "space" in str(e):
                    episode["status"] = "pending"
                    data.update(status="paused", stage=str(e))
                    break
                episode.update(status="failed", reason=str(e))
            except Exception:
                episode.update(status="failed", reason="Episode failed. Retry the batch or select this episode manually.")
            finally:
                with s._lock:
                    for key, (_, value) in list(s._items.items()):
                        if value is video or value.get("video") is video:
                            del s._items[key]
            save(data)
        if _cancel.is_set() and data["status"] in ("running", "stopping"):
            data.update(status="stopped", stage="Stopped. Remaining episodes can be resumed.")
        elif data["status"] == "running":
            waiting = any(e["status"] == "waiting" for e in data["episodes"])
            data.update(status="waiting" if waiting else "done", stage="Downloads finished; subtitle preparation is pending and will retry automatically." if waiting else "Batch finished. Review any unavailable or failed episodes below.")
    except Exception as e:
        data.update(status="paused", stage=str(e) if isinstance(e, s.UserError) else "Batch interrupted. Resume to retry remaining episodes.")
    finally:
        try:
            save(data)
        finally:
            _busy.release()
