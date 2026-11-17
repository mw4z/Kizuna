"""Run the existing Arabic pipeline in an isolated work folder, without publishing."""
import json
import os
from pathlib import Path
import re
import shutil
import sys

import agent
import fansub
import hardsub


BURNED_IN = "burned_in"     # prepare() result: Arabic already in the picture -> leave the file unchanged


def prepare(folder):
    folder = Path(folder)
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    raw, video = folder / "raw.media", folder / "media.mkv"
    if not raw.exists():
        raw = folder / metadata["_backup"]
    agent.WORK = agent.FOLDER = str(folder)
    agent.STATE = str(folder / "state.json")
    agent.PFILE = str(folder / "progress.json")
    agent.LOG = str(folder / "worker.log")
    state = agent.load_state()
    original = agent.arabic_track
    agent.arabic_track = lambda path: original(path, include_named=True)
    try:
        shutil.copy2(raw, video)
        tracks = agent.tracks(str(video))
        dubbed = agent.has_arabic_audio(tracks)
        mode = fansub.mode_for(metadata.get("meta", {}).get("name", ""), metadata.get("meta", {}).get("id", ""))
        if not dubbed and mode == "manual":
            agent.show(video.name, "Manual typesetting show; automatic publication blocked", 100, "wait")
            return False
        subtitle = folder / "arabic.srt"                 # a sidecar .ar.srt placed by the user is trusted as-is
        embedded = agent.arabic_track(str(video)) is not None
        if not dubbed and not subtitle.exists() and not embedded:
            # Darwin S01E12 (Anime4up) was hardsubbed: a second, soft Arabic track would double the subtitles
            burned, hits, checked = hardsub.burned_in_arabic(str(video))
            agent.log(f"burned-in Arabic check: {hits}/{checked} frames read as Arabic dialogue")
            if burned:
                agent.show(video.name, "Arabic is burned into the video; nothing to add", 100, "done")
                return BURNED_IN
        asked = not dubbed and not subtitle.exists() and (mode in ("fansub", "fansub_first") or not embedded)
        if asked:
            # fansub shows never take embedded Arabic of unknown origin first; styled fansub tracks never
            # reach this point (library_processing leaves them untouched). fansub_first: when the add-ons have
            # nothing, the file's own official Arabic is the last level-2 option.
            if not fetch_arabic(metadata, subtitle, mode, video) and (
                    mode == "fansub" or (mode == "fansub_first" and not embedded)):
                agent.show(video.name, "No acceptable fan translation yet; checking again hourly", 100, "wait")
                return False
        name = metadata.get("meta", {}).get("name") or metadata.get("name") or "Media"
        if metadata.get("type") != "movie" and metadata.get("season") is not None:
            search_name = f"{name} - S{metadata['season']:02d}E{metadata['episode']:02d}.mkv"
        else:
            search_name = name + " " + str(metadata.get("meta", {}).get("releaseInfo") or "") + ".mkv"
        return bool(agent.process(str(video), state, publish=False,
                                  subtitle_path=str(subtitle) if subtitle.exists() else None, search_name=search_name,
                                  addons=not asked))           # the add-ons were just asked; don't download it all twice
    finally:
        agent.save_state(state)
        agent.arabic_track = original


def jellyfin_imdb(name, kind):
    """IMDb id Jellyfin already matched for this library title (works for Arabic folder names such as
    'حادثة داروين', which subtitle sites only know by their English title). -> id or None."""
    if not name:
        return None
    try:
        with agent.jellyfin_db() as c:
            rows = c.execute(
                "select distinct p.ProviderValue from BaseItemProviders p join BaseItems b on b.Id = p.ItemId "
                "where p.ProviderId = 'Imdb' and b.Name = ? and b.Type like ?",
                (name, "%.Movie" if kind == "movie" else "%.Series")).fetchall()
    except Exception:
        return None
    ids = {r[0] for r in rows if re.fullmatch(r"tt\d+", str(r[0]))}
    return ids.pop() if len(ids) == 1 else None


