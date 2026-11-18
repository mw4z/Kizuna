"""Official Arabic SRT (logical order, RLE/RLM marks) -> ASS in the style chosen on the Settings page.

Every visual line is wrapped in RLM so edge punctuation and dialogue dashes
resolve right-to-left under libass; leading override tags such as {\\an8}
(sign translations) are kept outside the wrap. A timing offset is applied if given.
"""
import re
import sys

RLM = "‏"
CTRL = re.compile("[‎‏‪-‮⁦-⁩]")
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
    return HEAD.format(title=title, style=subtitle_style.style_line(style or subtitle_style.current("ar")))


def ts(s):
    cs = int(round(max(0, s) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    return f"{h}:{m:02d}:{cs // 100:02d}.{cs % 100:02d}"


def line(l):
    l = CTRL.sub("", l).strip()
    l = re.sub(r"</?(?:i|b|u|font)\b[^>]*>", "", l)      # ASS->SRT conversion leaves <font face=...>
    l = re.sub(r"\{\\(?!an\d)[^}]*\}", "", l)              # stray ASS overrides, except {\an8}
    l = re.sub(r"\s+", " ", l)
    tags = "".join(re.findall(r"^(?:\{[^}]*\})+", l))
    body = l[len(tags):].strip()
    body = re.sub(r"^-(?=[^\s-])", "- ", body)          # speaker dash: always "- "
    l = tags + body
    return tags + RLM + l[len(tags):].strip() + RLM


def convert(src, dst, title, offset=0.0, style=None):
    t = open(src, encoding="utf-8-sig").read()
    ev = []
    for b in re.split(r"\r?\n[ \t\r]*\n", t.strip()):     # "blank" lines may hold stray spaces
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+) --> (\d+):(\d+):(\d+)[,.](\d+)", b)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        lines = [x for x in b[m.end():].strip("\r\n").splitlines() if x.strip()]
        if not lines:
            continue
        if re.match(r"^\s*(\{[^}]*\})*\s*(ترجمة|تمت الترجمة|تدقيق)", CTRL.sub("", lines[0])):
            continue                                    # translator credit: user wants none
        if AD.search(" ".join(lines)):
            continue                                    # site advert / release-name line (OpenSubtitles etc.)
        txt = "\\N".join(line(x) for x in lines)
        tag = "".join(re.findall(r"^(?:\{[^}]*\})+", txt))   # keep tag once, at the very start
        if tag:
            txt = tag + txt.replace(tag, "")
        ev.append(f"Dialogue: 0,{ts(a + offset)},{ts(e + offset)},Default,,0,0,0,,{txt}")
    open(dst, "w", encoding="utf-8-sig", newline="\n").write(head(title, style) + "\n".join(ev) + "\n")
    return len(ev)


if __name__ == "__main__":
    print(convert(sys.argv[1], sys.argv[2], sys.argv[3],
                  float(sys.argv[4]) if len(sys.argv) > 4 else 0.0), "events")
