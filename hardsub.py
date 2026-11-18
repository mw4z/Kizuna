"""Does a video already carry subtitles in the subtitle language burned into the picture? (2026-11-17)

Some downloads (e.g. Anime4up releases) are hardsubbed. Adding a soft track to those puts two sets
of subtitles on screen, so the library processor leaves them unchanged instead.

Method: frames spread over the episode, bottom 30 % only (where dialogue sits), keep white letters next to
a dark outline (the usual subtitle look), then Windows' built-in OCR for the subtitle language. A frame counts
when it reads at least two words in that language's letters; the video counts as hardsubbed when enough frames do.
Signs or Japanese text rarely pass both tests, and a raw episode reads ~0 frames. No OCR installed for the
language (or a Latin-script language, where signs and credits read as words too): never guess "hardsubbed".
"""
import asyncio
import re
import subprocess

import cv2
import numpy as np

import agent
import language

SAMPLES, MIN_HITS = 20, 5
ARABIC_WORD = re.compile(r"[ء-ي]{2,}")


def word_pattern():
    script = language.info()["script"]
    return re.compile(f"[{script}]{{2,}}") if script else None


def _ocr_engine():
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine
    return OcrEngine.try_create_from_language(Language(language.info()["ocr"]))


async def _ocr(engine, png):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    stream.seek(0)
    bitmap = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()
    return " ".join(line.text for line in (await engine.recognize_async(bitmap)).lines)


def subtitle_mask(bgr):
    """Black-on-white image of white, unsaturated pixels that touch a dark outline (subtitle letters)."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    white = ((hsv[..., 2] > 190) & (hsv[..., 1] < 60)).astype(np.uint8)
    dark = (hsv[..., 2] < 70).astype(np.uint8)
    m = white & cv2.dilate(dark, np.ones((5, 5), np.uint8))
    m = cv2.dilate(m, np.ones((3, 3), np.uint8)) & white
    img = np.where(m[..., None] > 0, 0, 255).astype(np.uint8).repeat(3, 2)
    return cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), int(m.sum())


def frame(video, t, width=1280):
    """Bottom 30 % of the frame at t seconds, as BGR (None when it can't be read)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                        "-vf", f"scale={width}:-2,crop=iw:ih*0.3:0:ih*0.7", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True, timeout=60, creationflags=agent.NO_CF)
    if r.returncode or not r.stdout:
        return None
    return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)


def dialogue_words(text):
    pattern = word_pattern() or ARABIC_WORD
    return pattern.findall(text or "")


def burned_in_subtitles(video, samples=SAMPLES, min_hits=MIN_HITS):
    """-> (hardsubbed?, frames that read as dialogue in the subtitle language, frames checked)."""
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
                       capture_output=True, text=True, timeout=60, creationflags=agent.NO_CF)
    try:
        duration = float(p.stdout.strip())
    except ValueError:
        return False, 0, 0
    if word_pattern() is None:             # Latin letters: credits and signs read as words too; don't guess
        return False, 0, 0
    engine = _ocr_engine()
    if engine is None:                     # no OCR for this language on this Windows: never guess "hardsubbed"
        return False, 0, 0
    hits = checked = 0
    for t in np.linspace(duration * 0.08, duration * 0.92, samples):
        bgr = frame(video, t)
        if bgr is None:
            continue
        img, ink = subtitle_mask(bgr)
        checked += 1
        if ink < 200:                      # no subtitle-like letters at all
            continue
        text = asyncio.run(_ocr(engine, cv2.imencode(".png", img)[1].tobytes()))
        if len(dialogue_words(text)) >= 2:
            hits += 1
            if hits >= min_hits:
                break
    return hits >= min_hits, hits, checked


if __name__ == "__main__":
    import sys
    for v in sys.argv[1:]:
        print(burned_in_subtitles(v), v)
