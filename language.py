"""The subtitle language Kizuna adds (Settings > Subtitles > Language). One table drives everything that depends on
it: which tracks and add-on subtitles count, the "is this really that language" check, credit lines to strip,
right-to-left handling, the burned-in subtitle OCR, the track Kizuna writes and the default subtitle font.

Kizuna's own track is recognised by its name: "العربية" for Arabic (the name every earlier version wrote, so existing
libraries stay recognised) and "<native name> (Kizuna)" for every other language, so a release's own "English"
track is never mistaken for ours."""
import os
import re

TABLE = {
    # code: name, native, codes (ISO 639-1/2, add-on lang prefixes), rtl, script (letters that must dominate),
    #       words (frequent words, to tell languages of one script apart), OCR (Windows), default font
    "ar": dict(name="Arabic", native="العربية", codes=("ar", "ara"), rtl=True, script="؀-ۿ",
               words=("في", "من", "على", "لا", "هذا", "أنا", "ماذا", "هل", "كان", "لقد", "إلى", "يا"),
               ocr="ar-SA", font="Cairo"),
    "en": dict(name="English", native="English", codes=("en", "eng"), rtl=False, script=None,
               words=("the", "you", "and", "to", "is", "it", "that", "what", "of", "this", "me", "don't"),
               ocr="en-US", font="Noto Sans"),
    "es": dict(name="Spanish", native="Español", codes=("es", "spa"), rtl=False, script=None,
               words=("que", "de", "no", "la", "el", "y", "es", "lo", "por", "qué", "un", "está"),
               ocr="es-ES", font="Noto Sans"),
    "pt": dict(name="Portuguese", native="Português", codes=("pt", "por"), rtl=False, script=None,
               words=("não", "que", "de", "você", "é", "um", "uma", "para", "eu", "com", "isso", "está"),
               ocr="pt-BR", font="Noto Sans"),
    "fr": dict(name="French", native="Français", codes=("fr", "fre", "fra"), rtl=False, script=None,
               words=("je", "de", "est", "pas", "le", "vous", "que", "et", "tu", "ce", "les", "c'est"),
               ocr="fr-FR", font="Noto Sans"),
    "de": dict(name="German", native="Deutsch", codes=("de", "ger", "deu"), rtl=False, script=None,
               words=("ich", "nicht", "das", "du", "die", "und", "ist", "der", "es", "sie", "zu", "wir"),
               ocr="de-DE", font="Noto Sans"),
    "it": dict(name="Italian", native="Italiano", codes=("it", "ita"), rtl=False, script=None,
               words=("non", "che", "di", "è", "il", "un", "la", "per", "sono", "mi", "ho", "questo"),
               ocr="it-IT", font="Noto Sans"),
    "ru": dict(name="Russian", native="Русский", codes=("ru", "rus"), rtl=False, script="Ѐ-ӿ",
               words=("не", "я", "что", "ты", "в", "и", "на", "это", "он", "мы", "как", "вы"),
               ocr="ru-RU", font="Noto Sans"),
    "tr": dict(name="Turkish", native="Türkçe", codes=("tr", "tur"), rtl=False, script=None,
               words=("bir", "ve", "bu", "ne", "ben", "sen", "için", "çok", "değil", "var", "ama", "mı"),
               ocr="tr-TR", font="Noto Sans"),
    "pl": dict(name="Polish", native="Polski", codes=("pl", "pol"), rtl=False, script=None,
               words=("nie", "to", "się", "jest", "że", "co", "jak", "mi", "ja", "ale", "tak", "na"),
               ocr="pl-PL", font="Noto Sans"),
    "nl": dict(name="Dutch", native="Nederlands", codes=("nl", "dut", "nld"), rtl=False, script=None,
               words=("ik", "je", "het", "een", "niet", "dat", "van", "wat", "we", "zijn", "is", "en"),
               ocr="nl-NL", font="Noto Sans"),
    "id": dict(name="Indonesian", native="Bahasa Indonesia", codes=("id", "ind"), rtl=False, script=None,
               words=("yang", "aku", "tidak", "ini", "itu", "dan", "apa", "kita", "di", "ada", "kau", "kamu"),
               ocr="id-ID", font="Noto Sans"),
    "fa": dict(name="Persian", native="فارسی", codes=("fa", "per", "fas"), rtl=True, script="؀-ۿ",
               words=("که", "این", "را", "به", "از", "تو", "یک", "نه", "است", "با", "چی", "من"),
               ocr="fa-IR", font="Vazirmatn"),
    "he": dict(name="Hebrew", native="עברית", codes=("he", "heb", "iw"), rtl=True, script="֐-׿",
               words=("את", "לא", "זה", "אני", "של", "על", "מה", "אתה", "הוא", "כן", "עם", "יש"),
               ocr="he-IL", font="Rubik"),
}
DEFAULT = "en"                       # new installs; this owner's settings.json says "ar"
LEGACY_OWN = "العربية"               # the Arabic track name every version wrote
RTL_MARK = "‏"
# translator / sync credit cues (a cue that starts like this is dropped); Arabic keeps its original pattern
CREDITS = {
    "ar": r"(ترجمة|تمت الترجمة|تدقيق)",
    "*": r"(subtitles?|subs|translat\w*|traduc\w*|tradu[çc]\w*|sous-titr\w*|untertitel\w*|übersetz\w*|sottotitol\w*|"
         r"legenda\w*|перевод\w*|субтитр\w*|çeviri\w*|altyaz\w*|napis\w*|tłumacz\w*|ondertitel\w*|vertaal\w*|"
         r"terjemah\w*|زیرنویس|ترجمه|תרגום|כתוביות)\s*(by|por|par|von|da|de|door|oleh|przez|от|:|-|–|—)",
}


