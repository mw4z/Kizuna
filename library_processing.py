"""Durable mandatory Arabic processing for Stremio and manually added library files."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

import agent

FILE = Path(agent.HERE) / "library-processing.json"
ROOT = Path(agent.WORK) / "library-processing"
_guard = threading.RLock()
_processor = threading.Lock()
MEDIA = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm"}


def read():
    if not FILE.exists():
        return {}
    return json.loads(FILE.read_text(encoding="utf-8"))


def save_record(key, record):
    with _guard:
        data = read()
        record["updated"] = time.time()
        data[key] = record
        agent.write_atomic(FILE, json.dumps(data, ensure_ascii=False))


def signature(path):
    info = Path(path).stat()
    return [info.st_size, info.st_mtime_ns]


STYLED = ("S_TEXT/ASS", "S_TEXT/SSA")


def styled_arabic(path):
    """True when the file already carries a styled (ASS/SSA) Arabic subtitle, whatever its track name.

    Hand-made fansub tracks (e.g. Bleach's "العربية [Kurai Mangetsu]") are already house quality; rebuilding
    them in the plain weekly style flattened their styles, lyrics and signs (2026-11-15, Bleach 196-201)."""
    try:
        tracks = agent.tracks(str(path))
    except Exception:
        return False
    return any(t.get("type") == "subtitles"
               and str(t.get("properties", {}).get("language", "")).lower() in ("ara", "ar")
               and str(t.get("properties", {}).get("codec_id", "")).upper() in STYLED
               for t in tracks)


def unchanged_reason(path):
    """Why a library file needs no Arabic work (styled Arabic subtitles, or an Arabic dub), else None. Checked
    before any copy: most queued library files are dubs, and each used to be copied twice just to find out."""
    if styled_arabic(path):
        return "Styled Arabic subtitles already present; original unchanged"
    try:
        if agent.has_arabic_audio(agent.tracks(str(path))):
            return "Arabic dub already present; original unchanged"
    except Exception:
        pass
    return None


def leave_unchanged(key, path, destination, record=None, stage="Styled Arabic subtitles already present; original unchanged"):
    """Record a library file that needs nothing as done, without copying or touching it."""
    record = dict(record or {}, source=str(path), destination=str(destination), manual=True,
                  metadata=(record or {}).get("metadata") or metadata_for(destination),
                  input_signature=signature(path), status="done", pct=100,
                  stage=stage, completed_signature=signature(destination))
    record.pop("retry_at", None)
    save_record(key, record)
    return record


def key_for(path):
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode("utf-8")).hexdigest()


def metadata_for(path):
    path = Path(path)
    name, episode = agent.parse_name(path.name)
    if not episode and re.fullmatch(r"Season \d+", path.parent.name, re.I):
        number = re.match(r"(?:E|Episode[ ._-]*)?(\d+)(?:\D|$)", path.stem, re.I)
        if number:
            name = path.parent.parent.name
            episode = f"S{int(re.search(r'\d+', path.parent.name).group()):02d}E{int(number[1]):02d}"
    if episode:
        season, number = map(int, re.findall(r"\d+", episode))
        year = re.search(r"\b(?:19|20)\d{2}\b", name)
        return {"type": "series", "season": season, "episode": number,
                "meta": {"name": re.sub(r"\s*\(\d{4}\)", "", name).replace(".", " ").strip(),
                         "releaseInfo": year.group() if year else ""}}
    return {"type": "movie", "meta": movie_name(path.stem)}


def movie_name(stem):
    """'Dune.Part.Two.2024.1080p.WEB-DL' -> Dune Part Two / 2024; 'Blade Runner 2049 (2017)' -> Blade Runner 2049 /
    2017; '1917 (2019)' -> 1917 / 2019. A bracketed year wins; otherwise the LAST year-like number that isn't the
    whole title, so numbers inside titles survive."""
    name = re.sub(r"[._]+", " ", stem)
    name = re.sub(r"[ -]+(?:\d{3,4}p|WEB[- ]?(?:DL|Rip)|BluRay|BDRip|HDTV|x26[45]|HEVC)\b.*$", "", name, flags=re.I).strip()
    year = re.search(r"[(\[]((?:19|20)\d{2})[)\]]", name)
    if not year:
        year = ([m for m in re.finditer(r"\b((?:19|20)\d{2})\b", name) if m.start() > 0] or [None])[-1]
    if not year:
        return {"name": name, "releaseInfo": ""}
    return {"name": name[:year.start()].strip(" -([") or name, "releaseInfo": year.group(1)}


def decode_subtitle(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1256", errors="replace")


def sidecar(path, folder):
    for code in ("ar", "ara", "Arabic"):
        for suffix in ("srt", "ass", "vtt"):
            candidate = Path(path).with_suffix(f".{code}.{suffix}")
            if not candidate.exists():
                continue
            target = folder / "arabic.srt"
            if suffix == "srt":                 # hand-made Arabic SRTs are often Windows-1256: store as UTF-8
                target.write_text(decode_subtitle(candidate.read_bytes()), encoding="utf-8", newline="")
            else:
                subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(candidate), "-c:s", "srt", str(target)],
                               check=True, capture_output=True, timeout=60, creationflags=agent.NO_CF)
            return


def submit(path, destination, metadata=None, progress=None, manual=False):
    path, destination = Path(path), Path(destination)
    key = key_for(destination)
    with _processor:
        with _guard:
            record = read().get(key)
        if record and record["status"] == "done" and destination.exists() and record.get("completed_signature") == signature(destination):
            return record
        reason = manual and path == destination and unchanged_reason(path)
        if reason:
            return leave_unchanged(key, path, destination, record, reason)
        if not record or record.get("status") == "done" or (manual and record.get("input_signature") != signature(path)):
            folder = ROOT / key
            folder.mkdir(parents=True, exist_ok=True)
            for name in ("raw.media", "media.mkv", "arabic.srt", "ar.srt", "ar.ass", "state.json", "result.json", "progress.json"):
                (folder / name).unlink(missing_ok=True)
            record = {"source": str(path), "destination": str(destination), "metadata": metadata or metadata_for(destination),
                      "manual": manual, "input_signature": signature(path), "status": "pending",
                      "stage": "Waiting for Arabic preparation", "pct": 0}
            record["backup"] = f"original.{record['input_signature'][0]}.{record['input_signature'][1]}{path.suffix}"
            save_record(key, record)
            if shutil.disk_usage(folder).free < path.stat().st_size * (3.2 if manual else 2.2) + 500e6:
                record.update(status="waiting", stage="Not enough disk space for mandatory Arabic preparation; original retained", retry_at=time.time() + 1200)
                save_record(key, record)
                return record
            sidecar(path, folder)
            if manual:
                shutil.copy2(path, folder / "raw.media")
            else:
                shutil.move(str(path), str(folder / "raw.media"))
            save_record(key, record)
        return process_record(key, record, progress)


def process_record(key, record, progress=None):
    folder = ROOT / key
    destination = Path(record["destination"])
    import psutil
    try:
        pid = int((folder / "worker.pid").read_text(encoding="ascii"))
        arguments = psutil.Process(pid).cmdline()
        if any(Path(arg).name == "processing_worker.py" for arg in arguments) and str(folder) in arguments:
            record.update(status="waiting", stage="Previous Arabic worker is still finishing; recovery will retry automatically", retry_at=time.time() + 30)
            save_record(key, record)
            return record
    except (OSError, ValueError, psutil.Error):
        pass
    source = Path(record["source"])
    reason = record["manual"] and source == destination and source.exists() and unchanged_reason(source)
    if reason:
        for name in ("raw.media", "media.mkv"):                 # queued before this guard existed
            (folder / name).unlink(missing_ok=True)
        return leave_unchanged(key, source, destination, record, reason)
    if record.get("published_signature") and destination.exists() and signature(destination) == record["published_signature"]:
        record.update(status="done", stage="Arabic verified and saved to Jellyfin", pct=100, completed_signature=signature(destination))
        save_record(key, record)
        return record
    record.update(status="running", stage="Preparing Arabic subtitles and checking timing", pct=0)
    save_record(key, record)
    (folder / "metadata.json").write_text(json.dumps(dict(record["metadata"], _backup=record["backup"]), ensure_ascii=False), encoding="utf-8")
    (folder / "result.json").unlink(missing_ok=True)
    try:
        if not (folder / "raw.media").exists() and not (folder / record["backup"]).exists():
            original = Path(record["source"])
            if shutil.disk_usage(folder).free < original.stat().st_size * (3.2 if record["manual"] else 2.2) + 500e6:
                raise OSError("Not enough disk space")
            if record["manual"]:
                shutil.copy2(original, folder / "raw.media")
            else:
                shutil.move(str(original), str(folder / "raw.media"))
        sidecar(record["source"] if record["manual"] else record["destination"], folder)
        with subprocess.Popen([sys.executable, str(Path(agent.HERE) / "processing_worker.py"), str(folder)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=agent.NO_CF) as process:
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if time.monotonic() - started > 3 * 3600:
                        raise TimeoutError("Arabic processing exceeded three hours")
                    try:
                        current = json.loads((folder / "progress.json").read_text(encoding="utf-8"))
                        record.update(stage=current["stage"], pct=current["pct"])
                    except (OSError, ValueError, KeyError):
                        pass
                    save_record(key, record)
                    if progress:
                        progress(record["stage"], record["pct"])
                    time.sleep(1)
            finally:
                if process.poll() is None:
                    import psutil
                    try:
                        for child in psutil.Process(process.pid).children(recursive=True):
                            try:
                                child.kill()
                            except psutil.NoSuchProcess:
                                pass
                    finally:
                        process.kill()
                        process.wait()
            if process.returncode:
                raise RuntimeError("Arabic worker stopped unexpectedly")
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        if result.get("leave_unchanged"):          # e.g. Arabic burned into the picture: nothing to add
            if not record["manual"] and not destination.exists():   # Stremio download still in raw.media: deliver as is
                # (a manual file the user deleted meanwhile must never come back)
                deliver(key, record, folder / "raw.media", destination)
            record.update(status="done", stage=result["stage"], pct=100, completed_signature=signature(destination))
            record.pop("attempts", None)
            for name in ("raw.media", "media.mkv"):
                (folder / name).unlink(missing_ok=True)
        elif not result.get("ready"):
            record.update(status="waiting", stage=result["stage"], retry_at=backoff(record, 3600))
        else:
            publish(key, record, folder)
            record.pop("attempts", None)
    except Exception as error:
        record.update(status="waiting", stage="Arabic preparation or delivery failed; original retained. Automatic retry scheduled.",
                      retry_at=backoff(record, 1200))
        agent.log(f"Library processing: {destination.name}: {type(error).__name__}")
    save_record(key, record)
    return record


def backoff(record, base):
    """Next retry time: base, 2x, 4x ... capped at a day. Every retry repeats the full copy, OCR, add-on downloads
    and audio checks, so a job that keeps failing must not do that hourly forever. A new input starts at zero."""
    record["attempts"] = record.get("attempts", 0) + 1
    return time.time() + min(86400, base * 2 ** (record["attempts"] - 1))


def deliver(key, record, source, destination):
    """Copy `source` into the library via a .part file and an atomic rename; the source stays until it's there."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".animefya.part")
    shutil.copy2(source, part)
    record.update(published_signature=signature(part))
    save_record(key, record)
    os.rename(part, destination)


def publish(key, record, folder):
    destination = Path(record["destination"])
    original = Path(record["source"])
    prepared = folder / "media.mkv"
    tracks = agent.tracks(str(prepared))
    dubbed = agent.has_arabic_audio(tracks)
    if not dubbed and not any(t.get("type") == "subtitles" and t.get("properties", {}).get("track_name") == "العربية"
                              and t.get("properties", {}).get("language") == "ara" for t in tracks):
        raise RuntimeError("Mandatory Arabic output track is missing")
    if record["manual"] and signature(original) != record["input_signature"]:
        raise RuntimeError("Original changed during processing")
    if record["manual"] and dubbed:
        record.update(status="done", stage="Arabic dub already present; original unchanged", pct=100,
                      completed_signature=signature(original))
        for name in ("raw.media", "media.mkv"):
            (folder / name).unlink(missing_ok=True)
        return
    if record["manual"] and not dubbed and destination.suffix.lower() != ".mkv":
        destination = destination.with_suffix(".mkv")
    if destination.exists() and not (record["manual"] and destination == original):
        raise FileExistsError("Destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".animefya.part")
    if record["manual"] and not (folder / record["backup"]).exists():
        os.replace(folder / "raw.media", folder / record["backup"])
    shutil.copy2(prepared, part)
    if record["manual"] and signature(original) != record["input_signature"]:
        part.unlink(missing_ok=True)
        raise RuntimeError("Original changed before publication")
    record.update(destination=str(destination), published_signature=signature(part))
    save_record(key, record)
    if record["manual"] and destination == original:
        os.replace(part, destination)
    else:
        os.rename(part, destination)
    if record["manual"] and destination != original:
        # The byte-exact original is retained outside Jellyfin before removing its old library entry.
        try:
            original.unlink()
        except PermissionError:
            agent.log(f"Library processing: original still in use; retrying retirement for {original.name}")
    record.update(status="done", stage="Arabic dub preserved" if dubbed else "Arabic subtitles and timing verified", pct=100)
    record["completed_signature"] = signature(destination)
    alert = "Kizuna: movie ready" if record["metadata"].get("type") == "movie" else "Kizuna: ready"
    try:
        agent.notify(alert, f"{destination.stem}: Arabic preparation complete.")
    except Exception:
        agent.log("Library processing complete; notification unavailable")
    for name in ("raw.media", "media.mkv"):
        (folder / name).unlink(missing_ok=True)


def status():
    with _guard:
        return list(read().values())


def poll(observed):
    import stremio
    with _guard:
        records = read()
    for key, record in records.items():
        if record["status"] == "done" and record["manual"] and record["source"] != record["destination"]:
            original = Path(record["source"])
            destination = Path(record["destination"])
            if original.exists() and signature(original) == record["input_signature"] and destination.exists() and signature(destination) == record.get("completed_signature"):
                try:
                    original.unlink()
                except PermissionError:
                    pass
        if record["status"] != "done" and record.get("retry_at", 0) <= time.time():
            if not _processor.acquire(blocking=False):
                continue
            try:
                with _guard:
                    record = read().get(key, record)
                if record["status"] != "done":
                    process_record(key, record)
            except (OSError, ValueError, KeyError):
                record.update(status="waiting", stage="Processing queue entry needs attention; original retained", retry_at=backoff(record, 1200))
                save_record(key, record)
            finally:
                _processor.release()
    with _guard:
        records = read()
    completed = {r["destination"]: r for r in records.values() if r["status"] == "done"}
    for library in stremio.libraries():
        for path in Path(library["path"]).rglob("*"):
            if path.suffix.lower() not in MEDIA or ".tmp." in path.name or not path.is_file():
                continue
            stamp = signature(path)
            key = key_for(path)
            record = records.get(key)
            if completed.get(str(path), {}).get("completed_signature") == stamp:
                continue
            if record and record["status"] == "done" and record["source"] != record["destination"] and record["input_signature"] == stamp:
                continue
            if record and record["status"] != "done" and (not record["manual"] or record.get("input_signature") == stamp):
                continue
            # Do not redo a verified copy of the weekly inbox, but raw manual copies still need preparation.
            inbox = Path(agent.FOLDER) / path.name
            if Path(library["path"]).resolve() == Path(agent.LIBRARY).resolve() and inbox.exists() and signature(inbox) == stamp:
                tracks = agent.tracks(str(path))
                if agent.has_arabic_audio(tracks) or any(t.get("type") == "subtitles" and t.get("properties", {}).get("track_name") == "العربية" for t in tracks):
                    continue
            prior = observed.get(str(path))
            observed[str(path)] = (stamp, prior[1] if prior and prior[0] == stamp else time.time())
            if time.time() - observed[str(path)][1] >= 60:
                submit(path, path, manual=True)
    return {path: value for path, value in observed.items() if Path(path).exists()}


def watch():
    observed = {}
    while True:
        try:
            observed = poll(observed)
        except Exception as error:
            agent.log(f"Library processing queue: {type(error).__name__}")
        time.sleep(30)
