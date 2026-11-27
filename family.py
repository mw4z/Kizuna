"""Family audit: find kissing and nudity, let the user decide per scene, blur the approved shots cut to cut.

Flow for a title queued with "family" on (library_processing):
  1. audit(): frames at 2 fps (GPU decode when possible) scored by CLIP on the GPU against kissing / nudity /
     revealing prompts and many neutral decoys (what fooled earlier versions: text on black, arms, hands, a hospital
     bed, faces close while talking ...). A frame >= SEED starts a scene; frames >= SUPPORT nearby extend it and must
     number MIN_SUPPORT (a single strong nudity frame is enough: the strip-club shot in The Town was one frame).
     Every counted frame must also match its prompts absolutely (ABS_MIN), so a frame nothing fits can't win by default.
     Each scene is widened to the shot cuts around it, so a blur always covers the whole shot.
  2. The title is HELD (never published). The Now page lists it; the review page shows each scene as a frame strip with
     Blur / Keep, and "Add a scene" covers misses. Nothing changes until every scene is decided.
  3. render(): only the stretch between the clean keyframes around each approved shot is re-encoded (same codec,
     HDR10 metadata kept, no B-frames, headers in-band); after it the film's own headers are re-sent; everything else is
     stream-copied bit-identical. Verify: same frame count, clean decode across every join, identical frames outside.
  4. proof(): a dense sheet (4 fps, each blurred range +/- 10 s) from the FINAL file; published only after approval.

Calibration (The Town, 2026-11-27, every candidate checked by eye): all 8 real scenes found (3 kisses, strip club,
lingerie, underwear, a bed scene, a shower) with 5 false alarms in 2 h, from 96 with the first prompt set.
Detection never decides alone. Model: open_clip ViT-B-32 laion2b (downloaded once to the Hugging Face cache)."""
import json
import re
import subprocess
import time
from pathlib import Path

import agent

FPS = 2                      # scan rate (the calibration rate)
GAP = 4.0                    # supporting frames closer than this belong to one scene
PAD = 0.25                   # extra seconds blurred on each side of a shot
SHEET_FPS = 4
SEED, SUPPORT, MIN_SUPPORT, ABS_MIN = 0.6, 0.35, 2, 0.22
PROMPTS = {
    "kiss": ["two people kissing on the lips", "a couple kissing passionately", "a romantic kiss"],
    "nudity": ["a topless woman", "a naked woman", "a naked man", "a nude body", "a woman with bare breasts",
               "a stripper dancing on a pole"],
    "revealing": ["a woman in lingerie", "a woman in a bra", "a woman in underwear", "a woman in a bikini",
                  "a woman in a very revealing outfit"],
    "neutral": ["two people talking face to face", "a person drinking from a bottle", "two people looking at each other",
                "people hugging", "a close-up of a face", "a person smiling", "people sitting at a table",
                "a man holding a gun", "a police officer", "a street", "a building", "a car", "a dark room",
                "a woman wearing a dress", "a woman wearing a jacket", "a crowd of people", "a garden",
                "white text on a black background", "movie credits", "a black screen", "a person's arm", "a hand",
                "a woman lying in a hospital bed", "a woman in a black top", "a man in a uniform", "two people fighting",
                "a man and a woman talking closely", "a man whispering to a woman", "a parent hugging a child",
                "a woman covering her face", "a person on the floor", "people at a party",
                "a man and a woman flirting at a bar"],
}
KINDS = ("kiss", "nudity", "revealing")


def available():
    """The optional detection packages are installed (checked without loading them)."""
    import importlib.util
    return all(importlib.util.find_spec(m) for m in ("torch", "open_clip"))


# ---------------------------------------------------------------- detection
def _models():
    import open_clip
    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k", device=dev)
    model.eval()
    tok = open_clip.get_tokenizer("ViT-B-32")
    labels = [(g, p) for g, ps in PROMPTS.items() for p in ps]
    with torch.no_grad():
        tf = model.encode_text(tok([p for _, p in labels]).to(dev))
        tf /= tf.norm(dim=-1, keepdim=True)
    return dev, model, pre, labels, tf