def identify(s, meta, kind):
    """IMDb id for a title by exact name; when several shows share the name (Bleach 2004 vs a newer entry),
    the year from the folder name decides. -> id or None (ambiguous / not found)."""
    found = s.search(meta.get("name", ""), kind)
    metas = [s.item(t["key"], "title")["meta"] for t in found["results"]
             if t["name"].casefold() == meta.get("name", "").casefold()]
    ids = {m.get("id") for m in metas}
    year = str(meta.get("releaseInfo") or "")[:4]
    if len(ids) > 1 and year:
        ids = {m.get("id") for m in metas if str(m.get("releaseInfo") or m.get("year") or "")[:4] == year}
    return next(iter(ids)) if len(ids) == 1 and re.fullmatch(r"tt\d+", str(next(iter(ids)))) else None


def fetch_arabic(metadata, destination, mode="official_ok", media=None):
    """Human Arabic from the user's subtitle add-ons via fansub.pick (policy, no machine translation, timing
    checked against `media`). -> chosen release name, or None."""
    import stremio as s
    video = dict(metadata)
    meta = video.setdefault("meta", {})
    # Stremio downloads carry the episode's IMDb id ("tt33043892:2:3") while the show's meta id can be TMDB
    # ("tmdb:259909", Dexter: Resurrection 2026-11-17): take the IMDb id from the episode id.
    tt = re.match(r"(tt\d+)", str(video.get("id") or ""))
    if tt and not re.fullmatch(r"tt\d+", str(meta.get("id") or "")):
        meta["id"] = tt.group(1)
    try:
        if not video.get("id"):
            meta["id"] = (meta.get("id") or fansub.imdb_for(meta.get("name", ""))
                          or jellyfin_imdb(meta.get("name", ""), video.get("type", "series"))
                          or identify(s, meta, video.get("type", "series")))
            if re.fullmatch(r"tt\d+", str(meta.get("id") or "")):
                video["id"] = meta["id"] if video.get("type") == "movie" else f"{meta['id']}:{video['season']}:{video['episode']}"
            else:
                agent.log(f"fansub: could not identify '{meta.get('name')}' ({meta.get('releaseInfo') or 'no year'}) on Stremio")
        if re.fullmatch(r"tt\d+", str(meta.get("id", ""))):
            canonical = s.get_json("https://v3-cinemeta.strem.io/meta/" + (
                "movie" if video.get("type") == "movie" else "series") + "/" + meta["id"] + ".json", timeout=12)
            meta["name"] = canonical.get("meta", {}).get("name") or meta.get("name")
        if not re.fullmatch(r"tt\d+", str(meta.get("id", ""))) or media is None:
            return None
        movie = video.get("type") == "movie"
        # the episode id carries the catalog's own numbering ("tt..:2:5"); weekly shows may number their library
        # differently (per-season episodes), and subtitle add-ons only know the catalog numbers
        own = re.fullmatch(r"tt\d+:(\d+):(\d+)", str(video.get("id") or ""))
        season, episode = (int(own.group(1)), int(own.group(2))) if own else (video.get("season"), video.get("episode"))
        return fansub.pick(str(media), meta["id"], None if movie else season, None if movie else episode,
                           str(destination), mode, kind="movie" if movie else "series")
    except Exception as error:
        agent.log(f"Arabic subtitle add-ons unavailable ({type(error).__name__}); trying the existing subtitle fallback")
        return None


if __name__ == "__main__":
    work = Path(sys.argv[1])
    (work / "worker.pid").write_text(str(os.getpid()), encoding="ascii")
    try:
        ready = prepare(work)
        progress = json.loads((work / "progress.json").read_text(encoding="utf-8"))
        result = {"ready": ready is True, "leave_unchanged": ready == BURNED_IN,
                  "stage": progress.get("stage", "Arabic preparation incomplete")}
    except Exception as error:
        agent.log(f"Arabic processing worker failed ({type(error).__name__})")
        result = {"ready": False, "stage": "Arabic preparation failed. Original retained; automatic retry scheduled."}
    (work / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