def code():
    """The configured subtitle language (never raises: a broken setting means the default). The environment
    variable KIZUNA_SUBTITLE_LANGUAGE overrides it (tests; it reaches the worker processes too)."""
    forced = os.environ.get("KIZUNA_SUBTITLE_LANGUAGE")
    if forced in TABLE:
        return forced
    try:
        import settings
        c = settings.get("subtitle_language")
        return c if c in TABLE else DEFAULT
    except Exception:
        return DEFAULT


class SettingsUnreadable(RuntimeError):
    """settings.json exists but can't be read: don't guess the subtitle language."""


def require():
    """For the agent and the worker: the subtitle language, or SettingsUnreadable (they retry later)."""
    if not os.environ.get("KIZUNA_SUBTITLE_LANGUAGE"):
        import settings
        if settings.damaged():
            raise SettingsUnreadable("settings.json can't be read; subtitles wait until it is fixed")
    return code()


def info(c=None):
    return TABLE[c or code()]


def name(c=None):
    return info(c)["name"]


def is_code(value, c=None):
    """'ara', 'ar-SA', 'Arabic'... -> True for the subtitle language."""
    v = str(value or "").lower().split("-")[0].split("_")[0].strip()
    i = info(c)
    return v in i["codes"] or v == i["name"].lower()


def own_name(c=None):
    c = c or code()
    return LEGACY_OWN if c == "ar" else f"{info(c)['native']} (Kizuna)"


def is_own(track_name):
    """A subtitle track Kizuna wrote (any language, so switching languages never re-processes or loses them)."""
    n = str(track_name or "")
    return n == LEGACY_OWN or any(n == own_name(c) for c in TABLE if c != "ar")


def iso2(c=None):
    return info(c)["codes"][1]


def credits(c=None):
    c = c or code()
    return re.compile(r"^\s*(\{[^}]*\})*\s*" + CREDITS.get(c, CREDITS["*"]), re.I)


def words_of(text):
    return re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", text.lower())


def text_check(text, c=None):
    """(ok, reason): the text really is the subtitle language. Script languages need mostly their letters;
    languages sharing a script must also use more of their own frequent words than any other language."""
    c = c or code()
    i = TABLE[c]
    letters = re.findall(r"[^\W\d_]", text)
    if not letters:
        return False, f"not {i['name']}"
    if i["script"]:
        mine = sum(1 for ch in letters if re.match(f"[{i['script']}]", ch))
        if mine / len(letters) < 0.6:
            return False, f"not mostly {i['name']}"
    if c == "ar":                    # the original Arabic rule (script share) is all Arabic ever needed
        return True, ""
    words = words_of(text)
    if not words:
        return False, f"not {i['name']}"
    share = {k: sum(1 for w in words if w in set(v["words"])) / len(words) for k, v in TABLE.items()}
    best = max(share, key=share.get)
    if share[c] < 0.08 or (best != c and share[best] > share[c] * 1.2):
        return False, f"looks like {TABLE[best]['name']}, not {i['name']}"
    return True, ""


def has_letters(line, c=None):
    """The line holds dialogue in the subtitle language's script (copy detection works line by line)."""
    i = info(c)
    return bool(re.search(f"[{i['script']}]", line)) if i["script"] else bool(re.search(r"[^\W\d_]{2,}", line))


def choices():
    """For the Settings page: code -> (English name, native name)."""
    return {c: (v["name"], v["native"]) for c, v in TABLE.items()}
