"""Delete episodes 1 day after they were fully watched in Jellyfin (user requests 2026-11-14, 2026-11-17).

"Fully watched" = Jellyfin's Played flag (set when playback reaches the end, or when the user
presses the ✓). Only episodes in the Weekly Anime and Anime libraries are touched (never Movies);
for Weekly Anime the OneDrive inbox copy with the same file name goes too (Anime files are named
like "10.mkv", so the inbox is never touched for them). Files go to the Recycle Bin, never erased.
Runs hourly inside the watcher; `python cleanup.py --dry` lists what would be removed.
"""
import ctypes
import os
from pathlib import Path
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agent  # noqa: E402

KEEP_SECONDS = 24 * 3600          # default; the Settings page can change it


def keep_seconds():
    """How long a watched episode stays. If settings.json can't be read at all, wait the longest allowed time (30 days)
    instead of guessing a shorter one: recycling too late is harmless, too early is not."""
    try:
        import settings
        if settings.damaged():
            return 30 * 86400
        return settings.get("keep_hours") * 3600
    except Exception:
        return 30 * 86400
ANIME = r"C:\Users\moody\Downloads\Anime"          # finished shows (Bleach, البؤساء); Movies is never cleaned


def inside(path, lib):
    return Path(path).resolve().is_relative_to(Path(lib).resolve())


def managed(path):
    return inside(path, agent.LIBRARY) or inside(path, ANIME)


def watched():
    """[(path, watched_at_epoch)] for played episodes inside the Weekly Anime and Anime libraries."""
    rows = []
    with agent.jellyfin_db() as c:
        for lib in (agent.LIBRARY, ANIME):
            rows += c.execute(
                "select b.Path, max(u.LastPlayedDate) from BaseItems b join UserData u on u.ItemId = b.Id "
                "where b.Type like '%.Episode' and u.Played = 1 and b.Path like ? group by b.Path",
                (lib + "%",)).fetchall()
    out = []
    for path, when in rows:
        if not managed(path):
            continue
        if not when:
            continue
        t = datetime.strptime(when[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        out.append((path, t))
    return out


def due(now=None):
    """[(path, watched_at, delete_at)] still on disk, soonest first (also used by the status page)."""
    now = now or time.time()
    keep = keep_seconds()
    return sorted(((p, t, t + keep) for p, t in watched() if os.path.exists(p)), key=lambda x: x[2])


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def recycle(path):
    """Send one file to the Recycle Bin with no dialogs. Returns True on success."""
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 4, 16, 64, 1024
    op = _SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0", None,
                          FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not os.path.exists(path)


def run_once(dry=False):
    now = time.time()
    for path, t, delete_at in due(now):
        if not managed(path):
            continue
        if delete_at > now:
            continue
        # Inbox copy FIRST: due() is driven by the library copy, so the library file must stay until the
        # inbox copy is really gone, or a locked inbox copy would be forgotten (audit #2). Weekly only.
        inbox = [os.path.join(agent.FOLDER, os.path.basename(path))] if inside(path, agent.LIBRARY) else []
        for p in inbox + [path]:
            if not os.path.exists(p):
                continue
            if dry:
                print("would recycle", p)
            elif recycle(p):
                agent.log(f"cleanup: recycled {p} (watched {datetime.fromtimestamp(t):%Y-%m-%d %H:%M})")
            else:
                agent.log(f"cleanup: could not recycle {p} (in use?), will retry next hour")
                break


def run():
    while True:
        try:
            run_once()
        except Exception as e:                  # never take the watcher down
            agent.log(f"cleanup: error {e!r}")
        time.sleep(3600)


if __name__ == "__main__":
    if "--dry" in sys.argv:
        for p, t, d in due():
            print(f"{os.path.basename(p)} | watched {datetime.fromtimestamp(t):%Y-%m-%d %H:%M} | "
                  f"deletes {datetime.fromtimestamp(d):%Y-%m-%d %H:%M}")
        run_once(dry=True)
    elif "--selftest" in sys.argv:              # recycle a scratch file to prove the call works silently
        tmp = os.path.join(agent.WORK, "cleanup_selftest.tmp")
        os.makedirs(agent.WORK, exist_ok=True)
        open(tmp, "w").write("x")
        assert recycle(tmp), "recycle failed"
        print("ok: scratch file sent to Recycle Bin silently")
