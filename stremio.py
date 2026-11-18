"""Use the user's Stremio add-ons to select and save media into Jellyfin."""
from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import unicodedata
import xml.etree.ElementTree as ET

import egress
import psutil

import agent
import stremio_account

TOKEN = secrets.token_urlsafe(32)
_items = {}
_lock = threading.RLock()
_job_lock = threading.Lock()
_cancel_job = threading.Event()
_job_process = None
_job_state = None          # the running download's job dict (pause/resume update it in place)
_requests = {}
_pool = ThreadPoolExecutor(max_workers=12)
JOB = Path(agent.HERE) / "stremio-job.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class UserError(ValueError):
    pass


class SourceError(UserError):
    pass


def remember(value):
    with _lock:
        for key in list(_items):
            if time.time() - _items[key][0] > 7200:
                del _items[key]
        if len(_items) >= 2000:
            del _items[next(iter(_items))]  # Long queues/search sessions must not exhaust the selection cache.
        key = secrets.token_urlsafe(18)
        _items[key] = (time.time(), value)
        return key


def item(key, kind):
    with _lock:
        found = _items.get(key)
    if not found or time.time() - found[0] > 7200 or found[1].get("kind") != kind:
        raise UserError("This selection expired. Search for the title again.")
    return found[1]


ADDON_LOCAL_PORTS = (11470, 8096)   # local add-ons (Stremio service, Jellyfin add-on): JSON only
MEDIA_LOCAL_PORTS = (11470,)        # media may only come from Stremio's local torrent server, never Jellyfin


def is_local(url, ports):
    p = urllib.parse.urlsplit(url)
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1") and p.port in ports


