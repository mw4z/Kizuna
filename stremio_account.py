"""Windows-user-encrypted Stremio connection; imports only Stremio's own profile."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import types

HERE = Path(__file__).resolve().parent
CONNECTION = HERE / "stremio-account.bin"


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, decrypt=False):
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None,
                                                   None, 1, ctypes.byref(target))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "MyAnimeWatcher Stremio",
                                                 None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise RuntimeError("Windows could not unlock the Stremio connection for this user")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def save(connection):
    tmp = CONNECTION.with_suffix(".tmp")
    tmp.write_bytes(protect(json.dumps(connection, ensure_ascii=False).encode("utf-8")))
    os.replace(tmp, CONNECTION)


def load():
    if not CONNECTION.exists():
        raise ValueError("Connect Stremio first: run stremio_account.py --import-local")
    return json.loads(protect(CONNECTION.read_bytes(), decrypt=True))


def import_local():
    # The optional reader is only needed for import, never for normal operation.
    reader = HERE / "work" / "stremio-storage-reader" / "ccl_chromium_reader"
    pkg = types.ModuleType("ccl_chromium_reader")
    pkg.__path__ = [str(reader)]
    sys.modules["ccl_chromium_reader"] = pkg
    sys.path.insert(0, str(HERE / "work" / "stremio-snappy"))
    from ccl_chromium_reader.ccl_chromium_localstorage import LocalStoreDb

    storage = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "StremioService" / \
        "stremio-shell-ng.exe.WebView2" / "EBWebView" / "Default" / "Local Storage" / "leveldb"
    with LocalStoreDb(storage) as db:
        records = [r for r in db.iter_all_records() if r.script_key == "profile"
                   and "strem" in r.storage_key and r.is_live]
        if not records:
            raise ValueError("No signed-in Stremio profile found. Sign in to Stremio first.")
        profile = json.loads(max(records, key=lambda r: r.leveldb_seq_number).value)
    addons = profile.get("addons", [])
    if not addons:
        raise ValueError("The Stremio profile has no add-ons")
    connection = {"addons": addons, "authKey": (profile.get("auth") or {}).get("key")}
    save(connection)
    return [a.get("manifest", {}).get("name", "Unnamed add-on") for a in addons]


if __name__ == "__main__":
    if sys.argv[1:] != ["--import-local"]:
        raise SystemExit("Usage: stremio_account.py --import-local")
    try:
        print("Connected add-ons:", ", ".join(import_local()))
    except Exception as e:
        # Reader exceptions can contain account data; never print their values.
        raise SystemExit(f"Stremio import failed ({type(e).__name__}); no account details printed")
