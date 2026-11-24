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


def convert(src, dst, title, offset=0.0, style=None, lang=None):
    """offset: seconds to add, or (split, before, after): lines starting before `split` get `before`, the rest `after`."""
    import language
    lang = lang or language.code()
    rtl, credit = language.TABLE[lang]["rtl"], language.credits(lang)
    t = open(src, encoding="utf-8-sig").read()
    ev = []
    blocks = re.split(r"\r?\n[ \t\r]*\n", t.strip())      # "blank" lines may hold stray spaces
    flip = rtl and visual_order([x for b in blocks for x in b.splitlines()[2:] if x.strip()])
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
        o = offset if not isinstance(offset, tuple) else (offset[1] if a < offset[0] else offset[2])
        ev.append(f"Dialogue: 0,{ts(a + o)},{ts(e + o)},Default,,0,0,0,,{txt}")
    open(dst, "w", encoding="utf-8-sig", newline="\n").write(head(title, style) + "\n".join(ev) + "\n")
    return len(ev)


if __name__ == "__main__":
    print(convert(sys.argv[1], sys.argv[2], sys.argv[3],
                  float(sys.argv[4]) if len(sys.argv) > 4 else 0.0), "events")