def safe_url(url, local=()):
    """Reject non-http, credentials in URLs and non-public hosts. local: loopback ports allowed as-is."""
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise UserError("This source does not provide an HTTP download.")
    if local and is_local(url, local):
        return url
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError()
    except (OSError, ValueError):
        raise UserError("This source does not have a public download address.") from None
    return url


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def get_json(url, data=None, timeout=75):
    safe_url(url, local=ADDON_LOCAL_PORTS)
    headers = {"User-Agent": "MyAnimeWatcher/1.0", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    handlers = [urllib.request.ProxyHandler({}), Redirects()]
    if not is_local(url, ADDON_LOCAL_PORTS):
        handlers.append(egress.AlwaysProxy())     # every hop re-checked at connect time (no DNS rebinding)
    with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as response:
        raw = response.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise UserError("The add-on response is too large.")
        return json.loads(raw)


def safe_poster(url):
    """Posters are loaded by the app window itself: only https with a real DNS name (no IPs, no LAN names)."""
    p = urllib.parse.urlsplit(url if isinstance(url, str) else "")
    host = p.hostname or ""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return ""
    except ValueError:
        pass
    lan_name = "." not in host or host.endswith((".local", ".lan", ".home", ".localhost"))
    if p.scheme != "https" or lan_name or p.username or p.password:
        return ""
    return url


def addons():
    return stremio_account.load()["addons"]


def supports(addon, resource, media_type, media_id=""):
    manifest = addon.get("manifest", {})
    for r in manifest.get("resources", []):
        if isinstance(r, str):
            if r == resource and media_type in manifest.get("types", []):
                return True
        elif r.get("name") == resource and media_type in r.get("types", manifest.get("types", [])):
            prefixes = r.get("idPrefixes")
            if not prefixes or any(media_id.startswith(p) for p in prefixes):
                return True
    return False


def resource_future(addon, name, media_type, media_id, extra=None):
    base = addon["transportUrl"]
    if not base.endswith("/manifest.json"):
        raise UserError("This add-on uses an unsupported connection format.")
    path = "/".join(urllib.parse.quote(str(v), safe="") for v in (name, media_type, media_id))
    if extra:
        path += "/" + urllib.parse.urlencode(extra, quote_via=urllib.parse.quote)
    url = base[:-len("manifest.json")] + path + ".json"
    with _lock:
        now = time.monotonic()
        for key in list(_requests):
            created, future = _requests[key]
            if now - created > 300 or (future.done() and future.exception() is not None):
                del _requests[key]
        if url not in _requests:
            if len(_requests) >= 128:
                del _requests[next(iter(_requests))]
            timeout = 40 if name == "stream" else 12
            _requests[url] = (now, _pool.submit(get_json, url, timeout=timeout))
        future = _requests[url][1]
        future.provider_name = addon.get("manifest", {}).get("name")
        future.catalog_search = bool(extra and "search" in extra)
        future.media_type = media_type
        return future


def resource(addon, name, media_type, media_id, extra=None):
    return resource_future(addon, name, media_type, media_id, extra).result()


def parallel(tasks, timeout=8):
    results, failures = [], 0
    futures = []
    for fn, args in tasks:
        try:
            futures.append(resource_future(*args) if fn is resource else _pool.submit(fn, *args))
        except Exception:
            failures += 1
    done, pending = wait(futures, timeout=timeout)
    failures += len(pending)  # Late add-ons keep running; refresh reuses their completed results.
    for future in done:
        try:
            value = future.result()
            if isinstance(value, dict) and getattr(future, "provider_name", None):
                value = dict(value, _provider=future.provider_name, _catalog_search=future.catalog_search,
                             _media_type=future.media_type)
            results.append(value)
        except Exception:
            failures += 1  # Add-on exceptions can contain private configured URLs.
    return results, failures


def connection():
    import stremio_batch
    installed = addons()
    return {"addons": [a.get("manifest", {}).get("name", "Add-on") for a in installed],
            "libraries": libraries(), "job": job_status(), "batch": stremio_batch.status()}


def sync():
    saved = stremio_account.load()
    if not saved.get("authKey"):
        raise UserError("Reimport your signed-in Stremio profile to sync add-ons.")
    response = get_json("https://api.strem.io/api/addonCollectionGet",
                        {"authKey": saved["authKey"], "update": True, "type": "AddonCollectionGet"})
    result = response.get("result")
    installed = result.get("addons") if isinstance(result, dict) else result
    if not isinstance(installed, list) or not installed or not all(isinstance(a, dict) and a.get("transportUrl") for a in installed):
        raise UserError("Stremio could not sync the account. Reimport your signed-in profile.")
    pending = [(a, _pool.submit(get_json, a["transportUrl"], timeout=12)) for a in installed]
    done, _ = wait([future for _, future in pending], timeout=8)
    refresh_failures = 0
    for addon, future in pending:
        try:
            if future not in done:
                raise TimeoutError()
            manifest = future.result()
            if not isinstance(manifest, dict) or not manifest.get("id") or not isinstance(manifest.get("resources"), list):
                raise ValueError()
            addon["manifest"] = manifest
        except Exception:
            refresh_failures += 1  # Keep the previous manifest when an add-on is temporarily offline.
    saved["addons"] = installed
    stremio_account.save(saved)
    with _lock:
        _requests.clear()
    result = connection()
    result["manifest_refresh_failures"] = refresh_failures
    return result


def search(query, media_type):
    query = query.strip()
    if not 2 <= len(query) <= 150 or media_type not in ("all", "movie", "series", "anime"):
        raise UserError("Enter at least two characters to search.")
    types = ("movie", "series", "anime") if media_type == "all" else (media_type,)
    tasks = []
    for addon in addons():
        for catalog in addon.get("manifest", {}).get("catalogs", []):
            catalog_type = catalog.get("type")
            if catalog_type not in types:
                continue
            if any(e.get("name") == "search" for e in catalog.get("extra", [])):
                tasks.append((resource, (addon, "catalog", catalog_type, catalog["id"], {"search": query})))
            elif "Bidooo" in addon.get("manifest", {}).get("name", ""):
                # Bidooo has no search endpoint: search its cached catalog locally.
                tasks.append((resource, (addon, "catalog", catalog_type, catalog["id"])))
    results, failures = parallel(tasks)
    found = {}
    for result in results:
        for meta in result.get("metas", []):
            if result.get("_catalog_search") is False:
                text = str(meta.get("name", "")).casefold()
                if not all(word in text for word in query.casefold().split()):
                    continue
            if not meta.get("id") or not meta.get("name"):
                continue
            mt = meta.get("type") or result.get("_media_type", media_type)
            identity = (mt, meta["id"])
            if identity in found:
                continue
            key = remember({"kind": "title", "meta": meta, "type": mt})
            poster = meta.get("poster", "")
            found[identity] = {"key": key, "name": meta["name"], "type": mt,
                               "year": meta.get("releaseInfo", ""),
                               "poster": safe_poster(poster)}
    values = list(found.values())
    if media_type == "all":
        groups = [[v for v in values if v["type"] == mt][:20] for mt in types]
        values = [group[i] for i in range(20) for group in groups if i < len(group)]
    return {"results": values[:60], "failed_addons": failures}


def open_anilist(anilist_id):
    """A season card (AniList id) opens that exact show: the Anime Kitsu catalog maps it to the IMDb show and
    season the library already uses, so it lands on the same page and folders a search would."""
    if not str(anilist_id).isdigit():
        raise UserError("That show could not be opened.")
    meta = {}
    for kind in ("series", "movie"):
        try:
            meta = get_json(f"https://anime-kitsu.strem.fun/meta/{kind}/anilist:{anilist_id}.json", timeout=20).get("meta") or {}
        except Exception:
            meta = {}
        if meta.get("id"):
            break
    if not meta.get("id"):
        raise UserError("That show isn't in the anime catalog yet. Search for it by name instead.")
    kind = "movie" if meta.get("type") == "movie" else "series"
    first = (meta.get("videos") or [{}])[0]
    imdb = meta.get("imdb_id") if re.fullmatch(r"tt\d+", str(meta.get("imdb_id") or "")) else ""
    season = first.get("imdbSeason") if imdb and isinstance(first.get("imdbSeason"), int) else None
    title = {"kind": "title", "type": kind,
             "meta": {"id": imdb or meta["id"], "name": meta.get("name") or "Untitled", "poster": meta.get("poster", ""),
                      "releaseInfo": meta.get("releaseInfo", "")}}
    return {"key": remember(title), "type": kind, "name": title["meta"]["name"], "poster": safe_poster(meta.get("poster", "")),
            "year": str(meta.get("releaseInfo") or "")[:9], "season": season}


def details(key):
    selected = item(key, "title")
    meta = selected["meta"]
    results, _ = parallel([(resource, (a, "meta", selected["type"], meta["id"])) for a in addons()
                           if selected["type"] != "movie" and supports(a, "meta", selected["type"], meta["id"])])
    richer = [r["meta"] for r in results if isinstance(r.get("meta"), dict) and r["meta"].get("name")]
    if richer:
        meta = max(richer, key=lambda m: len(m.get("videos", [])))
    if selected["type"] != "movie" and not meta.get("videos") and str(meta.get("id", "")).startswith("kitsu:"):
        # anime catalogs give Kitsu ids that Cinemeta and most add-ons can't open: the public Anime Kitsu
        # catalog has their episodes (and art)
        try:
            kitsu = get_json(f"https://anime-kitsu.strem.fun/meta/series/{meta['id']}.json", timeout=20).get("meta") or {}
            if kitsu.get("videos"):
                meta = dict(kitsu, id=meta["id"], name=meta.get("name") or kitsu.get("name"))
        except Exception:
            pass
    selected["meta"] = meta
    videos = []
    for v in meta.get("videos", []):
        if not v.get("id") or v.get("season") is None or v.get("episode") is None:
            continue
        video = {"kind": "video", "type": selected["type"], "id": v["id"], "meta": meta,
                 "season": int(v["season"]), "episode": int(v["episode"])}
        videos.append({"key": remember(video), "id": video["id"], "season": video["season"], "episode": video["episode"],
                       "name": v.get("title") or v.get("name") or "Episode", "released": v.get("released", "")})
    if not videos and selected["type"] == "movie":
        videos = [{"key": remember({"kind": "video", "type": "movie", "id": meta["id"], "meta": meta}),
                   "name": meta["name"]}]
    genres = meta.get("genres") or meta.get("genre") or []
    return {"name": meta["name"], "description": meta.get("description", ""), "videos": videos,
            "poster": safe_poster(meta.get("poster", "")), "background": safe_poster(meta.get("background", "")),
            "genres": [str(g) for g in genres if isinstance(g, str)][:3], "year": str(meta.get("releaseInfo") or meta.get("year") or "")[:9],
            "rating": str(meta.get("imdbRating") or ""), "runtime": str(meta.get("runtime") or "")[:12]}


def quality(stream):
    text = str(stream.get("behaviorHints", {}).get("filename", "")) + " "
    text += " ".join(str(stream.get(k, "")) for k in ("name", "title", "description"))
    text = unicodedata.normalize("NFKC", text).replace("ᴘ", "p")
    m = re.search(r"\b(2160|1440|1080|720|576|480)p\b", text, re.I)
    if m:
        return m.group(1) + "p"
    for pattern, label in ((r"\b(4k|uhd)\b", "2160p"), (r"\bfhd\b", "1080p"), (r"\bhd\b", "720p")):
        if re.search(pattern, text, re.I):
            return label
    return "Unspecified"


def dynamic_range(stream):
    text = " ".join(str(stream.get(k, "")) for k in ("name", "title", "description"))
    text += " " + str(stream.get("behaviorHints", {}).get("filename", ""))
    text = unicodedata.normalize("NFKC", text)
    for pattern, label in ((r"\b(dv|dovi|dolby[ ._-]*vision)\b", "Dolby Vision"),
                           (r"\bhdr10\+", "HDR10+"), (r"\bhlg\b", "HLG"),
                           (r"\bhdr10\b", "HDR10"), (r"\bhdr\b", "HDR"), (r"\bsdr\b", "SDR")):
        if re.search(pattern, text, re.I):
            return label
    return "Unspecified"


def verified_range(video):
    side = json.dumps(video.get("side_data_list", [])).lower()
    if "dovi" in side or "dolby vision" in side:
        return "Dolby Vision"
    if "2094-40" in side or "hdr10+" in side:
        return "HDR10+"
    transfer = video.get("color_transfer")
    if transfer == "smpte2084":
        return "HDR10"
    if transfer == "arib-std-b67":
        return "HLG"
    if transfer in ("bt709", "smpte170m", "bt470m", "bt470bg", "iec61966-2-1"):
        return "SDR"
    return "Unspecified"


# Only explicit audio metadata/labels count; filename languages can describe subtitles.
AUDIO_LANGUAGES = {
    "en": ("English", "eng", "GB", "US", "AU"), "ja": ("Japanese", "jpn", "JP"),
    "ar": ("Arabic", "ara", "SA", "AE", "EG"), "es": ("Spanish", "spa", "ES", "MX"),
    "fr": ("French", "fra", "fre", "FR"), "de": ("German", "deu", "ger", "DE"),
    "it": ("Italian", "ita", "IT"), "ko": ("Korean", "kor", "KR"),
    "zh": ("Chinese", "zho", "chi", "CN", "TW"), "pt": ("Portuguese", "por", "PT", "BR"),
    "ru": ("Russian", "rus", "RU"), "hi": ("Hindi", "hin"), "ta": ("Tamil", "tam"),
    "te": ("Telugu", "tel"), "tr": ("Turkish", "tur", "TR"), "nl": ("Dutch", "nld", "dut", "NL"),
    "pl": ("Polish", "pol", "PL"), "sv": ("Swedish", "swe", "SE"), "fi": ("Finnish", "fin", "FI"),
    "da": ("Danish", "dan", "DK"), "no": ("Norwegian", "nor", "NO"), "th": ("Thai", "tha", "TH"),
    "vi": ("Vietnamese", "vie", "VN"), "id": ("Indonesian", "ind", "ID"), "he": ("Hebrew", "heb", "IL"),
    "fa": ("Persian", "fas", "per", "IR"), "uk": ("Ukrainian", "ukr", "UA"),
}


def reported_audio(stream):
    tracks = stream.get("audioTracks", stream.get("behaviorHints", {}).get("audioTracks"))
    if isinstance(tracks, list):
        languages = []
        for t in tracks:
            value = t.get("language", t.get("lang", "")) if isinstance(t, dict) else t
            if not isinstance(value, str):
                continue
            value = value.strip().lower()
            code = next((code for code, names in AUDIO_LANGUAGES.items()
                         if value in [code] + [n.lower() for n in names]), value)
            if re.fullmatch(r"[a-z]{2,3}", code) and code != "und":
                languages.append(code)
        return sorted(set(languages))
    languages = set()
    text = str(stream.get("description") or stream.get("title") or "")
    for line in text.splitlines():
        audio = re.search(r"🗣️?\s*(.*)", line)
        if not audio:
            audio = re.match(r"\s*(?:audio(?: languages?)?|dub(?:bed)?|الصوت|لغة الصوت)\s*[:=]\s*(.*)", line, re.I)
        if not audio:
            continue
        label = re.split(r"\b(?:subs?|subtitles?)\s*[:=]|📝|💬", audio.group(1), maxsplit=1, flags=re.I)[0]
        flags = re.findall(r"[\U0001f1e6-\U0001f1ff]{2}", label)
        label = re.sub(r"[\U0001f1e6-\U0001f1ff]{2}", " ", label)
        countries = {"".join(chr(ord(c) - 0x1f1e6 + ord('A')) for c in flag) for flag in flags}
        for code, names in AUDIO_LANGUAGES.items():
            if countries.intersection(names) or any(re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", label, re.I)
                                                    for n in (code,) + names):
                languages.add(code)
    return sorted(languages)


def stream_url(stream):
    if stream.get("url"):
        url = safe_url(stream["url"], local=MEDIA_LOCAL_PORTS)
        p = urllib.parse.urlsplit(url)
        if is_local(url, MEDIA_LOCAL_PORTS) and not egress.local_media_path(p.path, p.query):
            raise UserError("This source points at an unsupported local address. Choose another source.")
        return url
    info_hash = stream.get("infoHash", "")
    if re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
        idx = stream.get("fileIdx")
        if not isinstance(idx, int) or idx < 0:
            raise UserError("This torrent has no episode file index. Choose another source.")
        return f"http://127.0.0.1:11470/{info_hash}/{idx}"
    raise UserError("This source only opens an external player. Choose a downloadable source.")


AIO_ERROR_FILES = {"download_failed.mp4", "downloading.mp4", "unavailable_for_legal_reasons.mp4",
                   "store_limit_exceeded.mp4", "content_proxy_limit_reached.mp4", "no_matching_file.mp4",
                   "payment_required.mp4", "500.mp4", "429.mp4", "403.mp4", "401.mp4", "200.mp4"}


def reject_error_url(url, provider=""):
    decoded = urllib.parse.unquote(urllib.parse.unquote(str(url))).lower()
    for name in AIO_ERROR_FILES:
        if not re.search(r"(?:^|[/=])" + re.escape(name) + r"(?:$|[?&#/])", decoded):
            continue
        if not name[0].isdigit() or "aiostream" in provider.lower() or "/static/" in decoded or "/errors/" in decoded:
            raise UserError("The source returned a provider error video instead of the episode/movie. Choose another release.")


def resolve_media(source, headers):
    url = stream_url(source["stream"])
    provider = source.get("provider", "")
    reject_error_url(url, provider)
    reject_error_url(source["stream"].get("behaviorHints", {}).get("filename", ""), provider)
    if source["stream"].get("streamData", {}).get("error"):
        raise UserError("The provider marked this release unavailable. Choose another release.")
    if time.monotonic() - source.get("resolved_at", -1000) < 300:
        return source["resolved_url"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), egress.AlwaysProxy(), Redirects())
    request_headers = {"User-Agent": "MyAnimeWatcher/1.0", **headers}
    try:
        try:
            response = opener.open(urllib.request.Request(url, headers=request_headers, method="HEAD"), timeout=20)
        except urllib.error.HTTPError as e:
            if e.code not in (405, 501):
                raise
            response = opener.open(urllib.request.Request(url, headers={**request_headers, "Range": "bytes=0-0"}), timeout=20)
        with response:
            resolved = response.url
            reject_error_url(resolved, provider)
            reject_error_url(response.headers.get_filename() or "", provider)
            if response.headers.get_content_type() in ("text/html", "application/json"):
                raise UserError("This source returned an error page instead of media. Choose another release.")
    except UserError:
        raise
    except Exception:
        raise UserError("The source could not provide a playable media file. Choose another release.") from None
    source.update(resolved_url=resolved, resolved_at=time.monotonic())
    return resolved


