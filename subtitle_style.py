"""The look of the subtitles Kizuna builds (Settings > Subtitles), one style per subtitle language: validated style ->
ASS Style line, the font files to embed (so every player draws the same font), and a preview rendered by libass itself.

Kizuna builds one track, in the subtitle language chosen in Settings; the release's other tracks keep their look."""
import base64
import os
import re
import subprocess
import tempfile

import language

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
    "Noto Sans": ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"),     # Latin, Cyrillic, Greek, Vietnamese
}
LANGUAGES = tuple(language.TABLE)   # every language Kizuna can build a subtitle track for
DEFAULT = {                    # the original Kizuna house style (Arabic)
    "font": "Cairo", "size": 82, "bold": True, "color": "#FFFFFF",
    "outline": 2.6, "outline_color": "#2B2C2F",
    "shadow": 1.2, "shadow_color": "#000000", "shadow_opacity": 38,
    "box": False, "box_color": "#000000", "box_opacity": 60, "box_padding": 10,
    "position": "bottom", "margin": 42,
}
NUMBERS = {"size": (40, 140, int), "outline": (0, 8, float), "shadow": (0, 8, float), "shadow_opacity": (0, 100, int),
           "box_opacity": (0, 100, int), "box_padding": (2, 30, int), "margin": (10, 300, int)}
SAMPLES = {   # two preview lines per language (same meaning)
    "ar": ("- هل أنت متأكد أنه سيأتي الليلة؟", "- لا أعرف... لكن علينا أن ننتظر."),
    "en": ("- Are you sure he's coming tonight?", "- I don't know... but we have to wait."),
    "es": ("- ¿Estás seguro de que vendrá esta noche?", "- No lo sé... pero tenemos que esperar."),
    "pt": ("- Tem certeza de que ele vem esta noite?", "- Não sei... mas temos que esperar."),
    "fr": ("- Tu es sûr qu'il viendra ce soir ?", "- Je ne sais pas... mais il faut attendre."),
    "de": ("- Bist du sicher, dass er heute Nacht kommt?", "- Ich weiß nicht... aber wir müssen warten."),
    "it": ("- Sei sicuro che verrà stasera?", "- Non lo so... ma dobbiamo aspettare."),
    "ru": ("- Ты уверен, что он придёт сегодня?", "- Не знаю... но мы должны ждать."),
    "tr": ("- Bu gece geleceğinden emin misin?", "- Bilmiyorum... ama beklemek zorundayız."),
    "pl": ("- Jesteś pewien, że przyjdzie dziś wieczorem?", "- Nie wiem... ale musimy czekać."),
    "nl": ("- Weet je zeker dat hij vanavond komt?", "- Ik weet het niet... maar we moeten wachten."),
    "id": ("- Kau yakin dia akan datang malam ini?", "- Aku tidak tahu... tapi kita harus menunggu."),
    "fa": ("- مطمئنی امشب میاد؟", "- نمی‌دانم... ولی باید صبر کنیم."),
    "he": ("- אתה בטוח שהוא יבוא הלילה?", "- אני לא יודע... אבל אנחנו חייבים לחכות."),
}


def default_for(lang):
    """The starting style of a language: the house style with that language's font (smaller for Latin letters)."""
    i = language.TABLE[lang]
    return dict(DEFAULT, font=i["font"], **({} if i["rtl"] or lang == "ru" else {"size": 70}))


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


def current(lang=None):
    """The saved style of the subtitle language; its default if settings can't be read (never stops the agent)."""
    lang = lang or language.code()
    try:
        import settings
        return clean(settings.get(f"sub_style_{lang}"))
    except Exception:
        return default_for(lang)


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


def preview(style, bright=False, lang="ar"):
    """PNG (as a data: URL) of two sample lines drawn by libass with exactly this style, on a dark or bright scene."""
    import srt2ass
    s = clean(style)
    scene = "c0=0x1d2b4a:c1=0x0b0d12:c2=0x3a2a3f" if not bright else "c0=0xf3e7c9:c1=0x9ec5e8:c2=0xffffff"
    with tempfile.TemporaryDirectory() as work:
        text = "\\N".join(srt2ass.line(x, language.TABLE[lang]["rtl"]) for x in SAMPLES[lang])
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
