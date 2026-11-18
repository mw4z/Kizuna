"""Personal preferences (settings.json, git-ignored), edited on the Settings page. Anime and movies & series have their
own language/quality choices (anime: Japanese first; movies & series: English first). Every value is validated and
clamped; a missing or damaged file means the defaults, so the agent never stops over a setting."""
import json
from pathlib import Path
import threading

import agent
import subtitle_style

FILE = Path(agent.HERE) / "settings.json"
LANGUAGES = {"ja": "Japanese", "en": "English", "ar": "Arabic", "ko": "Korean", "zh": "Chinese", "es": "Spanish",
             "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "ru": "Russian", "tr": "Turkish",
             "hi": "Hindi"}
RESOLUTIONS = ("2160p", "1080p", "720p", "576p", "480p")
KINDS = ("anime", "screen")                    # screen = movies & series
DEFAULTS = {
    # anime (weekly episodes from Stremio, Discover defaults for anime)
    "anime_audio": ["ja"],
    "anime_subtitle": "none",                  # extra subtitle track to keep besides the Arabic one
    "anime_resolutions": ["1080p", "720p"],
    "anime_audio_filter": "",                  # Discover: preselected audio filter ("" = all)
    "library_anime": "Anime",
    # movies & series
    "screen_audio": ["en"],
    "screen_subtitle": "none",
    "screen_resolutions": ["1080p", "720p"],
    "screen_audio_filter": "",
    "library_movie": "Movies",
    "library_series": "Series",
    # everything
    "check_minutes": 20,                       # how often DKB and Stremio look for new episodes
    "keep_hours": 24,                          # watched episodes go to the Recycle Bin this long after finishing
    "notify_ready": True,                      # Windows notification when an episode or movie is ready
    "clock": "auto",                           # air times: "auto" (Windows setting), "12h" or "24h"
    "language": "en",                          # interface language: "en" or "ar" (right-to-left)
    "sub_style_ar": dict(subtitle_style.DEFAULT),   # look of the Arabic subtitles Kizuna builds
}
LEGACY = {"weekly_audio": "anime_audio", "weekly_subtitle": "anime_subtitle", "weekly_resolutions": "anime_resolutions",
          "default_audio_filter": "anime_audio_filter"}   # names used before the anime / movies & series split
_lock = threading.Lock()
_cache = {"stamp": None, "data": dict(DEFAULTS)}


class SettingsError(ValueError):
    pass


def _list(value):
    items = value if isinstance(value, list) else str(value).split(",")
    return [x.strip() for x in items if isinstance(x, str)]


def _clean(raw, strict=False, base=None):
    """Validated copy of `raw` (unknown keys dropped). A bad value keeps the one in `base` (the last good settings),
    so e.g. a damaged cleanup delay never silently drops to the default. strict: raise instead."""
    raw = dict(raw)
    for old, new in LEGACY.items():
        if old in raw and new not in raw:
            raw[new] = raw[old]
    out = dict(base or DEFAULTS)
    problems = []

    def bad(why):
        if strict:
            raise SettingsError(why)
        problems.append(why)

    for kind, label in (("anime", "anime"), ("screen", "movies & series")):
        langs = [x for x in dict.fromkeys(_list(raw.get(f"{kind}_audio", out[f"{kind}_audio"]))) if x in LANGUAGES]
        if langs:
            out[f"{kind}_audio"] = langs[:4]
        else:
            bad(f"Choose at least one audio language for {label}.")
        res = [x for x in dict.fromkeys(_list(raw.get(f"{kind}_resolutions", out[f"{kind}_resolutions"]))) if x in RESOLUTIONS]
        if res:
            out[f"{kind}_resolutions"] = res
        else:
            bad(f"Choose at least one resolution for {label}.")
        sub = str(raw.get(f"{kind}_subtitle", out[f"{kind}_subtitle"]))
        if sub == "none" or sub in LANGUAGES:
            out[f"{kind}_subtitle"] = sub
        else:
            bad("Unknown subtitle language.")
        audio = str(raw.get(f"{kind}_audio_filter", out[f"{kind}_audio_filter"]))
        if audio in ("", "und") or audio in LANGUAGES:
            out[f"{kind}_audio_filter"] = audio
        else:
            bad("Unknown language.")
    for key, low, high, label in (("check_minutes", 5, 240, "Check every"), ("keep_hours", 1, 24 * 30, "Keep watched episodes for")):
        try:
            n = int(str(raw.get(key, out[key])).strip())
            if not low <= n <= high:
                raise ValueError
            out[key] = n
        except (TypeError, ValueError):
            bad(f"{label} must be between {low} and {high}.")
    out["notify_ready"] = str(raw.get("notify_ready", out["notify_ready"])).lower() in ("1", "true", "yes", "on")
    language = str(raw.get("language", out["language"]))
    if language in ("en", "ar"):
        out["language"] = language
    else:
        bad("Unknown interface language.")
    clock = str(raw.get("clock", out["clock"]))
    if clock in ("auto", "12h", "24h"):
        out["clock"] = clock
    else:
        bad("Unknown clock format.")
    for key in ("library_movie", "library_series", "library_anime"):
        name = str(raw.get(key, out[key])).strip()
        if 1 <= len(name) <= 100 and not any(c in name for c in '<>:"/\\|?*'):
            out[key] = name
        else:
            bad("Choose a library.")
    for lang in subtitle_style.LANGUAGES:
        key = f"sub_style_{lang}"
        try:
            value = raw.get(key, out[key])
            out[key] = subtitle_style.clean(json.loads(value) if isinstance(value, str) else value)
        except (ValueError, TypeError) as e:
            bad(str(e) if str(e).startswith("Subtitle") else "Subtitle style is not valid.")
    if problems and not strict:
        out["_bad"] = True
    return out


def get(key=None):
    with _lock:
        try:
            stamp = FILE.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = None
        if stamp is None and _cache.get("damaged"):
            _cache.update(data=dict(DEFAULTS), damaged=False)   # file removed: back to the defaults
        if stamp != _cache["stamp"]:
            try:
                raw = json.loads(FILE.read_text(encoding="utf-8")) if stamp else {}
                if not isinstance(raw, dict):
                    raise ValueError("not an object")
                data = _clean(raw, base=_cache["data"])        # a bad value keeps the last good one
                damaged = bool(data.pop("_bad", False))   # an invalid value: keep it from cutting cleanup short
                _cache.update(stamp=stamp, data=data, damaged=damaged)
            except Exception:                      # unreadable: keep the last good values, retry on the next call
                _cache.update(stamp=None, damaged=True)
        data = dict(_cache["data"])
    return data if key is None else data[key]


def damaged():
    """True when settings.json exists but can't be read (cleanup then holds off rather than guess)."""
    get()
    with _lock:
        return bool(_cache.get("damaged"))


def minutes():
    """Seconds between checks for the DKB / Stremio loops; never raises (a bad setting must not stop them)."""
    try:
        return get("check_minutes") * 60
    except Exception:
        return DEFAULTS["check_minutes"] * 60


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
            "defaults": DEFAULTS, "fonts": list(subtitle_style.FONTS)}


def preview(params):
    """Settings page preview: the style exactly as the subtitles will look (drawn by libass)."""
    try:
        style = subtitle_style.clean(json.loads(params.get("style", "{}")))
    except (ValueError, TypeError) as e:
        raise SettingsError(str(e) if str(e).startswith("Subtitle") else "Subtitle style is not valid.") from None
    return {"image": subtitle_style.preview(style, params.get("scene") == "bright")}