def positive_number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else 0
    except (ValueError, TypeError):
        return 0


def expected_duration(source):
    stream = source["stream"]
    direct = positive_number(stream.get("streamData", {}).get("duration") or stream.get("duration"))
    if direct:
        return direct
    description = str(stream.get("description") or stream.get("title") or "")
    clock = re.search(r"⏱️?\s*([^\n]+)", description)
    runtime = clock.group(1) if clock else str(source.get("video", {}).get("meta", {}).get("runtime") or "")
    if not clock and source.get("video", {}).get("season") == 0:
        return 0  # A show's average episode length does not describe its Specials.
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", runtime, re.I)
    return sum(float(n) * (3600 if unit.lower().startswith('h') else 60 if unit.lower().startswith('m') else 1)
               for n, unit in parts)


def validate_media(source, data, actual_size=None):
    if source["stream"].get("streamData", {}).get("error"):
        raise UserError("The provider returned an error instead of media.")
    provider = source.get("provider", "")
    for url in (source["stream"].get("url", ""), source.get("resolved_url", ""), data.get("format", {}).get("filename", "")):
        reject_error_url(url, provider)
    tags = str(data.get("format", {}).get("tags", {})).lower()
    if "unavailable for legal reasons" in tags or ("aiostreams" in tags and any(t in tags for t in ("error", "payment required", "download failed"))):
        raise UserError("The provider returned an error video instead of the requested media.")
    duration = positive_number(data.get("format", {}).get("duration"))
    expected = expected_duration(source)
    size = positive_number(actual_size or data.get("format", {}).get("size"))
    advertised = positive_number(source["stream"].get("behaviorHints", {}).get("videoSize") or source["stream"].get("streamData", {}).get("size"))
    if (0 < duration <= 125 and expected >= 300 and duration < expected * .4
            or size and advertised >= 20e6 and size < min(advertised * .05, 5e6) and duration <= 125):
        raise UserError("This looks like a short error/placeholder clip and does not match the advertised episode/movie. It was rejected.")


