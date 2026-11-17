"""Personal preferences (settings.json, git-ignored), edited on the Settings page. Every value is validated and
clamped; a missing or damaged file means today's defaults, so the agent never stops over a setting."""
import json
from pathlib import Path
import threading

import agent

FILE = Path(agent.HERE) / "settings.json"
LANGUAGES = {"ja": "Japanese", "en": "English", "ar": "Arabic", "ko": "Korean", "zh": "Chinese", "es": "Spanish",
             "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "ru": "Russian", "tr": "Turkish",
             "hi": "Hindi"}
RESOLUTIONS = ("2160p", "1080p", "720p", "576p", "480p")
DEFAULTS = {
    "weekly_audio": ["ja"],                    # audio for weekly episodes fetched from Stremio, in order of preference
    "weekly_resolutions": ["1080p", "720p"],   # tried in this order
    "weekly_subtitle": "none",                 # extra subtitle track to keep from Stremio ("none" = only Arabic)
    "check_minutes": 20,                       # how often DKB and Stremio look for new episodes
    "keep_hours": 24,                          # watched episodes go to the Recycle Bin this long after finishing
    "notify_ready": True,                      # Windows notification when an episode or movie is ready
    "clock": "auto",                           # air times: "auto" (Windows setting), "12h" or "24h"
    "default_audio_filter": "",                # Discover: preselected audio filter ("" = all)
    "library_movie": "Movies",                 # Discover: default library per type
    "library_series": "Anime",
    "library_anime": "Anime",
}
_lock = threading.Lock()
_cache = {"stamp": None, "data": dict(DEFAULTS)}


class SettingsError(ValueError):
    pass


def _clean(raw, strict=False):
    """Validated copy of `raw` (unknown keys dropped). strict: raise on bad values instead of using the default."""
    out = dict(DEFAULTS)

    def bad(key, why):
        if strict:
            raise SettingsError(why)

    def langs(key):
        v = raw.get(key, out[key])
        v = v if isinstance(v, list) else [x.strip() for x in str(v).split(",")]
        v = [x for x in dict.fromkeys(v) if x in LANGUAGES]
        if v:
            out[key] = v[:4]
        else:
            bad(key, "Choose at least one audio language.")

    langs("weekly_audio")
    res = raw.get("weekly_resolutions", out["weekly_resolutions"])
    res = res if isinstance(res, list) else [x.strip() for x in str(res).split(",")]
    res = [x for x in dict.fromkeys(res) if x in RESOLUTIONS]
    if res:
        out["weekly_resolutions"] = res
    else:
        bad("weekly_resolutions", "Choose at least one resolution.")
    sub = str(raw.get("weekly_subtitle", out["weekly_subtitle"]))
    out["weekly_subtitle"] = sub if sub == "none" or sub in LANGUAGES else (bad("weekly_subtitle", "Unknown subtitle language.") or "none")
    for key, low, high, label in (("check_minutes", 5, 240, "Check every"), ("keep_hours", 1, 24 * 30, "Keep watched episodes for")):
        try:
            n = int(str(raw.get(key, out[key])).strip())
            if not low <= n <= high:
                raise ValueError
            out[key] = n
        except (TypeError, ValueError):
            bad(key, f"{label} must be between {low} and {high}.")
    out["notify_ready"] = str(raw.get("notify_ready", out["notify_ready"])).lower() in ("1", "true", "yes", "on")
    clock = str(raw.get("clock", out["clock"]))
    out["clock"] = clock if clock in ("auto", "12h", "24h") else (bad("clock", "Unknown clock format.") or "auto")
    audio = str(raw.get("default_audio_filter", out["default_audio_filter"]))
    out["default_audio_filter"] = audio if audio in ("", "und") or audio in LANGUAGES else (bad("default_audio_filter", "Unknown language.") or "")
    for key in ("library_movie", "library_series", "library_anime"):
        name = str(raw.get(key, out[key])).strip()
        if 1 <= len(name) <= 100 and not any(c in name for c in '<>:"/\\|?*'):
            out[key] = name
        else:
            bad(key, "Choose a library.")
    return out


def get(key=None):
    with _lock:
        try:
            stamp = FILE.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = None
        if stamp != _cache["stamp"]:
            try:
                raw = json.loads(FILE.read_text(encoding="utf-8")) if stamp else {}
                data = _clean(raw if isinstance(raw, dict) else {})
            except (OSError, ValueError):
                data = dict(DEFAULTS)              # damaged file: defaults, never a crash
            _cache.update(stamp=stamp, data=data)
        data = dict(_cache["data"])
    return data if key is None else data[key]


def update(form):
    data = _clean(dict(get(), **{k: v for k, v in form.items() if k in DEFAULTS}), strict=True)
    with _lock:
        agent.write_atomic(FILE, json.dumps(data, ensure_ascii=False, indent=1))
        _cache["stamp"] = None
    import shows
    shows.wake_dkb.set()                           # a new check interval takes effect now
    shows.wake_stremio.set()
    return page()


def page():
    import stremio
    try:
        libraries = sorted({lib["name"] for lib in stremio.libraries()})
    except Exception:
        libraries = []
    return {"settings": get(), "languages": LANGUAGES, "resolutions": RESOLUTIONS, "libraries": libraries,
            "defaults": DEFAULTS}
