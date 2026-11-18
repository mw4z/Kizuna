"""Add the styled Arabic track as the default subtitle, in place (via .tmp + verify)."""
import json
import os
import subprocess
import sys

MKV = r"C:\Program Files\MKVToolNix"
HERE = os.path.dirname(os.path.abspath(__file__))


def info(p):
    return json.loads(subprocess.run([MKV + r"\mkvmerge.exe", "-J", p], capture_output=True,
                                     text=True, encoding="utf-8",
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout)


class Locked(PermissionError):
    """The new file is built and fully verified (`tmp`), only the swap over the original was refused."""
    def __init__(self, tmp):
        super().__init__(f"episode in use; verified output waiting at {tmp}")
        self.tmp = tmp


class VerifyFailed(RuntimeError):
    pass


def mux(video, ass, progress=None, check=None):
    """check(tmp) -> bool: extra verification of the built file; the original is only replaced when it passes."""
    src = info(video)
    tr = src["tracks"]
    old = [t["id"] for t in tr if t["type"] == "subtitles" and t["properties"].get("track_name") == "العربية"]
    tr = [t for t in tr if t["id"] not in old]             # replace our earlier Arabic track, if any
    subs = [t for t in tr if t["type"] == "subtitles"]
    have = {a["file_name"] for a in src.get("attachments", [])}
    tmp = video[:-4] + ".tmp.mkv"
    cmd = [MKV + r"\mkvmerge.exe", "--gui-mode", "-o", tmp]
    for t in subs:
        cmd += ["--default-track-flag", f"{t['id']}:no"]
    if old:
        cmd += ["--subtitle-tracks", ",".join(str(t["id"]) for t in subs)] if subs else ["--no-subtitles"]
    cmd += [video, "--language", "0:ara", "--track-name", "0:العربية",
            "--default-track-flag", "0:yes", "--sub-charset", "0:UTF-8", ass]
    import subtitle_style
    for f in subtitle_style.font_files(ass):              # the font the style uses, so every player draws it
        if os.path.basename(f) not in have:
            cmd += ["--attachment-mime-type", "application/x-truetype-font", "--attach-file", f]
    first_sub = min((t["id"] for t in subs), default=10**9)   # no subtitles at all: Arabic goes last (audit #7)
    order = [f"0:{t['id']}" for t in tr if t["id"] < first_sub] + ["1:0"] + \
            [f"0:{t['id']}" for t in tr if t["id"] >= first_sub]
    cmd += ["--track-order", ",".join(order)]
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = []
    for ln in pr.stdout:
        out.append(ln)
        if progress and ln.startswith("#GUI#progress "):
            progress(float(ln.split()[1].rstrip("%")))
    if pr.wait() not in (0, 1):
        if os.path.exists(tmp):
            os.remove(tmp)                                   # never leave a half-written .tmp next to the episode
        raise RuntimeError("".join(out)[-800:])
    new = info(tmp)
    nt = new["tracks"]
    ara = [t for t in nt if t["type"] == "subtitles" and t["properties"].get("track_name") == "العربية"]
    defs = [t for t in nt if t["type"] == "subtitles" and t["properties"].get("default_track")]
    ok = (len(nt) == len(tr) + 1 and len(ara) == 1 and defs == ara
          and abs(new["container"]["properties"]["duration"] - src["container"]["properties"]["duration"]) < 5e8)
    if not ok or (check and not check(tmp)):
        os.remove(tmp)
        raise VerifyFailed("verification failed, original untouched")
    try:
        os.replace(tmp, video)
    except PermissionError as e:
        raise Locked(tmp) from e
    return f"ok: {len(nt)} tracks, Arabic = track {ara[0]['id']} (only default subtitle)"


if __name__ == "__main__":
    for pair in sys.argv[1:]:
        v, a = pair.split("|")
        print(os.path.basename(v)[:50], "->", mux(v, a), flush=True)