def streams(key):
    video = item(key, "video")
    installed = addons()
    tasks = [(resource, (a, "stream", video["type"], video["id"])) for a in installed
             if supports(a, "stream", video["type"], video["id"])]
    results, failures = parallel(tasks, timeout=15)
    out, seen, providers = [], set(), []
    for result in results:
        provider = result.get("_provider", "Add-on")
        providers.append({"name": provider, "count": len(result.get("streams", []))})
        for stream in result.get("streams", []):
            identity = (stream.get("url"), stream.get("infoHash"), stream.get("fileIdx"))
            if identity in seen or not (stream.get("url") or stream.get("infoHash")):
                continue
            seen.add(identity)
            label = str(stream.get("description") or stream.get("title") or stream.get("name") or "Source")
            label = re.sub(r"https?://\S+", "[link]", label)[:1200]
            skey = remember({"kind": "source", "video": video, "stream": stream, "provider": provider})
            languages = reported_audio(stream)
            if not languages and "Bidooo" in provider:
                languages = ["ar"]  # Bidooo explicitly describes its own catalog as Arabic dubbed; still unverified.
            out.append({"key": skey, "name": stream.get("name", "Source"), "description": label, "provider": provider,
                        "quality": quality(stream), "dynamic_range": dynamic_range(stream),
                        "audio_languages": languages, "audio_verified": False,
                        "size": stream.get("behaviorHints", {}).get("videoSize")})
    return {"sources": out[:120], "failed_addons": failures, "providers": providers}


