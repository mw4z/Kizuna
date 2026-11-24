"""Subtitle SRT -> ASS in the style chosen on the Settings page, for the configured subtitle language.

Right-to-left languages (Arabic, Persian, Hebrew): every visual line is wrapped in RLM so edge punctuation and
dialogue dashes resolve right-to-left under libass; leading override tags such as {\\an8} (sign translations) are
kept outside the wrap. Translator credit cues and subtitle-site adverts are dropped. A timing offset is applied if given.

Some right-to-left tracks are stored in visual order: the closing comma or "..." is moved to the START of the line so
that left-to-right players show it on the left (Crunchyroll's Arabic for The Apothecary Diaries, Kurai Mangetsu's
Bleach). Under the RLM wrap that punctuation would land on the right. When most lines of a track start with
punctuation and few end with it, it is moved back to the end of every line.
"""
import re
import sys

RLM = "‏"
CTRL = re.compile("[‎‏‪-‮⁦-⁩]")
# direction-neutral sentence punctuation, the marks visual-order tracks move to the start; the Arabic ؟ and ؛ are
# right-to-left letters themselves, already in place (Crunchyroll keeps them at the end)
EDGE = r"(?:\.\.\.|[.…،,!?:;])+"
LEADING = re.compile(rf"^({EDGE})\s*(.*\S)$")
TRAILING = re.compile(rf"{EDGE}$")


def visual_order(texts):
    """True when the lines carry their closing punctuation at the start (stored for left-to-right display)."""
    bodies = [re.sub(r"^(?:\{[^}]*\})+", "", CTRL.sub("", t)).strip() for t in texts]
    starts = sum(1 for b in bodies if LEADING.match(b))
    ends = sum(1 for b in bodies if TRAILING.search(b))
    return starts >= 5 and starts > 2 * ends
# Subtitle-site adverts and release-name lines (Collateral 2026-11-17: ">>OpenSubtitles v3+ v0.0.4<<" + the .srt name)
AD = re.compile(r"opensubtitles|subscene|subdl\.|addic7ed|podnapisi|www\.|https?://|>>.*<<|\.(srt|ass)\b", re.I)
HEAD = """[Script Info]
Title: {title}
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
{style}

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""


def head(title, style=None):
    import subtitle_style
    return HEAD.format(title=title, style=subtitle_style.style_line(style or subtitle_style.current()))


def ts(s):
    cs = int(round(max(0, s) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    return f"{h}:{m:02d}:{cs // 100:02d}.{cs % 100:02d}"


def line(l, rtl=True, flip=False):
    l = CTRL.sub("", l).strip()
    l = re.sub(r"</?(?:i|b|u|s|font)\b[^>]*>", "", l)    # ASS->SRT conversion leaves <font face=...> and <s> (strikeout)
    l = re.sub(r"\{\\(?!an\d)[^}]*\}", "", l)              # stray ASS overrides, except {\an8}
    l = re.sub(r"\s+", " ", l)
    tags = "".join(re.findall(r"^(?:\{[^}]*\})+", l))
    body = l[len(tags):].strip()
    if flip:                                            # visual-order track: closing punctuation back to the end
        m = LEADING.match(body)
        if m:
            body = m.group(2) + m.group(1)
    body = re.sub(r"^-(?=[^\s-])", "- ", body)          # speaker dash: always "- "
    l = tags + body
    body = l[len(tags):].strip()
    return tags + RLM + body + RLM if rtl else tags + body


def small_lines(layout):
    """From the official styled track: {start centiseconds: (size ratio, alignment, x, y)} for lines set clearly smaller
    than the dialogue (the "this story is fiction" note, episode titles, signs), with their anchor point as a fraction
    of the screen. Dialogue keeps the user's style; these keep their size relative to it and their place."""
    try:
        t = open(layout, encoding="utf-8-sig", errors="replace").read()
    except OSError:
        return {}
    res = [float(v) for v in (re.search(rf"^PlayRes{a}:\s*([\d.]+)", t, re.M) for a in "XY") if v for v in [v.group(1)]]
    rx, ry = res if len(res) == 2 and all(res) else (384.0, 288.0)          # the ASS defaults when PlayRes is missing
    f = re.search(r"^Format:(.*)$", t.split("[V4+ Styles]", 1)[1], re.M) if "[V4+ Styles]" in t else None
    fmt = [x.strip().lower() for x in f.group(1).split(",")] if f else []
    styles = {}
    for m in re.finditer(r"^Style:(.*)$", t, re.M):
        v = [x.strip() for x in m.group(1).split(",")]
        if fmt and len(v) == len(fmt):
            s = dict(zip(fmt, v))
            try:
                styles[s["name"]] = (float(s["fontsize"]), int(s["alignment"]), float(s["marginl"]), float(s["marginr"]), float(s["marginv"]))
            except (KeyError, ValueError):
                pass
    events = []
    for m in re.finditer(r"^Dialogue:\s*([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),(.*)$", t, re.M):
        if m.group(4).strip() in styles:
            h, mi, se = m.group(2).strip().split(":")
            events.append((round((int(h) * 3600 + int(mi) * 60 + float(se)) * 100), m.group(4).strip(),
                           [m.group(i).strip() for i in (6, 7, 8)], m.group(10)))
    if not events:
        return {}
    counts = {}
    for e in events:
        counts[e[1]] = counts.get(e[1], 0) + 1
    base = styles[max(counts, key=counts.get)][0]                          # the dialogue size: the most-used style's
    out = {}
    for start, name, margins, text in events:
        size, an, ml, mr, mv = styles[name]
        if not base or size >= 0.8 * base:
            continue
        ml, mr, mv = [float(o) if o.strip("0") else d for o, d in zip(margins, (ml, mr, mv))]
        p = re.search(r"\\pos\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)", text)
        a = re.search(r"\\an([1-9])", text)
        an = int(a.group(1)) if a else an
        if p:
            x, y = float(p.group(1)), float(p.group(2))
        else:
            x = ml if an in (1, 4, 7) else rx - mr if an in (3, 6, 9) else (ml + rx - mr) / 2
            y = ry - mv if an in (1, 2, 3) else ry / 2 if an in (4, 5, 6) else mv
        out.setdefault(start, (size / base, an, x / rx, y / ry))
    return out