def _frames(video, w=640, h=360):
    """(index, RGB array) at FPS, GPU-decoded when the build supports it."""
    import numpy as np
    base = ["ffmpeg", "-v", "error"]
    tail = ["-i", str(video), "-map", "0:v:0", "-vf",
            f"fps={FPS},scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    for hw in (["-hwaccel", "cuda"], []):
        p = subprocess.Popen(base + hw + tail, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=agent.NO_CF)
        i = 0
        while True:
            buf = p.stdout.read(w * h * 3)
            if len(buf) < w * h * 3:
                break
            yield i, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            i += 1
        p.wait()
        if i:                                   # decoded with this method: done; otherwise retry on the CPU
            return


def score(video, progress=None):
    """-> per frame {"t", <kind>: group probability, <kind>_abs: best absolute similarity}."""
    import torch
    from PIL import Image
    dev, model, pre, labels, tf = _models()
    groups = {g: [i for i, (gg, _) in enumerate(labels) if gg == g] for g in KINDS}
    rows, batch = [], []

    def flush():
        imgs = torch.stack([pre(Image.fromarray(a)) for _, a in batch]).to(dev)
        with torch.no_grad():
            f = model.encode_image(imgs)
            f /= f.norm(dim=-1, keepdim=True)
            sims = (f @ tf.T)
            pr = (100 * sims).softmax(dim=-1).cpu().numpy()
            sims = sims.cpu().numpy()
        for (i, _), p, s in zip(batch, pr, sims):
            row = {"t": i / FPS}
            for g, ix in groups.items():
                row[g] = round(float(p[ix].sum()), 3)
                row[g + "_abs"] = round(float(s[ix].max()), 3)
            rows.append(row)
        batch.clear()
    for i, a in _frames(video):
        batch.append((i, a))
        if len(batch) == 64:
            flush()
            if progress:
                progress(i / FPS)
    if batch:
        flush()
    return rows


def scenes(rows, want=KINDS):
    """Scored frames -> candidate scenes [{"from", "to", "kinds", "peak"}], earliest first (see the module notes)."""
    found = []
    for g in want:
        groups, cur = [], None
        for r in rows:
            if r[g] < SUPPORT or r[g + "_abs"] < ABS_MIN:
                continue
            if cur and r["t"] - cur[-1]["t"] <= GAP:
                cur.append(r)
            else:
                cur = [r]
                groups.append(cur)
        for grp in groups:
            peak = max(r[g] for r in grp)
            if peak >= SEED and (len(grp) >= MIN_SUPPORT or (g == "nudity" and peak >= 0.97)):
                found.append({"from": grp[0]["t"], "to": grp[-1]["t"], "kinds": [g], "peak": peak})
    out = []
    for s in sorted(found, key=lambda x: x["from"]):           # kinds overlapping in time become one scene
        if out and s["from"] - out[-1]["to"] <= GAP:
            out[-1]["to"] = max(out[-1]["to"], s["to"])
            out[-1]["kinds"] = sorted(set(out[-1]["kinds"]) | set(s["kinds"]))
            out[-1]["peak"] = max(out[-1]["peak"], s["peak"])
        else:
            out.append(dict(s))
    return out


def cuts(video, a, b):
    """Shot-cut times between a and b (ffmpeg scdet on a small decode)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-hwaccel", "cuda", "-ss", f"{max(0, a)}", "-t", f"{b - max(0, a)}",
                        "-i", str(video), "-map", "0:v:0", "-vf", "scale=640:360,scdet=threshold=6:sc_pass=1,metadata=print:file=-",
                        "-f", "null", "-"], capture_output=True, text=True, creationflags=agent.NO_CF)
    return [max(0, a) + float(x) for x in re.findall(r"lavfi\.scd\.time=([\d.]+)", p.stdout)]


def shot_range(video, a, b):
    """Widen [a, b] to the shot boundaries around it (+PAD), searching 30 s each way."""
    c = cuts(video, a - 30, b + 30)
    start = max([x for x in c if x <= a + 0.05], default=max(0.0, a - 2))
    end = min([x for x in c if x >= b + 1 / FPS - 0.05], default=b + 2)
    return round(max(0.0, start - PAD), 3), round(end + PAD, 3)


# ---------------------------------------------------------------- frame strips (review UI + final proof)
def strip(video, a, b, out, n=6, w=320, h=180):
    """n frames evenly across [a, b] side by side -> JPEG at out."""
    from PIL import Image
    times = [a + (b - a) * (i + .5) / n for i in range(n)]
    sheet = Image.new("RGB", (w * n, h))
    for i, t in enumerate(times):
        p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={w}:{h}",
                            "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, creationflags=agent.NO_CF)
        if len(p.stdout) >= w * h * 3:
            sheet.paste(Image.frombytes("RGB", (w, h), p.stdout[:w * h * 3]), (i * w, 0))
    sheet.save(out, quality=82)
    return out


def proof(video, ranges, out, w=240, h=135, cols=8):
    """Dense sheet from the FINAL file: every blurred range +/- 10 s at SHEET_FPS, one band per range, blurred frames
    marked red. -> path."""
    from PIL import Image, ImageDraw
    bands = []
    for a, b in ranges:
        times = [round(a - 10 + i / SHEET_FPS, 2) for i in range(int((b - a + 20) * SHEET_FPS) + 1)]
        tiles = []
        for t in times:
            p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t)}", "-i", str(video), "-frames:v", "1",
                                "-vf", f"scale={w}:{h}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True,
                               creationflags=agent.NO_CF)
            im = Image.frombytes("RGB", (w, h), p.stdout[:w * h * 3]) if len(p.stdout) >= w * h * 3 else Image.new("RGB", (w, h))
            d = ImageDraw.Draw(im)
            inside = a <= t <= b
            d.rectangle([0, 0, w - 1, h - 1], outline=(220, 40, 60) if inside else (60, 60, 60), width=3 if inside else 1)
            d.text((5, 4), f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}", fill=(255, 230, 0))
            tiles.append(im)
        rows = (len(tiles) + cols - 1) // cols
        band = Image.new("RGB", (cols * w, rows * h), (12, 12, 14))
        for i, im in enumerate(tiles):
            band.paste(im, ((i % cols) * w, (i // cols) * h))
        bands.append(band)
    sheet = Image.new("RGB", (cols * w, sum(b.height + 12 for b in bands)), (12, 12, 14))
    y = 0
    for b in bands:
        sheet.paste(b, (0, y))
        y += b.height + 12
    sheet.save(out, quality=80)
    return out


# ---------------------------------------------------------------- render
def _packets(video, a, b):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{max(0, a)}%{b}",
                        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True, creationflags=agent.NO_CF)
    out = []
    for line in r.stdout.splitlines():
        p = line.split(",")
        if len(p) >= 2 and p[0] not in ("", "N/A"):
            out.append((float(p[0]), "K" in p[1]))
    return out


def clean_keys(video, a, b):
    """Keyframes in [a, b] that no later packet reaches back across (safe to cut at)."""
    pk = _packets(video, a, b)
    keys = []
    for i, (t, k) in enumerate(pk):
        if k:
            nxt = [pt for pt, _ in pk[i + 1:i + 60]]
            prev = [pt for pt, _ in pk[max(0, i - 60):i]]
            if nxt and min(nxt) > t and (not prev or max(prev) < t):
                keys.append(t)
    return keys


def _video_info(video):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,pix_fmt,width,height,r_frame_rate,color_transfer,color_primaries", "-of", "json",
                        str(video)], capture_output=True, text=True, creationflags=agent.NO_CF)
    return json.loads(r.stdout)["streams"][0]


def _hdr_params(video):
    """x265 parameters that keep HDR10 (colour, mastering display, light level) as in the source."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1", "-show_frames",
                        "-show_entries", "frame=side_data_list", "-of", "json", str(video)], capture_output=True, text=True,
                       creationflags=agent.NO_CF)
    sd = {d["side_data_type"]: d for f in json.loads(r.stdout or "{}").get("frames", []) for d in f.get("side_data_list", [])}
    md, cl = sd.get("Mastering display metadata"), sd.get("Content light level metadata")
    q = lambda v, s: round(int(str(v).split("/")[0]) * s / int(str(v).split("/")[1]))  # noqa: E731
    out = ["hdr10=1", "hdr10-opt=1", "repeat-headers=1", "colorprim=bt2020", "transfer=smpte2084", "colormatrix=bt2020nc", "range=limited"]
    if md:
        out.append("master-display=" + "".join(f"{k}({q(md[k + '_x'], 50000)},{q(md[k + '_y'], 50000)})" for k in ("green", "blue", "red"))
                   + f"WP({q(md['white_point_x'], 50000)},{q(md['white_point_y'], 50000)})"
                   + f"L({q(md['max_luminance'], 10000)},{q(md['min_luminance'], 10000)})")
    if cl:
        out.append(f"max-cll={cl['max_content']},{cl['max_average']}")
    return out


