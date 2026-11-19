"""Time a subtitle file against a video's audio.

The embedded English track is not a safe reference - it can itself be offset,
and trusting it once put a whole episode 15s out while measuring as a "perfect"
match. The audio cannot lie: speech in the mix has to line up with the cue that
translates it.

Envelope of the audio vs a 0/1 "a cue is on screen" signal, cross-correlated
by FFT. The envelope has its slow level drift removed first so loud music does
not dominate, and only positive excursions are kept so the correlation keys on
speech onsets rather than overall loudness.

The per-window check (agent.timing's consensus) uses windows that each hold the
same number of subtitle lines, so dialogue-dense stretches get the windows and
long music or fight scenes with a few lines don't dilute them; each window is
searched only near the whole-episode answer, judged against the typical level
of the full lag range so a window with no real match stays weak. Measured on
Bleach S14 2026-11-19: 0 false acceptances on 77 wrong/shifted cases, every
verified episode still passes, 6 more Blu-ray-timed episodes resolved.
"""
import functools
import json
import os
import subprocess
import sys

import numpy as np

SR = 8000
HOP = SR // 20          # 50 ms frames


def audio_env(path, stream="a:0"):
    """Decoded once per file version: every candidate subtitle is timed against the same audio (a 2 h film decodes
    to ~1 GB, ten candidates used to decode it ten times). Callers get a copy, the cache stays clean."""
    st = os.stat(path)
    return _audio_env(path, stream, st.st_size, st.st_mtime_ns).copy()


@functools.lru_cache(maxsize=2)
def _audio_env(path, stream, size, mtime):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:{stream}",
                        "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    a = np.frombuffer(p.stdout, dtype=np.float32).astype(np.float64)
    if p.returncode or len(a) < SR * 4 or not np.isfinite(a).all() or np.max(np.abs(a)) < 1e-6:
        raise ValueError("No usable decoded audio for timing")
    n = len(a) // HOP
    e = np.sqrt((a[:n * HOP].reshape(n, HOP) ** 2).mean(axis=1) + 1e-12)
    e = 20 * np.log10(e)
    k = 80                                   # ~4 s moving average
    base = np.convolve(e, np.ones(k) / k, mode="same")
    return np.clip(e - base, 0, None)


def cue_signal(path, length):
    h = open(path, encoding="utf-8-sig", errors="replace").read()

    def t2s(t):
        hh, mm, ss = t.split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)

    sig = np.zeros(length)
    spans = []
    for line in h.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        p = line.split(",", 4)
        if len(p) < 3:
            continue
        try:
            a, b = t2s(p[1]), t2s(p[2])
        except Exception:
            continue
        if b <= a or b - a > 20:
            continue
        spans.append((a, b))
        i, j = int(a * 20), min(length, int(b * 20))
        if j > i:
            sig[i:j] = 1.0
    return sig, spans


LOCAL = 2.5             # s: a window is searched only this far from the whole-episode answer


def _xcorr(env, sig, max_shift_s):
    n = 1 << int(np.ceil(np.log2(len(env) + len(sig))))
    E = np.fft.rfft(env - env.mean(), n)
    S = np.fft.rfft(sig - sig.mean(), n)
    xc = np.fft.irfft(E * np.conj(S), n)
    xc = np.concatenate([xc[-int(max_shift_s * 20):], xc[:int(max_shift_s * 20) + 1]])
    lags = np.arange(-int(max_shift_s * 20), int(max_shift_s * 20) + 1)
    return lags, xc, np.median(np.abs(xc))


def best_offset(env, sig, max_shift_s=90.0):
    if not len(env) or not len(sig) or np.std(env) < 1e-6 or np.std(sig) < 1e-6:
        return 0.0, 0.0
    lags, xc, med = _xcorr(env, sig, max_shift_s)
    k = int(np.argmax(xc))
    return lags[k] / 20.0, float(xc[k] / (med + 1e-9))


def best_offset_near(env, sig, center, radius=LOCAL, max_shift_s=90.0):
    """best_offset restricted to lags within `radius` of `center`. The strength is still judged against the typical
    level over the whole range, so a window with no real match near the answer stays weak instead of inventing one."""
    if not len(env) or not len(sig) or np.std(env) < 1e-6 or np.std(sig) < 1e-6:
        return 0.0, 0.0
    lags, xc, med = _xcorr(env, sig, max_shift_s)
    keep = np.abs(lags / 20.0 - center) <= radius
    if not keep.any():
        return 0.0, 0.0
    k = int(np.argmax(np.where(keep, xc, -np.inf)))
    return lags[k] / 20.0, float(xc[k] / (med + 1e-9))


def cue_windows(spans, n):
    """n windows that each hold the same number of subtitle lines, at least 5 (the last takes the remainder)
    -> [(from, to)]."""
    spans = sorted(spans)
    per = max(5, len(spans) // n)
    out = []
    for i in range(0, len(spans), per):
        last = i + 2 * per > len(spans)
        part = spans[i:] if last else spans[i:i + per]
        out.append((part[0][0], part[-1][1]))
        if last:
            break
    return out


def fit(video, subs, windows=10):
    env = audio_env(video)
    sig, spans = cue_signal(subs, len(env))
    g, strength = best_offset(env, sig)
    out = {"video_len": len(env) / 20.0, "cues": len(spans),
           "global": g, "strength": strength, "windows": []}
    for a, b in cue_windows(spans, windows) if spans else []:
        s2 = np.zeros_like(sig)
        ia, ib = int(a * 20), int(b * 20)
        s2[ia:ib] = sig[ia:ib]
        if s2.sum() < 20:
            continue
        o, st = best_offset_near(env, s2, g)
        out["windows"].append({"from": round(a, 1), "to": round(b, 1),
                               "offset": round(o, 2), "strength": round(st, 1)})
    return out


if __name__ == "__main__":
    r = fit(sys.argv[1], sys.argv[2])
    print(json.dumps(r, indent=1, ensure_ascii=False))