def input_args(source):
    args = ["-protocol_whitelist", egress.PROTOCOLS]
    headers = source["stream"].get("behaviorHints", {}).get("proxyHeaders", {}).get("request", {})
    if headers:
        if not isinstance(headers, dict) or any(re.search(r"[\r\n]", str(k) + str(v)) for k, v in headers.items()):
            raise UserError("The source returned invalid request headers.")
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    url = resolve_media(source, headers)
    if str(url).lower().startswith(("http://", "https://")):
        # pick up again where it stopped when the source drops the connection (network blip, long pause)
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
                 "-reconnect_delay_max", "30"]
    return args + ["-rw_timeout", "30000000", "-i", url]


def probe(source):
    cmd = ["ffprobe", "-v", "error", "-probesize", "8000000", "-analyzeduration", "8000000"]
    cmd += input_args(source) + ["-show_streams", "-show_format", "-of", "json"]
    result = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW, timeout=60, env=egress.ffmpeg_env())
    if result.returncode:
        raise UserError("This source could not be inspected. It may be offline; try another release.")
    data = json.loads(result.stdout)
    if not any(s.get("codec_type") == "video" for s in data.get("streams", [])):
        raise UserError("This source did not return a video.")
    validate_media(source, data)
    return data


def track(stream):
    tags = stream.get("tags", {})
    lang = tags.get("language", "und")
    return {"id": str(stream["index"]), "language": lang, "role": track_role(stream),
            "label": " · ".join(str(x) for x in (lang, tags.get("title"), stream.get("codec_name"),
                                                   f"{stream['channels']} channels" if stream.get("channels") else None) if x)}


def track_role(stream):
    disposition = stream.get("disposition", {})
    title = str(stream.get("tags", {}).get("title", stream.get("label", ""))).lower()
    return {"commentary": bool(disposition.get("comment") or re.search(r"commentary|تعليق", title)),
            "forced": bool(disposition.get("forced") or re.search(r"\bforced\b", title)),
            "hearing_impaired": bool(disposition.get("hearing_impaired") or re.search(r"\b(sdh|hearing.impaired|cc)\b", title))}


def inspect(key):
    source = item(key, "source")
    if time.monotonic() - source.get("inspected_at", -1000) < 300:
        return source["inspection"]
    data = probe(source)
    source["probe"] = data
    audio = [track(s) for s in data["streams"] if s.get("codec_type") == "audio"]
    subtitles = [track(s) for s in data["streams"] if s.get("codec_type") == "subtitle"]
    subtitles += external_tracks(source, source["stream"].get("subtitles") or [])
    v = next(s for s in data["streams"] if s.get("codec_type") == "video")
    result = {"audio": audio, "subtitles": subtitles, "width": v.get("width"), "height": v.get("height"),
              "duration": data.get("format", {}).get("duration"), "quality": quality(source["stream"]),
              "dynamic_range": verified_range(v)}
    source.update(inspection=result, inspected_at=time.monotonic())
    return result


def external_tracks(source, subtitles):
    out = []
    source.setdefault("external", {})
    for sub in subtitles[:200]:
        if not isinstance(sub, dict) or not sub.get("url"):
            continue
        sid = "ext:" + hashlib.sha256((sub["url"] + str(sub.get("lang", ""))).encode()).hexdigest()[:24]
        source["external"][sid] = sub
        out.append({"id": sid, "language": sub.get("lang", "und"),
                    "role": track_role(sub),
                    "label": f"{sub.get('lang', 'und')} · add-on subtitle"})
    return out