def plan(video, ranges):
    """Approved blur ranges -> re-encode spans between clean keyframes, merged when they overlap.
    -> [{"k1", "k2", "blur": [[a, b], ...]}]"""
    spans = []
    for a, b in sorted(ranges):
        keys = clean_keys(video, a - 60, b + 60)
        k1 = max([k for k in keys if k <= a - 0.1], default=None)
        k2 = min([k for k in keys if k >= b + 0.1], default=None)
        if k1 is None or k2 is None:
            raise RuntimeError(f"no clean keyframe around {a:.1f}-{b:.1f}")
        if spans and k1 <= spans[-1]["k2"] + 2.0:             # close spans become one: no sliver between them
            spans[-1]["k2"] = max(spans[-1]["k2"], k2)
            spans[-1]["blur"].append([a, b])
        else:
            spans.append({"k1": k1, "k2": k2, "blur": [[a, b]]})
    return spans


def render(video, ranges, work, progress=None):
    """Blur `ranges` in `video` -> path of the edited file in `work`; the source is only read."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    info = _video_info(video)
    num, den = (int(x) for x in info["r_frame_rate"].split("/"))
    fps = num / den
    w, h = int(info["width"]), int(info["height"])
    hdr = info.get("color_transfer") == "smpte2084"
    ten = "10" in str(info.get("pix_fmt"))
    spans = plan(video, ranges)
    ts = lambda s: f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"  # noqa: E731
    codec_es = "hevc" if info["codec_name"] == "hevc" else "h264"
    # pieces in film order: ("keep", a, b) stream copy | ("mid", n) re-encoded span | ("restore", a, b): the stretch from a
    # span's end to the next clean keyframe, re-muxed so the film's own parameter sets are sent again (the span's
    # in-band ones would otherwise stay active and break the decode of everything after it)
    pieces, cur = [], 0.0
    for n, sp in enumerate(spans):
        if sp["k1"] - cur > 0.02:
            pieces.append(("keep", cur, sp["k1"]))
        pieces.append(("mid", n))
        nxt = spans[n + 1]["k1"] if n + 1 < len(spans) else None
        # at least a second: a one-frame raw stream can't be re-wrapped (The Town, 2026-11-27)
        k3 = min([k for k in clean_keys(video, sp["k2"] + 1.0, sp["k2"] + 60) if k >= sp["k2"] + 1.0], default=None)
        if nxt is not None and (k3 is None or k3 > nxt):
            k3 = nxt
        pieces.append(("restore", sp["k2"], k3))
        cur = k3
    if cur is not None:
        pieces.append(("keep", cur, None))
    cuts_wanted = [x for x in pieces if x[0] != "mid"]
    rng = ",".join(f"{ts(x[1])}-{ts(x[2])}" if x[2] is not None else f"{ts(x[1])}-" for x in cuts_wanted)
    r = subprocess.run([agent.MKV + r"\mkvmerge.exe", "-q", "-o", str(work / "cut.mkv"), "-A", "-S", "-B", "-M", "--no-chapters",
                        "--split", f"parts:{rng}", str(video)], capture_output=True, text=True, creationflags=agent.NO_CF)
    made = sorted(work.glob("cut-*.mkv")) or ([work / "cut.mkv"] if (work / "cut.mkv").exists() else [])
    if r.returncode not in (0, 1) or len(made) != len(cuts_wanted):
        raise RuntimeError(f"could not cut the film ({len(made)} of {len(cuts_wanted)} pieces)")
    files = dict(zip(cuts_wanted, made))
    for x in cuts_wanted:
        if x[0] == "restore":
            es = work / f"restore.{codec_es}"
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(files[x]), "-map", "0:v:0", "-c:v", "copy",
                                "-bsf:v", f"{codec_es}_mp4toannexb", "-f", codec_es, str(es)], capture_output=True, text=True,
                               creationflags=agent.NO_CF)
            fixed = files[x].with_name(files[x].stem + "-r.mkv")
            r2 = subprocess.run([agent.MKV + r"\mkvmerge.exe", "-q", "-o", str(fixed), "--default-duration", f"0:{num}/{den}p", str(es)],
                                capture_output=True, text=True, creationflags=agent.NO_CF)
            es.unlink(missing_ok=True)
            if r.returncode or r2.returncode not in (0, 1):
                raise RuntimeError("could not restore the film's headers after an edit")
            files[x].unlink(missing_ok=True)
            files[x] = fixed
    mids = []
    for n, s in enumerate(spans):
        frames = round((s["k2"] - s["k1"]) * fps)
        enable = "+".join(f"between(t,{a},{b})" for a, b in s["blur"])
        # a fixed 16x9 whatever the resolution, then a blur scaled to the height: 1/48 still left lingerie and a body
        # outline readable on a 4K film (The Town 14:05, 2026-11-27); 16x9 leaves only soft colour
        blur = (f"[0:v]split[a][b];[b]scale=16:9,scale={w}:{h}:flags=bilinear,gblur=sigma={max(20, h // 24)}[bl];"
                f"[a][bl]overlay=enable='{enable}'[v]")
        raw = work / f"mid{n}.{'hevc' if info['codec_name'] == 'hevc' or hdr else 'h264'}"
        if info["codec_name"] == "hevc" or hdr:
            # no B-frames: the span's timestamps can't run backwards at a join; headers in-band: the film's own header
            # (kept by mkvmerge) never decodes the new frames
            params = (_hdr_params(video) if hdr else ["repeat-headers=1"]) + ["log-level=error", "aud=1", "keyint=48", "min-keyint=1", "bframes=0"]
            codec = ["-c:v", "libx265", "-preset", "slow", "-crf", "14", "-pix_fmt", "yuv420p10le" if ten else "yuv420p",
                     "-x265-params", ":".join(params), "-f", "hevc"]
        else:
            codec = ["-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", "yuv420p", "-bf", "0",
                     "-x264-params", "repeat-headers=1:aud=1", "-f", "h264"]
        cmd = ["ffmpeg", "-v", "error", "-y", "-copyts", "-ss", f"{s['k1']}", "-i", str(video), "-frames:v", str(frames),
               "-filter_complex", blur, "-map", "[v]"] + codec + [str(raw)]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=agent.NO_CF)
        if r.returncode:
            raise RuntimeError("re-encoding a blurred shot failed: " + r.stderr[-200:])
        mid = work / f"mid{n}.mkv"
        subprocess.run([agent.MKV + r"\mkvmerge.exe", "-q", "-o", str(mid), "--default-duration", f"0:{num}/{den}p", str(raw)],
                       capture_output=True, creationflags=agent.NO_CF)
        raw.unlink(missing_ok=True)
        mids.append(mid)
        if progress:
            progress((n + 1) / len(spans) * 50)
    order = [mids[x[1]] if x[0] == "mid" else files[x] for x in pieces]
    video_only = work / "video.mkv"
    args = [agent.MKV + r"\mkvmerge.exe", "-q", "-o", str(video_only), str(order[0])]
    for p in order[1:]:
        args += ["+", str(p)]
    r = subprocess.run(args, capture_output=True, text=True, creationflags=agent.NO_CF)
    if r.returncode not in (0, 1):
        raise RuntimeError("joining the film failed: " + r.stdout[-200:])
    for p in order:
        p.unlink(missing_ok=True)
    out = work / "family.mkv"
    r = subprocess.run([agent.MKV + r"\mkvmerge.exe", "-q", "-o", str(out), str(video_only), "-D", str(video)],
                       capture_output=True, text=True, creationflags=agent.NO_CF)
    video_only.unlink(missing_ok=True)
    if r.returncode not in (0, 1):
        raise RuntimeError("adding audio and subtitles failed: " + r.stdout[-200:])
    verify(video, out, spans)
    return out


def _count(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                        "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True, creationflags=agent.NO_CF)
    return int(r.stdout.strip() or 0)


def _md5(path, a, t):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, a)}", "-t", f"{t}", "-i", str(path), "-map", "0:v:0",
                        "-f", "framemd5", "-"], capture_output=True, text=True, creationflags=agent.NO_CF)
    return [line.split(",")[-1].strip() for line in r.stdout.splitlines() if line and not line.startswith("#")]


def verify(src, out, spans):
    """Same number of frames; every join decodes cleanly; frames just outside each span are bit-identical."""
    if _count(src) != _count(out):
        raise RuntimeError("the edited film has a different number of frames")
    for n, s in enumerate(spans):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, s['k1'] - 6)}", "-t", f"{s['k2'] - s['k1'] + 12}",
                            "-i", str(out), "-map", "0:v:0", "-f", "null", "-"], capture_output=True, text=True, creationflags=agent.NO_CF)
        if r.stderr.strip():
            raise RuntimeError("decode error at a join: " + r.stderr[-200:])
        # the untouched frames on each side, never reaching into a neighbouring edit (The Town: two edits 2.3 s apart)
        prev_end = spans[n - 1]["k2"] if n else 0.0
        next_start = spans[n + 1]["k1"] if n + 1 < len(spans) else None
        for a, b in ((max(prev_end + 0.2, s["k1"] - 4), s["k1"] - 0.2),
                     (s["k2"] + 0.2, min(s["k2"] + 3.5, next_start - 0.2) if next_start is not None else s["k2"] + 3.5)):
            if b - a < 0.3:
                continue                                   # no untouched stretch here (edits back to back)
            # by frame sequence, not by time: re-wrapped pieces round timestamps to the millisecond, which can move a
            # seek by one frame; the original frames must appear unchanged and in order in a slightly wider window
            x, y = _md5(src, a, b - a), _md5(out, a - 0.5, b - a + 1)
            if not x or not any(y[i:i + len(x)] == x for i in range(len(y) - len(x) + 1)):
                raise RuntimeError(f"frames changed outside the edit near {a:.1f}s")


# ---------------------------------------------------------------- the queue step
def audit(folder, video, progress=None):
    """Scan a prepared title and write review.json with candidate scenes (and their frame strips). -> scenes."""
    folder = Path(folder)
    rows = score(video, progress)
    (folder / "family-scores.json").write_text(json.dumps(rows), encoding="utf-8")
    widened = []
    for s in scenes(rows):
        a, b = shot_range(video, s["from"], s["to"])
        if widened and a <= widened[-1]["to"]:                 # two scenes in one shot: one review entry
            widened[-1].update(to=max(widened[-1]["to"], b), kinds=sorted(set(widened[-1]["kinds"]) | set(s["kinds"])),
                               peak=max(widened[-1]["peak"], s["peak"]), seen_to=s["to"])
        else:
            widened.append(dict(s, **{"from": a, "to": b, "seen_from": s["from"], "seen_to": s["to"]}))
    found = []
    for n, s in enumerate(widened):
        shot = folder / f"scene{n}.jpg"
        # the strip shows the flagged moments (so the reason is visible); the blur still covers the whole shot
        strip(video, max(s["from"], s["seen_from"] - 0.5), min(s["to"], s["seen_to"] + 0.5), shot)
        found.append({"id": n, "from": s["from"], "to": s["to"], "kinds": s["kinds"], "peak": round(float(s["peak"]), 3),
                      "decision": None, "image": shot.name})
    review = {"created": time.time(), "scenes": found, "added": [], "stage": "review"}
    (folder / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
    return found
