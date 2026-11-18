"""First-run setup (the /setup page): what this PC still needs, and the few actions the wizard can take for you.

Checks: the tools Kizuna runs (ffmpeg, ffprobe, MKVToolNix), the folders, Jellyfin (running, and libraries that
point at the Weekly and Anime folders) and Stremio (signed in, a stream add-on, a subtitle add-on).
Actions: install a missing app with winget in a visible window (the person sees and approves every installer),
create the folders, sign in to Stremio (only the session key is kept, encrypted for this Windows user; the
password is never stored), and mark setup as done."""
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request

import agent
import mux

APPS = {   # winget package ids (https://winget.run); installed in a visible console window
    "ffmpeg": ("Gyan.FFmpeg", "FFmpeg (video tools)"),
    "mkvtoolnix": ("MoritzBunkus.MKVToolNix", "MKVToolNix (adds the subtitle track)"),
    "jellyfin": ("Jellyfin.Server", "Jellyfin Server (your media library)"),
    "stremio": ("Stremio.Stremio", "Stremio (optional: manage your add-ons)"),
}
JELLYFIN = "http://127.0.0.1:8096"


class SetupError(ValueError):
    pass


def _jellyfin_up():
    try:
        with urllib.request.urlopen(JELLYFIN + "/System/Info/Public", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _covered(folder, libraries):
    """The folder is (inside) a Jellyfin library path."""
    f = os.path.normcase(os.path.abspath(folder))
    for lib in libraries:
        p = os.path.normcase(os.path.abspath(lib["path"]))
        if f == p or f.startswith(p.rstrip("\\/") + os.sep):
            return lib["name"]
    return None


def check():
    import settings
    import stremio
    s = settings.get()
    tools = {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
             "mkvtoolnix": os.path.exists(os.path.join(mux.MKV, "mkvmerge.exe"))}
    folders = {k: {"path": s[k], "exists": os.path.isdir(s[k])} for k in settings.FOLDERS}
    try:
        libraries = stremio.libraries()
    except Exception:
        libraries = []
    jellyfin = {"running": _jellyfin_up(), "url": "http://localhost:8096", "libraries": [l["name"] for l in libraries],
                "weekly": _covered(s["folder_weekly"], libraries), "anime": _covered(s["folder_anime"], libraries)}
    try:
        installed = stremio.addons()
        names = [a.get("manifest", {}).get("name", "Add-on") for a in installed]
        res = lambda a: {r if isinstance(r, str) else r.get("name") for r in a.get("manifest", {}).get("resources", [])}  # noqa: E731
        stremio_state = {"connected": True, "addons": names, "streams": any("stream" in res(a) for a in installed),
                         "subtitles": any("subtitles" in res(a) for a in installed)}
    except Exception:
        stremio_state = {"connected": False, "addons": [], "streams": False, "subtitles": False}
    ready = (all(tools.values()) and all(f["exists"] for f in folders.values()) and jellyfin["running"]
             and jellyfin["weekly"] and stremio_state["connected"] and stremio_state["streams"])
    return {"tools": tools, "folders": folders, "jellyfin": jellyfin, "stremio": stremio_state, "ready": bool(ready),
            "done": s["setup_done"], "apps": {k: v[1] for k, v in APPS.items()}, "winget": bool(shutil.which("winget")),
            "subtitle_language": s["subtitle_language"], "language": s["language"]}


def install(app):
    """Open a console window running winget for one app; the person follows it (and any Windows prompt) there."""
    if app not in APPS:
        raise SetupError("Unknown app.")
    if not shutil.which("winget"):
        raise SetupError("winget is missing on this PC. Install 'App Installer' from the Microsoft Store, then try again.")
    package, label = APPS[app]
    subprocess.Popen(["cmd", "/c", "start", f"Kizuna setup: {label}", "cmd", "/k",
                      "winget", "install", "--id", package, "-e", "--source", "winget"],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"started": app, "label": label}


def make_folders():
    """Create the folders; when they differ from the ones this run uses, restart Kizuna (the supervisor brings it
    back in ~10 s) so the watcher, the queue and cleanup use the new ones."""
    import sys
    import threading
    import cleanup
    import settings
    s = settings.get()
    made = []
    for key in settings.FOLDERS:
        p = Path(s[key])
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            made.append(str(p))
    using = {"folder_inbox": agent.FOLDER, "folder_weekly": agent.LIBRARY, "folder_anime": cleanup.ANIME}
    restart = "--child" in sys.argv and any(os.path.normcase(os.path.abspath(s[k])) != os.path.normcase(os.path.abspath(v))
                                            for k, v in using.items())
    if restart:
        agent.log("setup: folders changed, restarting to use them")
        threading.Timer(1.5, os._exit, [0]).start()
    return {"made": made, "restarting": restart, **check()}


def stremio_login(email, password):
    """Sign in to Stremio's API, keep only the session key (encrypted for this Windows user) and the add-ons."""
    import stremio
    import stremio_account
    email, password = str(email or "").strip(), str(password or "")
    if "@" not in email or not password:
        raise SetupError("Enter the email and password of your Stremio account.")
    try:
        result = stremio.get_json("https://api.strem.io/api/login",
                                  {"type": "Login", "email": email, "password": password, "facebook": False}, timeout=30)
    except Exception:
        raise SetupError("Stremio did not answer. Check the internet connection and try again.") from None
    key = ((result or {}).get("result") or {}).get("authKey")
    if not key:
        raise SetupError("Stremio did not accept that email and password.")
    stremio_account.save({"addons": [], "authKey": key})
    try:
        stremio.sync()                        # fetch the account's add-ons with the new key
    except Exception:
        raise SetupError("Signed in, but the add-ons could not be loaded. Try 'Sync Stremio add-ons' on Discover.") from None
    agent.log("setup: Stremio connected")
    return check()


def finish():
    import settings
    settings.update({"setup_done": "true"})
    return check()
