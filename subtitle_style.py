"""The look of the subtitles Kizuna builds (Settings > Subtitles), per language: validated style -> ASS Style line,
the font files to embed (so every player draws the same font), and a preview rendered by libass itself.

Kizuna builds only the Arabic track; other languages are the release's own tracks and keep their look."""
import base64
import os
import re
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
FONTS = {  # family name inside the font -> files embedded with it (all OFL, from Google Fonts; variable fonts were
    # cut into static Regular/Bold with fontTools, only for families without a Reserved Font Name)
    "Cairo": ("Cairo-Bold.ttf", "Cairo-SemiBold.ttf"),
    "Tajawal": ("Tajawal-Regular.ttf", "Tajawal-Bold.ttf"),
    "Almarai": ("Almarai-Regular.ttf", "Almarai-Bold.ttf"),
    "IBM Plex Sans Arabic": ("IBMPlexSansArabic-Regular.ttf", "IBMPlexSansArabic-Bold.ttf"),
    "Noto Kufi Arabic": ("NotoKufiArabic-Regular.ttf", "NotoKufiArabic-Bold.ttf"),
    "Vazirmatn": ("Vazirmatn-Regular.ttf", "Vazirmatn-Bold.ttf"),
    "Rubik": ("Rubik-Regular.ttf", "Rubik-Bold.ttf"),
    "Changa": ("Changa-Regular.ttf", "Changa-Bold.ttf"),
    "El Messiri": ("ElMessiri-Regular.ttf", "ElMessiri-Bold.ttf"),
    "Amiri": ("Amiri-Regular.ttf", "Amiri-Bold.ttf"),
    "Scheherazade New": ("ScheherazadeNew-Regular.ttf", "ScheherazadeNew-Bold.ttf"),
    "Markazi Text": ("MarkaziText-Regular.ttf", "MarkaziText-Bold.ttf"),
    "Lalezar": ("Lalezar-Regular.ttf",),
}
LANGUAGES = ("ar",)            # languages Kizuna builds a subtitle track for
DEFAULT = {                    # the original Kizuna house style
    "font": "Cairo", "size": 82, "bold": True, "color": "#FFFFFF",
    "outline": 2.6, "outline_color": "#2B2C2F",
    "shadow": 1.2, "shadow_color": "#000000", "shadow_opacity": 38,
    "box": False, "box_color": "#000000", "box_opacity": 60, "box_padding": 10,
    "position": "bottom", "margin": 42,
}
NUMBERS = {"size": (40, 140, int), "outline": (0, 8, float), "shadow": (0, 8, float), "shadow_opacity": (0, 100, int),
           "box_opacity": (0, 100, int), "box_padding": (2, 30, int), "margin": (10, 300, int)}
SAMPLE = ("- هل أنت متأكد أنه سيأتي الليلة؟", "- لا أعرف... لكن علينا أن ننتظر.")


def clean(raw, base=None):
    """Validated copy of a style (unknown keys dropped, missing keys from `base`). Raises ValueError with a message."""
    if not isinstance(raw, dict):
        raise ValueError("Subtitle style is not valid.")
    out = dict(base or DEFAULT)
    for key, value in raw.items():
        if key not in DEFAULT:
            continue
        if key == "font":
            if value not in FONTS:
                raise ValueError("Unknown subtitle font.")
            out[key] = value
        elif key in ("bold", "box"):
            out[key] = value is True or str(value).lower() in ("1", "true", "yes", "on")
        elif key.endswith("color"):
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(value)):
                raise ValueError("Subtitle colours must look like #RRGGBB.")
            out[key] = str(value).upper()
        elif key == "position":
            if value not in ("bottom", "top"):
                raise ValueError("Subtitle position must be bottom or top.")
            out[key] = value
        else:
            low, high, kind = NUMBERS[key]
            try:
                n = kind(float(value))
            except (TypeError, ValueError):
                raise ValueError("Subtitle sizes must be numbers.") from None
            if not low <= n <= high:
                raise ValueError(f"Subtitle {key.replace('_', ' ')} must be between {low} and {high}.")
            out[key] = round(n, 1) if kind is float else n
    return out


def _colour(hex_rgb, opacity=100):
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H{round(255 * (1 - opacity / 100)):02X}{b}{g}{r}".upper()


def style_line(style):
    s = clean(style)
    if s["box"]:                    # libass draws the opaque box (BorderStyle 3) in the outline colour
        border, outline, edge = 3, s["box_padding"], _colour(s["box_color"], s["box_opacity"])
    else:
        border, outline, edge = 1, s["outline"], _colour(s["outline_color"])
    return (f"Style: Default,{s['font']},{s['size']},{_colour(s['color'])},&H000000FF,{edge},"
            f"{_colour(s['shadow_color'], s['shadow_opacity'])},{-1 if s['bold'] else 0},0,0,0,95,100,0,0,{border},"
            f"{outline:g},{s['shadow']:g},{8 if s['position'] == 'top' else 2},180,180,{s['margin']},1")


def current(language="ar"):
    """The saved style for a language; the house style if settings can't be read (never stops the agent)."""
    try:
        import settings
        return clean(settings.get(f"sub_style_{language}"))
    except Exception:
        return dict(DEFAULT)


def font_files(ass_path):
    """Font files to embed for the font an ASS file uses (Cairo when unknown)."""
    family = "Cairo"
    try:
        m = re.search(r"^Style:\s*Default,([^,]+),", open(ass_path, encoding="utf-8-sig").read(), re.M)
        if m and m.group(1).strip() in FONTS:
            family = m.group(1).strip()
    except OSError:
        pass
    return [os.path.join(FONT_DIR, f) for f in FONTS[family]]


def preview(style, bright=False):
    """PNG (as a data: URL) of two sample lines drawn by libass with exactly this style, on a dark or bright scene."""
    import srt2ass
    s = clean(style)
    scene = "c0=0x1d2b4a:c1=0x0b0d12:c2=0x3a2a3f" if not bright else "c0=0xf3e7c9:c1=0x9ec5e8:c2=0xffffff"
    with tempfile.TemporaryDirectory() as work:
        text = "\\N".join(srt2ass.line(x) for x in SAMPLE)
        with open(os.path.join(work, "p.ass"), "w", encoding="utf-8-sig", newline="\n") as f:
            f.write(srt2ass.head("Preview", s) + f"Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,{text}\n")
        fonts = FONT_DIR.replace("\\", "/").replace(":", "\\:")
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"gradients=s=960x540:{scene}:n=3:seed=7:d=1",
                            "-vf", f"subtitles=p.ass:fontsdir='{fonts}'", "-frames:v", "1", "-f", "image2pipe",
                            "-vcodec", "png", "-"], cwd=work, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode or not r.stdout.startswith(b"\x89PNG"):
        raise RuntimeError("preview failed: " + r.stderr.decode("utf-8", "replace")[-300:])
    return "data:image/png;base64," + base64.b64encode(r.stdout).decode()