def subtitle_options(key):
    source = item(key, "source")
    hints = source["stream"].get("behaviorHints", {})
    extra = {k: hints[k] for k in ("videoHash", "videoSize", "filename") if hints.get(k)}
    video = source["video"]
    results, failures = parallel([(resource, (a, "subtitles", video["type"], video["id"], extra)) for a in addons()
                                  if supports(a, "subtitles", video["type"], video["id"])], timeout=5)
    external = []
    for result in results:
        external.extend(result.get("subtitles") or [])
    return {"subtitles": external_tracks(source, external), "failed_addons": failures}


LIBRARY_ROOT = Path(r"C:\ProgramData\Jellyfin\Server\root\default")


def library_id(name, path):
    """Stable id: the same library folder keeps its id when another library folder is missing or added."""
    return name + ":" + hashlib.sha1(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:8]


def libraries(_legacy=False):
    out = []
    for options in LIBRARY_ROOT.glob("*/options.xml"):
        try:
            for p in ET.parse(options).getroot().findall(".//MediaPathInfo/Path"):
                if p.text and Path(p.text).is_dir():
                    out.append({"id": options.parent.name + ":" + str(len(out)) if _legacy else library_id(options.parent.name, p.text),
                                "name": options.parent.name, "path": p.text})
        except (OSError, ET.ParseError):
            continue
    return out


def find_library(wanted):
    """The library for an id from the page or a saved batch; batches saved before 2026-11-18 carry the old
    position-based id ("Anime:0"), still resolved the old way."""
    found = next((lib for lib in libraries() if lib["id"] == wanted), None)
    if found:
        return found
    old = next((lib for lib in libraries(_legacy=True) if lib["id"] == wanted), None)
    return old and dict(old, id=library_id(old["name"], old["path"]))


def clean_name(text):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(text)).strip(" .")[:130].rstrip(" .")
    if not name or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name):
        raise UserError("The title cannot be used as a folder name.")
    return name


_ORIGINAL = {}
TRANSLATED = re.compile("[\u0600-\u06FF]")


def original_name(video):
    """The show's original title for folders: catalogs such as an Arabic add-on give translated names (Bleach ->
    "المطهر"), which would split a show already in the library into a second folder. Cinemeta (by IMDb id) has
    the original; without an IMDb id, or offline, the catalog's name is kept."""
    meta = video["meta"]
    name = str(meta.get("name", ""))
    if not TRANSLATED.search(name):
        return name
    imdb = next((str(x).split(":")[0] for x in (meta.get("id", ""), video.get("id", "")) if str(x).startswith("tt")), "")
    if imdb and imdb not in _ORIGINAL:
        kind = "movie" if video.get("type") == "movie" else "series"
        try:
            found = (get_json(f"https://v3-cinemeta.strem.io/meta/{kind}/{imdb}.json", timeout=15).get("meta") or {}).get("name")
            if found and not TRANSLATED.search(found):
                _ORIGINAL[imdb] = found
        except Exception:
            pass
    return _ORIGINAL.get(imdb) or name


def target(video, library):
    meta = video["meta"]
    year = re.search(r"\b(19|20)\d{2}\b", str(meta.get("releaseInfo", "")))
    name = clean_name(original_name(video) + (f" ({year.group()})" if year else ""))
    root = Path(library["path"]).resolve()
    if video["type"] == "movie":
        dest = root / name / (name + ".mkv")
    else:
        season, ep = video["season"], video["episode"]
        dest = root / name / f"Season {season:02d}" / f"{name} - S{season:02d}E{ep:02d}.mkv"
    if not dest.resolve().is_relative_to(root):
        raise UserError("The destination must stay inside the Jellyfin library.")
    return dest


def write_job(job):
    with _lock:
        job["updated"] = time.time()
        agent.write_atomic(JOB, json.dumps(job, ensure_ascii=False))


def read_job():
    """Readers take the same lock as write_job: on Windows an open reader makes the writer's swap fail, which
    used to kill the running download (status polling every 2.5 s)."""
    with _lock:
        return json.loads(JOB.read_text(encoding="utf-8"))


def dismiss_download():
    """Hide a finished/failed download everywhere (saved in the job file); a running one must be cancelled first."""
    if _job_lock.locked():
        raise UserError("A download is running; cancel it first.")
    try:
        data = read_job()
    except (OSError, ValueError):
        return None
    write_job(dict(data, dismissed=True))
    return None


def job_status():
    try:
        data = read_job()
        if data.get("dismissed") and not _job_lock.locked():
            return None
        if data.get("processing_key"):
            import library_processing
            with library_processing._guard:
                record = library_processing.read().get(data["processing_key"])
            if record and record.get("updated", 0) >= data.get("started", 0):   # not a record from an earlier download
                busy = (record["status"] == "running" and library_processing._processor.locked()) or _job_lock.locked()
                data.update(status="done" if record["status"] == "done" else "running" if busy else "waiting",
                            stage=record["stage"], pct=100 if record["status"] == "done" else min(99, 90 + record.get("pct", 0) * .09),
                            path=record["destination"], eta_seconds=None)
                return data
        if data.get("status") == "running" and not _job_lock.locked():
            data.update(status="interrupted", stage="The app restarted. Select the source again to retry.")
            part = data.get("part")
            # The half download of a dead job is never resumed; subtitle preparation owns the file once it started.
            if part and part.endswith(".stremio.part") and data.get("phase") != "processing":
                try:
                    Path(part).unlink(missing_ok=True)
                except OSError:
                    pass
        return data
    except (OSError, ValueError):
        return None


def cancel_download():
    if not _job_lock.locked():
        raise UserError("No single download is running.")
    with _lock:
        try:
            current = read_job()
        except (OSError, ValueError):
            current = {}
        if current.get("phase") == "processing":
            raise UserError("Subtitle preparation has started and cannot be cancelled safely.")
        _cancel_job.set()
    if _job_process and _job_process.poll() is None:
        _job_process.terminate()
    return {"status": "cancelling", "stage": "Stopping download"}