def convert(src, dst, title, offset=0.0, style=None, lang=None, layout=None):
    """offset: seconds to add, or (split, before, after): lines starting before `split` get `before`, the rest `after`.
    layout: the official styled track `src` was converted from; its small lines keep their relative size and place."""
    import language
    import subtitle_style
    lang = lang or language.code()
    rtl, credit = language.TABLE[lang]["rtl"], language.credits(lang)
    t = open(src, encoding="utf-8-sig").read()
    ev = []
    blocks = re.split(r"\r?\n[ \t\r]*\n", t.strip())      # "blank" lines may hold stray spaces
    flip = rtl and visual_order([x for b in blocks for x in b.splitlines()[2:] if x.strip()])
    small = small_lines(layout) if layout else {}
    size = subtitle_style.clean(style or subtitle_style.current()).get("size", 60) if small else 0
    for b in blocks:
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+) --> (\d+):(\d+):(\d+)[,.](\d+)", b)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        lines = [x for x in b[m.end():].strip("\r\n").splitlines() if x.strip()]
        if not lines:
            continue
        if credit.match(CTRL.sub("", lines[0])):
            continue                                    # translator credit: user wants none
        if AD.search(" ".join(lines)):
            continue                                    # site advert / release-name line (OpenSubtitles etc.)
        txt = "\\N".join(line(x, rtl, flip) for x in lines)
        tag = "".join(re.findall(r"^(?:\{[^}]*\})+", txt))   # keep tag once, at the very start
        if tag:
            txt = tag + txt.replace(tag, "")
        s = small.get(round(a * 100))
        if s:                                           # a note or sign: its own size and place, not dialogue size
            tag = "".join(re.findall(r"^(?:\{[^}]*\})+", txt))
            txt = f"{{\\an{s[1]}\\pos({s[2] * 1920:.0f},{s[3] * 1080:.0f})\\fs{max(12, round(size * s[0]))}}}" + txt[len(tag):]
        o = offset if not isinstance(offset, tuple) else (offset[1] if a < offset[0] else offset[2])
        ev.append(f"Dialogue: 0,{ts(a + o)},{ts(e + o)},Default,,0,0,0,,{txt}")
    open(dst, "w", encoding="utf-8-sig", newline="\n").write(head(title, style) + "\n".join(ev) + "\n")
    return len(ev)


if __name__ == "__main__":
    print(convert(sys.argv[1], sys.argv[2], sys.argv[3],
                  float(sys.argv[4]) if len(sys.argv) > 4 else 0.0), "events")