def start_download(key, audio, subtitle, library_id, _batch=False):
    import stremio_batch
    if not _batch and stremio_batch.active():
        raise UserError("A season/show download is active. Stop its remaining episodes before starting another download.")
    source = item(key, "source")
    if "probe" not in source:
        raise UserError("Inspect this source before choosing its tracks.")
    tracks = source["probe"]["streams"]
    audio_ids = {str(t["index"]) for t in tracks if t.get("codec_type") == "audio"}
    subtitle_ids = {str(t["index"]) for t in tracks if t.get("codec_type") == "subtitle"}
    if audio not in audio_ids and not (not audio_ids and audio == "none"):
        raise UserError("Choose one of the audio tracks available in this source.")
    if subtitle not in subtitle_ids | set(source.get("external", {})) | {"none"}:
        raise UserError("Choose one of the available subtitle tracks, or no subtitles.")
    library = find_library(library_id)
    if not library:
        raise UserError("Choose an existing Jellyfin library.")
    destination = target(source["video"], library)
    if destination.exists():
        raise UserError("This episode or movie already exists in that library.")
    import library_processing
    with library_processing._guard:
        pending = library_processing.read().get(library_processing.key_for(destination))
    if pending and pending["status"] != "done":
        raise UserError("This item is already downloaded and waiting for subtitle preparation. Check the processing status; it will retry automatically.")
    size = source["stream"].get("behaviorHints", {}).get("videoSize") or source["probe"].get("format", {}).get("size")
    required = max(float(size or 0) * 1.1, 2e9)
    if shutil.disk_usage(library["path"]).free < required:
        raise UserError("There is not enough free space in this library.")
    if not _job_lock.acquire(blocking=False):
        raise UserError("A download is already running. Wait for it to finish.")
    job = {"name": source["video"]["meta"]["name"], "status": "running", "stage": "Starting download",
           "pct": 0, "library": library["name"], "started": time.time()}
    _cancel_job.clear()
    if source["video"]["type"] != "movie":
        job.update(season=source["video"]["season"], episode=source["video"]["episode"])
    try:
        write_job(job)
    except Exception:
        _job_lock.release()
        raise
    if _batch:
        download(source, audio, subtitle, destination, job)
    else:
        try:
            threading.Thread(target=download, args=(source, audio, subtitle, destination, job), daemon=True).start()
        except Exception:
            _job_lock.release()
            raise
    return job


def estimate_remaining(started, fraction, now=None):
    elapsed = (time.time() if now is None else now) - started
    if fraction <= 0 or elapsed <= 0:
        return None
    return max(0, elapsed * (1 - fraction) / fraction)


def pause_download():
    """Freeze the running ffmpeg download (nothing is lost; resume continues it)."""
    pr, job = _job_process, _job_state
    if not _job_lock.locked() or pr is None or pr.poll() is not None or job is None:
        raise UserError("No download is running.")
    if job.get("phase") == "processing":
        raise UserError("Subtitle preparation has started and cannot be paused.")
    if not job.get("paused"):
        psutil.Process(pr.pid).suspend()
        job.update(paused=True, paused_at=time.time(), stage="Paused")
        write_job(job)
    return job


def resume_download():
    pr, job = _job_process, _job_state
    if job is None or not job.get("paused"):
        raise UserError("The download isn't paused.")
    if pr is not None and pr.poll() is None:
        psutil.Process(pr.pid).resume()
    job["started"] = job.get("started", time.time()) + time.time() - job.pop("paused_at", time.time())  # ETA ignores the pause
    job.update(paused=False, stage="Downloading before subtitle preparation")
    write_job(job)
    return job


PAUSE_RECHECK = 60          # seconds between time-limit checks while a download is paused


def download_limit(duration):
    return min(86400, max(3600, duration * 8))


def download(source, audio, subtitle, destination, job):
    global _job_process, _job_state
    _job_state = job
    temp = destination.with_name(destination.stem + ".stremio.part")
    job["part"] = str(temp)                 # lets a later status check remove it if the app dies mid-download
    preserve_raw = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y"] + input_args(source)
        except UserError as e:
            raise SourceError(str(e)) from None
        external = source.get("external", {}).get(subtitle)
        if external:
            cmd += ["-protocol_whitelist", egress.PROTOCOLS, "-rw_timeout", "30000000",
                    "-i", safe_url(external["url"])]
        cmd += ["-map", "0:v:0"]
        if audio != "none":
            cmd += ["-map", f"0:{audio}"]
        if subtitle != "none":
            cmd += ["-map", "1:0" if external else f"0:{subtitle}"]
        import language                    # keep the source's own text subtitles in the subtitle language
        arabic_subs = [t for t in source["probe"]["streams"] if t.get("codec_type") == "subtitle"
                       and language.is_code(t.get("tags", {}).get("language", ""))
                       and str(t["index"]) != subtitle and t.get("codec_name") in ("ass", "ssa", "subrip", "webvtt", "mov_text")]
        for track in arabic_subs:
            cmd += ["-map", f"0:{track['index']}"]
        cmd += ["-map_metadata", "0", "-c", "copy", "-disposition:a:0", "default"]
        if subtitle != "none":
            st = next((t for t in source["probe"]["streams"] if str(t["index"]) == subtitle), {})
            if external or st.get("codec_name") in ("mov_text", "webvtt"):
                cmd += ["-c:s", "srt"]
            cmd += ["-disposition:s:0", "default"]
            if external and re.fullmatch(r"[a-z]{2,3}", external.get("lang", "")):
                cmd += ["-metadata:s:s:0", f"language={external['lang']}"]
        for index, track in enumerate(arabic_subs, start=int(subtitle != "none")):
            if track.get("codec_name") in ("webvtt", "mov_text"):
                cmd += [f"-c:s:{index}", "srt"]
        cmd += ["-progress", "pipe:1", "-nostats", "-f", "matroska", str(temp)]
        # Never log ffmpeg stderr: it may echo signed download URLs or request headers.
        duration = float(source["probe"].get("format", {}).get("duration") or 0)
        last = 0
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, creationflags=NO_WINDOW, env=egress.ffmpeg_env()) as pr:
            _job_process = pr
            if _cancel_job.is_set():        # Cancel pressed while the source was still being resolved
                pr.terminate()
            expired = threading.Event()
            timers = []
            limit, begun = download_limit(duration), time.time()
            def arm(seconds):
                timer = threading.Timer(seconds, expire)
                timer.daemon = True
                timers[:] = [timer]
                timer.start()
            def expire():
                if pr.poll() is not None:
                    return
                # Only downloading time counts: resume moves job["started"] forward by the paused time.
                left = limit - (time.time() - job.get("started", begun))
                if job.get("paused") or left > 0.5:
                    arm(PAUSE_RECHECK if job.get("paused") else left)
                    return
                expired.set()
                try:
                    pr.kill()
                except OSError:
                    pass
            arm(limit)
            try:
                for line in pr.stdout:
                    if line.startswith("out_time_us=") and time.time() - last > 1:
                        try:
                            seconds = float(line.split("=", 1)[1]) / 1e6
                        except ValueError:
                            continue
                        fraction = min(1, seconds / duration) if duration else 0
                        job.update(stage="Downloading before subtitle preparation", pct=min(90, fraction * 90) if duration else None,
                                   eta_seconds=estimate_remaining(job.get("started", time.time()), fraction))
                        write_job(job)
                        last = time.time()
                code = pr.wait()
            finally:
                for timer in timers:
                    timer.cancel()
                if pr.poll() is None:
                    pr.kill()
                    pr.wait()
            if _cancel_job.is_set():
                # not retryable: a batch must pause on Cancel, not move on to the next release of the same episode
                job.update(status="cancelled", retryable=False, stage="Download cancelled.")
                return
            if expired.is_set():
                raise SourceError("The source exceeded the download time limit. Try another release.")
            if code:
                if shutil.disk_usage(destination.parent).free < 2e9:
                    raise UserError("Not enough disk space. Free space and retry.")
                raise SourceError("The download stopped or the source could not be saved. Select another source to retry.")
        job.update(stage="Verifying download", pct=99, eta_seconds=None)
        write_job(job)
        try:
            result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(temp)],
                                    capture_output=True, timeout=30, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise SourceError("The saved file could not be verified within the time limit. Try another release.") from None
        if result.returncode:
            raise SourceError("The saved file failed verification; it was not added to Jellyfin.")
        data = json.loads(result.stdout)
        try:
            validate_media(source, data, temp.stat().st_size)
        except UserError as e:
            raise SourceError(str(e)) from None
        kinds = [t.get("codec_type") for t in data.get("streams", [])]
        actual = float(data.get("format", {}).get("duration") or 0)
        # The selected subtitle is optional at acquisition time: the mandatory subtitle
        # processing pipeline can source and time a replacement after saving the media.
        if "video" not in kinds or (audio != "none" and "audio" not in kinds) or (
                duration > 0 and actual < duration - max(5, duration * .01)):
            raise SourceError("The saved video/audio is incomplete; it was not added to Jellyfin.")
        import library_processing
        video = source["video"]
        metadata = {k: video[k] for k in ("type", "id", "season", "episode") if k in video}
        metadata["meta"] = {k: video.get("meta", {}).get(k) for k in ("id", "name", "releaseInfo")}
        with _lock:                         # atomic with cancel_download: either cancelled here, or refused there
            if _cancel_job.is_set():        # Cancel pressed during "Verifying download"
                job.update(status="cancelled", retryable=False, stage="Download cancelled.")
                return
            # processing_key now (not after submit returns): after a restart the card follows the queued subtitle job
            job.update(phase="processing", processing_key=library_processing.key_for(destination),
                       stage="Preparing subtitles and checking timing", pct=90, eta_seconds=None)
            write_job(job)
        def processing_progress(stage, pct):
            job.update(stage=stage, pct=90 + min(100, max(0, pct)) * .09, eta_seconds=None)
            write_job(job)
        preserve_raw = True
        record = library_processing.submit(temp, destination, metadata, processing_progress)
        job.update(processing_key=library_processing.key_for(destination),
                   status="done" if record["status"] == "done" else "waiting", stage=record["stage"],
                   pct=100 if record["status"] == "done" else 90, eta_seconds=None, path=str(destination))
    except Exception as e:
        if _cancel_job.is_set() and job.get("phase") != "processing":
            job.update(status="cancelled", retryable=False, stage="Download cancelled.")
            return
        job.update(status="failed", retryable=isinstance(e, SourceError),
                   stage=str(e) if isinstance(e, UserError) else "Download failed. Check disk space or try another source.")
        agent.log(f"Stremio: download failed ({type(e).__name__}); no private source details logged")
    finally:
        try:
            if not preserve_raw:
                temp.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            job.pop("paused", None)
            job.pop("paused_at", None)
            write_job(job)
        finally:
            _job_process = None
            _job_state = None
            _job_lock.release()
