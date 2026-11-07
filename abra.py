"""
Абракадабра v2 — голосовой помощник для macOS.

Запуск (из папки ~/abrakadabra):  uv run abra.py
Остановка: Ctrl+C или «Абракадабра, выключись».

КОМАНДЫ (всё после слова «Абракадабра»):
  Приложения:  открой / запусти / закрой <приложение>
  Поиск:       найди на ютубе <запрос> · найди <запрос> · открой сайт <название>
  Папки:       открой загрузки / документы / рабочий стол
  Звук:        громче · тише · громкость 50 · выключи звук · включи звук
  Экран:       ярче · темнее · заблокируй экран · усыпи мак (спросит подтверждение)
  Время:       который час · какое сегодня число · таймер на 10 минут
               напомни через 20 минут выключить чайник
  Погода:      какая погода
  Музыка:      пауза · продолжи · дальше · предыдущий трек   (Spotify или Музыка)
  Прочее:      сделай скриншот · напиши <текст> · выключись
"""

import collections
import datetime
import difflib
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import wave

import numpy as np
import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

# ═════════════════════════ НАСТРОЙКИ ═════════════════════════

WAKE = "абракадабра"
WAKE_THRESHOLD = 0.75     # чувствительность сторожа (ниже = чаще срабатывает)

# Распознавание команд. Если установлен mlx-whisper, работает на чипе Apple (в разы быстрее).
# Иначе — faster-whisper на процессоре.
MLX_MODEL = "mlx-community/whisper-small-mlx"   # точнее, но медленнее: "mlx-community/whisper-large-v3-turbo"
MODEL_SIZE = "small"      # для faster-whisper (запасной вариант)
BEAM_SIZE = 1

SAMPLE_RATE = 16000
CHUNK = 1600              # 0.1 с звука: быстрее реакция на слово и на конец фразы
PRE_ROLL_SEC = 1.0        # звук до срабатывания (там бывает начало команды)
SILENCE_END_SEC = 0.4     # пауза, после которой команда считается законченной
WAIT_SPEECH_SEC = 4.0     # сколько ждать начала команды после «Абракадабра»
NAME_PAUSE_SEC = 2.0      # сколько ждать команду, если после имени ты сделал паузу
FOLLOWUP_WAIT_SEC = 2.5   # если в записи оказалось только имя — сколько ещё ждать команду
MAX_COMMAND_SEC = 6.0
FAST_TIMEOUT_SEC = 180     # быстрый режим выключается сам после 3 минут без команд
SOUND_OPEN = "/System/Library/Sounds/Bottle.aiff"
SOUND_CLOSE = "/System/Library/Sounds/Pop.aiff"
SOUND_OK = "/System/Library/Sounds/Morse.aiff"
YES_NO_TIMEOUT_SEC = 6.0  # сколько ждать ответа «да / нет»

YES_WORDS = ["да", "давай", "ага", "конечно", "подтверждаю", "угу", "точно"]
NO_WORDS = ["нет", "отмена", "не надо", "стоп"]

# Фразы, которые Whisper иногда «слышит» в тишине
HALLUCINATIONS = ["продолжение следует", "субтитры", "спасибо за просмотр", "редактор субтитров",
                  "найди на youtube. таймер", "открой safari. найди"]
MIN_SPEECH_SEC = 0.3       # короче — это щелчок или шорох, а не команда

# Голос Piper: файл лежит в ~/abrakadabra/voices/<PIPER_VOICE>.onnx
# Варианты: "ru_RU-denis-medium", "ru_RU-dmitri-medium"
PIPER_VOICE = "ru_RU-dmitri-medium"   # голос по умолчанию; «смени голос» переключает
VOICES_DIR = os.path.expanduser("~/abrakadabra/voices")

SOUND_WAKE = "/System/Library/Sounds/Tink.aiff"
SOUND_ERROR = "/System/Library/Sounds/Basso.aiff"
SOUND_ALARM = "/System/Library/Sounds/Glass.aiff"

ALIASES = {
    "сафари": "Safari",
    "хром": "Google Chrome", "гугл хром": "Google Chrome",
    "опера": "Opera", "оперу": "Opera",
    "яндекс": "Yandex", "яндекс браузер": "Yandex",
    "телеграм": "Telegram", "телеграмм": "Telegram", "телега": "Telegram", "телегу": "Telegram",
    "ватсап": "WhatsApp", "вотсап": "WhatsApp",
    "дискорд": "Discord",
    "спотифай": "Spotify",
    "обсидиан": "Obsidian",
    "клод": "Claude",
    "вс код": "Visual Studio Code", "вскод": "Visual Studio Code",
    "настройки": "System Settings", "системные настройки": "System Settings",
    "заметки": "Notes",
    "музыка": "Music", "музыку": "Music",
    "калькулятор": "Calculator",
    "терминал": "Terminal",
    "файндер": "Finder",
    "почта": "Mail", "почту": "Mail",
    "календарь": "Calendar",
    "фото": "Photos", "фотографии": "Photos",
    "сообщения": "Messages",
    "напоминания": "Reminders",
    "карты": "Maps",
    "магазин": "App Store", "апп стор": "App Store", "эпп стор": "App Store",
}

SITES = {
    # ключ — как говорят (по-русски и как Whisper пишет латиницей) → адрес
    "ютуб": "youtube.com", "youtube": "youtube.com", "ютуб музыка": "music.youtube.com",
    "вк": "vk.com", "вконтакте": "vk.com", "vk": "vk.com", "wk": "vk.com", "вк музыка": "vk.com/audio",
    "яндекс": "ya.ru", "яндекс музыка": "music.yandex.ru", "кинопоиск": "kinopoisk.ru",
    "гугл": "google.com", "google": "google.com", "джимейл": "mail.google.com", "gmail": "mail.google.com",
    "почта яндекс": "mail.yandex.ru", "мейл": "mail.ru",
    "гитхаб": "github.com", "github": "github.com",
    "клод": "claude.ai", "claude": "claude.ai", "чат гпт": "chatgpt.com", "chatgpt": "chatgpt.com",
    "телеграм веб": "web.telegram.org",
    "тикток": "tiktok.com", "tiktok": "tiktok.com", "инстаграм": "instagram.com", "instagram": "instagram.com",
    "твич": "twitch.tv", "twitch": "twitch.tv", "реддит": "reddit.com", "reddit": "reddit.com",
    "википедия": "ru.wikipedia.org", "саундклауд": "soundcloud.com", "soundcloud": "soundcloud.com",
    "спотифай": "open.spotify.com", "spotify": "open.spotify.com",
    "рутуб": "rutube.ru", "дзен": "dzen.ru", "хабр": "habr.com",
    "авито": "avito.ru", "озон": "ozon.ru", "вайлдберриз": "wildberries.ru", "вб": "wildberries.ru",
    "госуслуги": "gosuslugi.ru", "пинтерест": "pinterest.com", "нетфликс": "netflix.com",
    "твиттер": "x.com", "икс": "x.com",
}

FOLDERS = {
    "загрузки": "~/Downloads",
    "документы": "~/Documents",
    "рабочий стол": "~/Desktop",
    "картинки": "~/Pictures", "изображения": "~/Pictures",
    "программы": "/Applications",
    "абракадабра": "~/abrakadabra", "проект": "~/abrakadabra",
}

SETTINGS_FILE = os.path.expanduser("~/abrakadabra/settings.json")
TLDS = {"com", "ru", "org", "net", "io", "ai", "tv", "me", "app", "dev", "by", "kz", "ua", "su", "info", "co"}
BROWSERS = {"Safari", "Google Chrome", "Opera", "Yandex", "Firefox", "Arc", "Microsoft Edge",
            "Brave Browser", "Opera GX", "Vivaldi", "Chromium"}

APP_DIRS = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
            os.path.expanduser("~/Applications")]

# Подсказку для Whisper не используем: на коротком шуме он выдавал её как текст
# («...Найди на YouTube. Таймер на 5 минут») и запускал случайные команды.
WHISPER_PROMPT = None

# ═════════════════════════ ГОЛОС ═════════════════════════


class Voice:
    """Говорит голосом Piper, а если он не установлен — системным голосом macOS."""

    def __init__(self):
        self.piper = None
        self.current = None
        try:
            from piper import PiperVoice
            self._PiperVoice = PiperVoice
        except Exception as e:  # piper не установлен
            self._PiperVoice = None
            print(f"Piper недоступен ({e}), говорю системным голосом.")
        saved = os.path.join(VOICES_DIR, "current.txt")
        name = open(saved).read().strip() if os.path.exists(saved) else PIPER_VOICE
        self.load(name) or self.load(PIPER_VOICE) or self.load((self.available() or [None])[0])
        self.q: queue.Queue = queue.Queue()
        self.busy = False          # сейчас говорит — микрофон в это время игнорируется
        self.quiet_until = 0.0     # и ещё чуть-чуть после
        threading.Thread(target=self._worker, daemon=True).start()

    def is_talking(self) -> bool:
        return self.busy or not self.q.empty() or time.time() < self.quiet_until

    @staticmethod
    def available() -> list:
        try:
            return sorted(f[:-5] for f in os.listdir(VOICES_DIR) if f.endswith(".onnx"))
        except FileNotFoundError:
            return []

    def load(self, name) -> bool:
        if not name or not self._PiperVoice:
            return False
        path = os.path.join(VOICES_DIR, name + ".onnx")
        if not os.path.exists(path):
            return False
        try:
            self.piper = self._PiperVoice.load(path)
            self.current = name
            with open(os.path.join(VOICES_DIR, "current.txt"), "w") as f:
                f.write(name)
            return True
        except Exception as e:
            print(f"Не загрузился голос {name}: {e}")
            return False

    def next_voice(self) -> str:
        voices = self.available()
        if not voices:
            return "системный"
        i = voices.index(self.current) if self.current in voices else -1
        self.load(voices[(i + 1) % len(voices)])
        return self.current

    @property
    def name(self) -> str:
        return self.current if self.piper else "системный macOS"

    def _synth(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as wf:
            if hasattr(self.piper, "synthesize_wav"):   # piper-tts 1.3+
                self.piper.synthesize_wav(text, wf)
            else:                                       # piper-tts 1.2
                self.piper.synthesize(text, wf)
        return path

    def _worker(self):
        while True:
            text, done = self.q.get()
            self.busy = True
            try:
                if self.piper:
                    path = self._synth(text)
                    subprocess.run(["afplay", path])
                    os.remove(path)
                else:
                    subprocess.run(["say", text])
            except Exception as e:
                print(f"Ошибка голоса: {e}")
            finally:
                self.quiet_until = time.time() + 0.25
                self.busy = False
                if done:
                    done.set()

    def say(self, text: str, wait: bool = False) -> None:
        print(f"🗣  {text}")
        done = threading.Event() if wait else None
        self.q.put((text, done))
        if done:
            done.wait(timeout=15)


def play(sound: str) -> None:
    subprocess.Popen(["afplay", sound])


def osa(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0 and r.stderr.strip():
        print(f"⚠️  osascript: {r.stderr.strip()[:200]}")
    return r.stdout.strip()

# ═════════════════════════ ПОИСК ПРИЛОЖЕНИЙ ═════════════════════════

TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
})


def installed_apps() -> dict:
    apps = {}
    for d in APP_DIRS:
        try:
            for name in os.listdir(d):
                if name.endswith(".app") and name.lower() != "abrakadabra.app":
                    apps[name[:-4].lower()] = name[:-4]
        except FileNotFoundError:
            pass
    return apps


def resolve_app(spoken: str, apps: dict):
    s = spoken.strip().lower()
    if not s:
        return None
    if s in ALIASES and ALIASES[s].lower() in apps:
        return apps[ALIASES[s].lower()]
    if s in apps:
        return apps[s]
    m = difflib.get_close_matches(s, ALIASES.keys(), n=1, cutoff=0.75)
    if m and ALIASES[m[0]].lower() in apps:
        return apps[ALIASES[m[0]].lower()]
    latin = s.translate(TRANSLIT)
    m = difflib.get_close_matches(latin, apps.keys(), n=1, cutoff=0.6)
    if m:
        return apps[m[0]]
    for key, real in apps.items():
        if key.startswith(latin) or key.startswith(s):
            return real
    return None


def match_key(target: str, table: dict, cutoff: float = 0.75):
    m = difflib.get_close_matches(target, table.keys(), n=1, cutoff=cutoff)
    return m[0] if m else None


# ── Выделение нужного слова из фразы с лишними словами ──

JUNK = {"ну", "давай", "короче", "блин", "бля", "блять", "это", "а", "и", "потом", "пожалуйста",
        "мне", "быстро", "там", "вот", "же", "ка", "сука", "нахуй", "плиз", "ещё", "еще", "уже",
        "сейчас", "щас", "так", "типа", "слушай", "эй", "можешь", "мог", "бы", "быстрее"}

APP_THRESHOLD = 0.72
FOLDER_THRESHOLD = 0.8
SITE_THRESHOLD = 0.8


def stem(w: str) -> str:
    """Отрезает окончание: «сафаря» → «сафар», «заметку» → «замет»."""
    return re.sub(r"[аеиоуыэюяйь]+$", "", w) if len(w) >= 5 else w


PHON = str.maketrans({"б": "п", "в": "ф", "г": "к", "д": "т", "ж": "ш", "з": "с", "ё": "е", "э": "е"})


def phon(w: str) -> str:
    """Как слово звучит: «клод» и «клот» звучат одинаково (оглушение согласных)."""
    return stem(w).translate(PHON)


def ratio(a: str, b: str) -> float:
    r = difflib.SequenceMatcher(None, a, b).ratio()
    sa, sb = stem(a), stem(b)
    if sa != a or sb != b:
        r = max(r, difflib.SequenceMatcher(None, sa, sb).ratio())
    if len(a) >= 4 and len(b) >= 4:      # для коротких слов слишком много совпадений
        r = max(r, difflib.SequenceMatcher(None, phon(a), phon(b)).ratio())
    return r


def score_app(s: str, apps: dict):
    """Насколько фраза похожа на какое-то приложение: (оценка 0..1, имя приложения)."""
    s = s.strip().lower()
    if not s:
        return 0.0, None
    if s in ALIASES and ALIASES[s].lower() in apps:
        return 1.0, apps[ALIASES[s].lower()]
    if s in apps:
        return 1.0, apps[s]
    best = (0.0, None)
    for alias, real in ALIASES.items():
        if real.lower() in apps:
            r = ratio(s, alias)
            if r > best[0]:
                best = (r, apps[real.lower()])
    latin = s.translate(TRANSLIT)
    for key, real in apps.items():
        r = max(ratio(latin, key), ratio(s, key))
        if r > best[0]:
            best = (r, real)
    if best[0] < 0.8 and len(latin) >= 4:
        for key, real in apps.items():
            if key.startswith(latin) or key.startswith(s):
                best = (0.8, real)
                break
    return best


def score_table(s: str, table: dict):
    best = (0.0, None)
    latin = s.translate(TRANSLIT)
    for key in table:
        r = max(ratio(s, key), ratio(latin, key), ratio(latin, key.translate(TRANSLIT)))
        if r > best[0]:
            best = (r, key)
    return best


def best_in_phrase(target: str, scorer, max_words: int = 3):
    """Перебирает куски фразы по 1–3 слова и возвращает лучшее совпадение.
    «заметки ну давай» → «заметки». При равной оценке — более длинный кусок и тот, что раньше."""
    words = target.split()
    best_score, best_val, best_len = 0.0, None, 0
    for i in range(len(words)):
        for n in range(1, max_words + 1):
            gram = words[i:i + n]
            if len(gram) < n:
                break
            if gram[0] in JUNK or gram[-1] in JUNK:
                continue
            sc, val = scorer(" ".join(gram))
            if sc > best_score + 1e-9 or (abs(sc - best_score) < 1e-9 and n > best_len):
                best_score, best_val, best_len = sc, val, n
    return best_score, best_val


def find_target(target: str, apps: dict, strict: bool = False):
    """Что имелось в виду: ('folder' | 'app' | 'site', значение) или (None, None).
    strict=True — для названия без «открой»: нужна более точная похожесть."""
    extra = 0.13 if strict else 0.0
    folders = all_folders()
    f_sc, folder = best_in_phrase(target, lambda s: score_table(s, folders))
    a_sc, app = best_in_phrase(target, lambda s: score_app(s, apps))
    s_sc, site = best_in_phrase(target, lambda s: score_table(s, SITES))
    options = []
    if f_sc >= FOLDER_THRESHOLD + extra:
        options.append((f_sc, 2, "folder", folder))   # 2,1,0 — приоритет при равной оценке
    if a_sc >= APP_THRESHOLD + extra:
        options.append((a_sc, 1, "app", app))
    if s_sc >= SITE_THRESHOLD + extra:
        options.append((s_sc, 0, "site", site))
    if not options:
        return None, None
    _, _, kind, val = max(options)
    return kind, val

def all_folders() -> dict:
    """Основные папки + папки внутри рабочего стола, документов, загрузок, картинок."""
    res = {k: os.path.expanduser(v) for k, v in FOLDERS.items()}
    for base in ("~/Desktop", "~/Documents", "~/Downloads", "~/Pictures"):
        b = os.path.expanduser(base)
        try:
            for name in os.listdir(b):
                p = os.path.join(b, name)
                if not name.startswith(".") and os.path.isdir(p) and not name.endswith(".app"):
                    res.setdefault(normalize(name), p)
        except OSError:
            pass
    return res


def send_media_key(key: int) -> None:
    """Нажимает системную медиаклавишу (16 play/pause, 17 next, 18 previous) через AppKit."""
    import ctypes
    import ctypes.util
    objc = ctypes.CDLL(ctypes.util.find_library("objc"))
    ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    class NSPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    make = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, NSPoint,
                            ctypes.c_ulong, ctypes.c_double, ctypes.c_long, ctypes.c_void_p,
                            ctypes.c_short, ctypes.c_long, ctypes.c_long)(("objc_msgSend", objc))
    to_cg = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]

    ns_event = objc.objc_getClass(b"NSEvent")
    sel_make = objc.sel_registerName(
        b"otherEventWithType:location:modifierFlags:timestamp:windowNumber:context:subtype:data1:data2:")
    sel_cg = objc.sel_registerName(b"CGEvent")
    for down in (True, False):
        flags = 0xA00 if down else 0xB00
        data1 = (key << 16) | ((0xA if down else 0xB) << 8)
        ev = make(ns_event, sel_make, 14, NSPoint(0, 0), flags, 0.0, 0, None, 8, data1, -1)
        if not ev:
            raise RuntimeError("NSEvent не создан")
        cgev = to_cg(ev, sel_cg)
        cg.CGEventPost(0, cgev)
        time.sleep(0.02)


# ═════════════════════════ ЧИСЛА И ВРЕМЯ ═════════════════════════

NUM_WORDS = {
    "одну": 1, "один": 1, "одна": 1, "пару": 2, "две": 2, "два": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
}
UNITS = {"сек": 1, "мин": 60, "час": 3600}


def parse_duration(s: str):
    """«10 минут выключить чайник» → (600, «выключить чайник»)."""
    s = s.strip()
    fixed = {"полчаса": 1800, "полминуты": 30, "час": 3600, "минуту": 60, "секунду": 1}
    m = re.match(r"(полчаса|полминуты|час|минуту|секунду)\b\s*(.*)", s)
    if m:
        return fixed[m.group(1)], m.group(2)
    m = re.match(r"(\d+|[а-я]+)\s+(сек\w*|мин\w*|час\w*)\s*(.*)", s)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUM_WORDS.get(m.group(1))
        if n:
            return n * UNITS[m.group(2)[:3]], m.group(3)
    return None, s


def human_duration(sec: int) -> str:
    if sec % 3600 == 0:
        return f"{sec // 3600} ч"
    if sec % 60 == 0:
        return f"{sec // 60} мин"
    return f"{sec} сек"


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

# ═════════════════════════ РАЗБОР КОМАНДЫ ═════════════════════════

FILLERS = r"(?:мне|пожалуйста|быстро|ка)"
YT = r"(?:ютубе|ютуб|youtube|ютюбе|ютубчике)"
GOOGLE = r"(?:гугле|гугл|google|интернете|браузере)"


def normalize(text: str) -> str:
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def clean(t: str) -> str:
    t = re.sub(rf"\b{FILLERS}\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse(text: str):
    """Текст → (действие, данные). Порядок важен: команды с «хвостом» проверяются первыми."""
    t = normalize(text)

    if re.search(r"\b(выключись|отключись|заверши работу)\b", t):
        return "stop", None

    if re.search(r"(обычн\w* режим|(?:выйди|выйти|выход)\w* из\b|(?:выключ|заверш|отключ|останов)\w*\s+(?:\w+\s+)?режим"
                 r"|хватит слушать|^(?:ну |все |всё |так )?хватит$|абракадабра хватит)", t):
        return "mode", "off"
    if re.search(r"(?:отмен|останов|сброс|убер|выключ|удали)\w*\s+(?:все\s+)?(?:таймер|напоминан)", t):
        return "cancel_timers", None
    if re.search(r"(смени|поменяй|другой|следующий)\s+голос", t):
        return "voice", None
    if re.search(r"(быстр\w* режим|постоянн\w* режим|непрерывн\w* режим|режим без имени|слушай меня)", t):
        return "mode", "on"

    m = re.search(rf"(?:найди|поищи|включи|открой|покажи)\s+(?:{FILLERS}\s+)?(?:в|на)\s+{YT}\s+(.+)", t)
    if m:
        return "youtube", clean(m.group(1))

    m = re.search(r"(?:открой|найди|покажи)\s+(?:мне\s+)?файл\w*\s+(.+)", t)
    if m:
        return "file", clean(m.group(1))

    m = re.search(rf"(?:найди|поищи|загугли)\s+(?:{FILLERS}\s+)?(?:(?:в|на)\s+{GOOGLE}\s+)?(.+)", t)
    if m:
        return "google", clean(m.group(1))

    # Диктовка: берём ОРИГИНАЛЬНЫЙ текст с регистром и знаками препинания
    m = re.search(r"(?:напиши|напечатай|введи)\s+(.+)", text, re.IGNORECASE)
    if m:
        return "type", m.group(1).strip()

    m = re.search(r"напомни\s+(?:мне\s+)?через\s+(.+)", t)
    if m:
        sec, rest = parse_duration(m.group(1))
        rest = re.sub(r"^(?:о том,? что|о том,? чтобы|что|чтобы|о|об|про)\s+", "", rest)
        return "remind", {"sec": sec, "text": rest}

    m = re.search(r"(?:таймер|засеки)\s+(?:на\s+)?(.+)", t)
    if m:
        sec, _ = parse_duration(m.group(1))
        return "timer", {"sec": sec}

    m = re.search(r"(?:открой|зайди на)\s+(?:мне\s+)?сайт\s+(.+)", t)
    if m:
        return "site", clean(m.group(1))

    # Создать папку: «создай папку Проекты на рабочем столе». Имя берём из оригинала (регистр).
    m = re.search(r"созда\w*\s+(?:новую\s+)?папк\w*\s+(.+)", text, re.IGNORECASE)
    if m:
        raw = re.sub(r"[.!?]+$", "", m.group(1)).strip()
        loc = None
        lm = re.search(r"\s+(?:в|во|на)\s+(.+)$", raw)
        if lm:
            loc, raw = lm.group(1), raw[:lm.start()]
        return "mkdir", {"name": raw.strip(" ,"), "where": normalize(loc) if loc else None}

    m = re.search(r"открой\s+(?:мне\s+)?папку\s+(.+)", t)
    if m:
        return "folder", clean(m.group(1))

    if re.search(r"(который час|сколько времени|сколько сейчас времени)", t):
        return "time", None
    if re.search(r"(какое .*число|какой .*день|какая .*дата|какое сегодня)", t):
        return "date", None
    if "погод" in t:
        return "weather", None

    m = re.search(r"громкость\s+(?:на\s+)?(\d+)", t)
    if m:
        return "volume", int(m.group(1))
    if re.search(r"(выключи звук|без звука|убери звук|отключи звук)", t):
        return "volume", "mute"
    if re.search(r"(включи звук|верни звук)", t):
        return "volume", "unmute"
    if re.search(r"\b(громче|погромче)\b", t):
        return "volume", "up"
    if re.search(r"\b(тише|потише)\b", t):
        return "volume", "down"

    if re.search(r"(максимальн\w* яркост|яркост\w* (?:на )?максимум|полн\w* яркост)", t):
        return "brightness", 100
    if re.search(r"(минимальн\w* яркост|яркост\w* (?:на )?минимум)", t):
        return "brightness", 0
    m = re.search(r"яркост\w*\s+(?:на\s+)?(\d+)", t)
    if m:
        return "brightness", int(m.group(1))
    if re.search(r"\b(ярче|поярче|добавь яркост\w*)\b", t):
        return "brightness", "up"
    if re.search(r"\b(темнее|потемнее|убавь яркост\w*)\b", t):
        return "brightness", "down"

    if re.search(r"(блокир|блокировк)", t) and "разблок" not in t:
        return "lock", None
    if re.search(r"(усыпи|режим сна|спящий режим)", t):
        return "sleep", None

    short = len(words_after_wake(t, keep_junk=True)) <= 2   # «дальше», «пауза» — только отдельной командой
    if re.search(r"(следующ\w* трек|следующ\w* песн\w*|переключи трек)", t) or \
            (short and re.search(r"\bдальше\b", t)):
        return "music", "next track"
    if re.search(r"(предыдущ\w* трек|прошл\w* трек|предыдущ\w* песн\w*)", t):
        return "music", "previous track"
    if re.search(r"(на паузу|пауз\w* музык|останови музык\w*|выключи музык\w*|стоп музык\w*|музык\w* на паузу)", t) \
            or (short and re.search(r"\bпауз\w*\b", t)):
        return "music", "pause"
    if re.search(r"(\bпродолжи\b|включи музыку|\bиграй\b|сними с паузы)", t):
        return "music", "play"

    if re.search(r"(ск\w{0,4}н\w{0,4}шот|скрин|снимок экрана|сфоткай экран)", t):
        return "screenshot", None

    # «закрой» / «закрыть» без названия — закрыть последнее открытое
    if re.search(r"\b(?:закрой|закрыть)\b(?:\s+(?:его|ее|это|их|приложение))?$", t):
        return "close", None

    m = re.search(r"(?:закрой|закрыть|выключи|заверши)\s+(.+)", t)
    if m:
        return "close", clean(m.group(1))

    m = re.search(r"(?:открой|открыть|запусти|включи)\s+(.+)", t)
    if m:
        return "open", clean(m.group(1))

    # Просто название без глагола: «Абракадабра, Safari»
    rest = words_after_wake(t)
    if 0 < len(rest) <= 4:
        return "bare", " ".join(rest)

    return None, None


def words_after_wake(t: str, keep_junk: bool = False) -> list:
    """Слова после имени помощника, без «привет» (и без мусорных слов, если keep_junk=False)."""
    words = t.split()
    last = -1
    for i, w in enumerate(words):
        if is_wake(w):
            last = i
    rest = words[last + 1:]
    while rest and (rest[0] == "привет" or is_wake_like(rest[0])):   # искажённое имя в начале
        rest = rest[1:]
    return [w for w in rest if w != "привет" and (keep_junk or w not in JUNK)]

# ═════════════════════════ ДЕЙСТВИЯ ═════════════════════════


class Actions:
    """Только действия из этого списка. Произвольные команды не выполняются никогда."""

    def __init__(self, voice: Voice, apps: dict, ask_yes_no, ask_text=None):
        self.voice = voice
        self.apps = apps
        self.ask_yes_no = ask_yes_no
        self.ask_text = ask_text or (lambda q: "")
        self.opened = []         # приложения, открытые голосом, по порядку (стопка)
        self.timers = []         # активные таймеры и напоминания
        self.fast = False        # быстрый режим: команды без имени
        self.fast_until = 0.0

    def ack(self, text: str, sound: str = SOUND_OK):
        """Подтверждение — только звуком. Голос — когда нужно что-то сообщить или не понял."""
        print(f"✔️  {text}")
        play(sound)

    def do_mode(self, mode):
        if mode == "on":
            self.fast = True
            self.fast_until = time.time() + FAST_TIMEOUT_SEC
            self.voice.say("Быстрый режим. Говори без имени")
        else:
            self.fast = False
            self.voice.say("Обычный режим")

    def fail(self, text: str):
        play(SOUND_ERROR)
        self.voice.say(text)

    def run(self, action: str, data):
        fn = getattr(self, "do_" + action, None)
        if fn is None:
            self.fail("Не понял команду")
        else:
            fn(data)

    # ── Настройки ──
    def settings(self) -> dict:
        try:
            with open(SETTINGS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings(self, data: dict):
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️  settings: {e}")

    def open_url(self, url: str):
        if not url.startswith("http"):
            url = "https://" + url
        browser = self.settings().get("browser")
        if browser and browser in self.apps.values():
            subprocess.run(["open", "-a", browser, url])
        else:
            subprocess.run(["open", url])
        print(f"🌐 {url} → {browser or 'браузер macOS по умолчанию'}")

    def maybe_set_browser(self, app: str):
        """Первый раз открыл браузер голосом — спросить, открывать ли в нём сайты."""
        st = self.settings()
        asked = st.setdefault("asked_browsers", [])
        if app == st.get("browser") or app in asked:
            return
        asked.append(app)
        if self.ask_yes_no(f"Открывать сайты в {app}?"):
            st["browser"] = app
            self.voice.say(f"Хорошо, сайты в {app}")
        self.save_settings(st)

    # ── Приложения, сайты, папки ──
    def do_open(self, target, strict: bool = False):
        if not target:
            return self.fail("Не расслышал, что открыть")
        kind, val = find_target(target, self.apps, strict=strict)
        print(f"🎯 «{target}» → {kind}: {val}")
        if kind == "folder":
            return self.do_folder(val)
        if kind == "app":
            subprocess.run(["open", "-a", val])
            self.push_opened(val)
            self.ack(f"Открываю {val}", SOUND_OPEN)
            if val in BROWSERS:
                self.maybe_set_browser(val)
            return
        if kind == "site":
            return self.do_site(val)
        if strict:
            print(f"🤷 «{target}» — не похоже ни на одно приложение")
            return play(SOUND_ERROR)
        self.fail(f"Не нашёл {target}")

    def do_bare(self, target):
        """Сказано только название: «Safari», «заметки»."""
        self.do_open(target, strict=True)

    def push_opened(self, item):
        if item in self.opened:
            self.opened.remove(item)
        self.opened.append(item)

    @staticmethod
    def is_running(app: str) -> bool:
        return osa(f'application "{app}" is running') == "true"

    @staticmethod
    def close_folder_window(path: str) -> bool:
        """Закрывает окна Finder с этой папкой. Способ 1 — сравнение пути, способ 2 — по имени окна."""
        p = os.path.realpath(os.path.expanduser(path))
        out = osa(f'''set AppleScript's text item delimiters to " ; "
tell application "Finder"
  set tf to (POSIX file "{p}") as alias
  set tfText to tf as text
  set before to count of Finder windows
  set seen to {{}}
  repeat with i from before to 1 by -1
    try
      set w to Finder window i
      set t to (target of w) as alias
      set end of seen to (POSIX path of t)
      if (t as text) is tfText then close w
    on error errMsg
      set end of seen to ("ошибка: " & errMsg)
    end try
  end repeat
  if (count of Finder windows) is before then
    try
      set dn to displayed name of tf
      close (every Finder window whose name is dn)
    end try
  end if
  set closedCount to before - (count of Finder windows)
  return (closedCount as text) & "|" & (seen as text)
end tell''')
        closed, _, seen = out.partition("|")
        print(f"🗂  закрытие {p}: закрыто {closed or '?'}; окна Finder: {seen or 'нет'}")
        return closed.strip() not in ("", "0")

    def close_front_finder_window(self) -> bool:
        before = osa('tell application "Finder" to count of Finder windows')
        osa('tell application "Finder" to if (count of Finder windows) > 0 then close front Finder window')
        after = osa('tell application "Finder" to count of Finder windows')
        return before != after

    def do_close(self, target):
        if not target:
            # «закрой» без названия: в обратном порядке открытия (приложения и папки).
            while self.opened:
                item = self.opened.pop()
                if isinstance(item, tuple):                    # ("folder", путь)
                    if self.close_folder_window(item[1]):
                        return self.ack(f"Закрываю папку {item[1]}", SOUND_CLOSE)
                elif self.is_running(item):
                    osa(f'quit app "{item}"')
                    return self.ack(f"Закрываю {item}", SOUND_CLOSE)
            front = osa('tell application "System Events" to get name of first '
                        'application process whose frontmost is true')
            if front and front != "Finder":
                osa(f'quit app "{front}"')
                return self.ack(f"Закрываю {front}", SOUND_CLOSE)
            return self.fail("Нечего закрывать")

        # «Закрой папку» без названия — последнюю открытую папку или переднее окно Finder
        target = re.sub(r"\b(?:эту|это|окно|папк\w*)\b", " ", target).strip()
        if not target:
            for item in reversed(self.opened):
                if isinstance(item, tuple):
                    self.opened.remove(item)
                    if self.close_folder_window(item[1]):
                        return self.ack(f"Закрываю папку {item[1]}", SOUND_CLOSE)
            if self.close_front_finder_window():
                return self.ack("Закрываю окно Finder", SOUND_CLOSE)
            return self.fail("Нет открытых папок")

        # Сначала — не папка ли это
        f_sc, folder = best_in_phrase(target, lambda s: score_table(s, all_folders()))
        a_sc, app = best_in_phrase(target, lambda s: score_app(s, self.apps))
        print(f"🎯 «{target}» → папка: {folder} ({f_sc:.2f}), приложение: {app} ({a_sc:.2f})")
        if folder and f_sc >= FOLDER_THRESHOLD and f_sc >= a_sc:
            path = all_folders()[folder]
            if self.close_folder_window(path):
                return self.ack(f"Закрываю папку {folder}", SOUND_CLOSE)
            return self.fail(f"Папка {folder} не открыта")
        if a_sc < APP_THRESHOLD or not app:
            return self.fail(f"Не нашёл {target}")
        osa(f'quit app "{app}"')
        if app in self.opened:
            self.opened.remove(app)
        self.ack(f"Закрываю {app}", SOUND_CLOSE)

    def do_site(self, target):
        target = target or ""
        key = match_key(target, SITES, 0.75)
        if key:
            self.open_url(SITES[key])
            return self.ack(f"Открываю {SITES[key]}", SOUND_OPEN)
        words = target.split()
        if len(words) >= 2 and words[-1] in TLDS and all(re.fullmatch(r"[a-z0-9-]+", w) for w in words):
            addr = "".join(words[:-1]) + "." + words[-1]     # продиктовали адрес: «habr точка com»
            self.open_url(addr)
            return self.ack(f"Открываю {addr}", SOUND_OPEN)
        # Незнакомый сайт: «Мне повезёт» — сразу первый результат поиска
        self.open_url("https://duckduckgo.com/?q=" + urllib.parse.quote_plus("! " + target))
        self.ack(f"Ищу сайт {target}", SOUND_OPEN)

    def do_folder(self, target):
        folders = all_folders()
        sc, key = best_in_phrase(target or "", lambda s: score_table(s, folders), max_words=4)
        if not key or sc < 0.6:
            return self.fail("Не знаю такую папку")
        path = folders[key]
        subprocess.run(["open", path])
        self.push_opened(("folder", path))
        self.ack(f"Открываю {key}", SOUND_OPEN)

    def do_mkdir(self, data):
        name = (data or {}).get("name", "").strip()
        if not name:
            return self.fail("Как назвать папку?")
        name = re.sub(r"[/:]", "-", name)[:60]
        base = os.path.expanduser("~/Desktop")
        if data.get("where"):
            folders = all_folders()
            key = match_key(data["where"], folders, 0.6)
            if not key:
                return self.fail(f"Не знаю, где это: {data['where']}")
            base = folders[key]
        path = os.path.join(base, name[:1].upper() + name[1:])
        if os.path.exists(path):
            subprocess.run(["open", path])
            return self.voice.say("Такая папка уже есть, открыл её")
        try:
            os.makedirs(path)
        except OSError as e:
            print(f"⚠️  mkdir: {e}")
            return self.fail("Не получилось создать папку")
        subprocess.run(["open", path])
        self.push_opened(("folder", path))
        self.ack(f"Создал {path}", SOUND_OPEN)

    def do_file(self, target):
        """«Открой файл отчёт» — ищет через Spotlight по имени и открывает самый похожий."""
        if not target:
            return self.fail("Какой файл открыть?")
        r = subprocess.run(["mdfind", "-onlyin", os.path.expanduser("~"), "-name", target.split()[0]],
                           capture_output=True, text=True, timeout=5)
        cands = [p for p in r.stdout.splitlines()
                 if "/Library/" not in p and "/." not in p and "/.venv/" not in p and os.path.isfile(p)]
        if not cands:
            return self.fail(f"Не нашёл файл {target}")
        def score(p):
            name = os.path.splitext(os.path.basename(p))[0].lower()
            return (ratio(target, name), -len(p))
        best = max(cands[:200], key=score)
        if ratio(target, os.path.splitext(os.path.basename(best))[0].lower()) < 0.5:
            return self.fail(f"Не нашёл файл {target}")
        subprocess.run(["open", best])
        self.ack(f"Открываю {best}", SOUND_OPEN)

    def do_youtube(self, q):
        if not q:
            return self.fail("Что найти?")
        self.open_url("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(q))
        self.ack("Ищу на ютубе")

    def do_google(self, q):
        if not q:
            return self.fail("Что найти?")
        self.open_url("https://www.google.com/search?q=" + urllib.parse.quote_plus(q))
        self.ack("Ищу")

    # ── Звук и экран ──
    def do_volume(self, cmd):
        if cmd == "mute":
            osa("set volume with output muted")
            return print("🔇 звук выключен")
        if cmd == "unmute":
            osa("set volume without output muted")
            return self.voice.say("Звук включён")
        try:
            current = int(osa("output volume of (get volume settings)"))
        except ValueError:
            current = 50
        new = {"up": current + 15, "down": current - 15}.get(cmd, cmd)
        new = max(0, min(100, int(new)))
        osa(f"set volume output volume {new} without output muted")
        self.ack(f"Громкость {new}")

    def do_brightness(self, cmd):
        """Яркость встроенного экрана через системную библиотеку DisplayServices.
        Клавиши яркости через System Events на Apple Silicon не работают."""
        try:
            import ctypes
            ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
            ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
            display = cg.CGMainDisplayID()

            cur = ctypes.c_float(-1)
            ds.DisplayServicesGetBrightness(display, ctypes.byref(cur))
            if isinstance(cmd, int):
                target = max(0, min(100, cmd)) / 100
            else:
                target = cur.value + (0.15 if cmd == "up" else -0.15)
            target = max(0.0, min(1.0, target))

            ds.DisplayServicesSetBrightness(display, ctypes.c_float(target))
            after = ctypes.c_float(-1)
            ds.DisplayServicesGetBrightness(display, ctypes.byref(after))
            print(f"🔆 яркость: было {cur.value:.2f} → задано {target:.2f} → стало {after.value:.2f}")
            label = f"Яркость {round(after.value * 100)}"
            return self.ack(label)
        except Exception as e:
            print(f"⚠️  DisplayServices: {e}")
            self.fail("Не получилось поменять яркость")

    def do_lock(self, _):
        play(SOUND_OK)
        time.sleep(0.3)
        # 1. Системная функция блокировки (как «Заблокировать экран» в меню )
        try:
            import ctypes
            login = ctypes.CDLL("/System/Library/PrivateFrameworks/login.framework/Versions/Current/login")
            login.SACLockScreenImmediate()
            print("🔒 заблокировано (SACLockScreenImmediate)")
            return
        except Exception as e:
            print(f"⚠️  SACLockScreenImmediate: {e}")
        # 2. Запасной: заставка (блокирует, если в настройках включён пароль после заставки)
        r = subprocess.run(["open", "-a", "ScreenSaverEngine"], capture_output=True, text=True)
        if r.returncode == 0:
            print("🔒 запущена заставка")
            return
        print(f"⚠️  ScreenSaverEngine: {r.stderr.strip()[:200]}")
        self.fail("Не получилось заблокировать")

    def do_sleep(self, _):
        if self.ask_yes_no("Усыпить мак? Скажи да или нет"):
            self.voice.say("Спокойной ночи", wait=True)
            subprocess.run(["pmset", "sleepnow"])
        else:
            self.voice.say("Отменил")

    # ── Время ──
    def do_time(self, _):
        now = datetime.datetime.now()
        self.voice.say(f"Сейчас {now.hour} {now.minute:02d}")

    def do_date(self, _):
        now = datetime.datetime.now()
        self.voice.say(f"Сегодня {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}")

    def do_timer(self, data):
        sec = data.get("sec")
        if not sec:
            return self.fail("Не понял, на сколько таймер")
        label = human_duration(sec)

        def ring():
            for _ in range(3):
                play(SOUND_ALARM)
                time.sleep(0.8)
            self.voice.say(f"Таймер на {label} закончился")

        t = threading.Timer(sec, ring)
        t.daemon = True
        t.start()
        self.timers.append(t)
        self.voice.say(f"Таймер на {label}")

    def do_remind(self, data):
        sec, text = data.get("sec"), data.get("text") or "ты просил напомнить"
        if not sec:
            return self.fail("Не понял, через сколько напомнить")

        def remind():
            play(SOUND_ALARM)
            safe = text.replace('"', "'")
            osa(f'display notification "{safe}" with title "Абракадабра"')
            self.voice.say(f"Напоминаю: {text}")

        t = threading.Timer(sec, remind)
        t.daemon = True
        t.start()
        self.timers.append(t)
        self.voice.say(f"Напомню через {human_duration(sec)}")

    def do_cancel_timers(self, _):
        active = [t for t in self.timers if t.is_alive()]
        for t in active:
            t.cancel()
        self.timers.clear()
        self.voice.say("Таймер отменён" if active else "Таймеров нет")

    def do_voice(self, _):
        name = self.voice.next_voice()
        short = name.replace("ru_RU-", "").replace("-medium", "")
        self.voice.say(f"Голос {short}. Так лучше?")

    # ── Погода ──
    def do_weather(self, _):
        try:
            url = "https://wttr.in/?format=%t+%C&lang=ru&m"
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            raw = urllib.request.urlopen(req, timeout=6).read().decode("utf-8").strip()
        except Exception:
            return self.fail("Не получилось узнать погоду. Проверь интернет")
        m = re.match(r"([+-]?)(\d+)°C\s*(.*)", raw)
        if not m:
            return self.voice.say(raw)
        sign = {"+": "плюс ", "-": "минус "}.get(m.group(1), "")
        self.voice.say(f"Сейчас {sign}{m.group(2)} градусов, {m.group(3).lower()}")

    # ── Музыка ──
    def do_music(self, cmd):
        """Медиаклавиши macOS: управляют тем, что сейчас играет — Яндекс Музыка, VK, Spotify,
        YouTube в браузере, SoundCloud и т.д. «Пауза» и «продолжи» — одна клавиша play/pause."""
        key = {"pause": 16, "play": 16, "next track": 17, "previous track": 18}[cmd]
        try:
            send_media_key(key)
            return print(f"🎵 медиаклавиша: {cmd}")
        except Exception as e:
            print(f"⚠️  медиаклавиша: {e}")
        for app in ("Spotify", "Music"):                      # запасной путь
            if osa(f'application "{app}" is running') == "true":
                osa(f'tell application "{app}" to {cmd}')
                return print(f"🎵 {app}: {cmd}")
        self.fail("Не получилось управлять музыкой")

    # ── Прочее ──
    def do_screenshot(self, _):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H.%M.%S")
        tmp = os.path.join(tempfile.gettempdir(), f"abra-shot-{stamp}.png")
        # Снимаем сразу, до вопроса — иначе на скриншоте будет момент разговора
        r = subprocess.run(["screencapture", "-x", tmp], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
            print(f"⚠️  screencapture: код {r.returncode}, {r.stderr.strip()[:200]}")
            return self.fail("Скриншот не получился. Нужен доступ к записи экрана")
        play(SOUND_OK)
        answer = self.ask_text("Куда сохранить?")
        folders = all_folders()
        key = match_key(answer, folders, 0.6) if answer else None
        if not key and answer:
            sc, key = best_in_phrase(answer, lambda s: score_table(s, folders))
            key = key if sc >= 0.7 else None
        base = folders[key] if key else os.path.expanduser("~/Desktop")
        dest = os.path.join(base, f"Скриншот {stamp}.png")
        try:
            import shutil
            shutil.move(tmp, dest)
        except Exception as e:
            print(f"⚠️  перенос скриншота: {e}")
            return self.fail("Не получилось сохранить скриншот")
        self.voice.say(f"Сохранил в {key or 'рабочий стол'}")
        print(f"📸 {dest}")

    def do_type(self, text):
        """Вставляет текст в активное поле через буфер обмена (старый буфер возвращается)."""
        if not text:
            return self.fail("Что написать?")
        env = {**os.environ, "LANG": "en_US.UTF-8"}
        old = subprocess.run(["pbpaste"], capture_output=True, env=env).stdout
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), env=env)
        # key code 9 — физическая клавиша V. keystroke "v" при русской раскладке нажимал не то.
        osa('tell application "System Events" to key code 9 using command down')
        time.sleep(0.4)
        subprocess.run(["pbcopy"], input=old, env=env)
        print(f"⌨️  напечатал: {text}")

    def do_stop(self, _):
        self.voice.say("Выключаюсь", wait=True)
        os._exit(0)

# ═════════════════════════ СЛУХ ═════════════════════════


def is_wake(text: str) -> bool:
    t = text.replace(" ", "").lower()
    if not t:
        return False
    if WAKE[:8] in t:
        return True
    n = len(WAKE)
    for i in range(max(1, len(t) - n + 1)):
        if difflib.SequenceMatcher(None, t[i:i + n], WAKE).ratio() >= WAKE_THRESHOLD:
            return True
    return False


WAKE_PARTS = ["абракадабра", "абрака", "кадабра", "добра", "дабра", "обрака", "апрака", "абарка", "кадра",
              "добро", "доброго", "добрка", "обратка", "хабрака"]


def is_wake_like(w: str) -> bool:
    """Похоже на имя или его кусок, как его искажает Whisper: «абрака», «добра», «апрокадавра»."""
    if is_wake(w):
        return True
    return any(difflib.SequenceMatcher(None, w, p).ratio() >= 0.72 for p in WAKE_PARTS)


def is_wake_only(text: str) -> bool:
    """Во фразе нет ничего, кроме имени (и «привет»)?"""
    rest = [w for w in normalize(text).split() if w != "привет" and not is_wake_like(w)]
    return not rest


def rms(chunk: bytes) -> float:
    a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


class Transcriber:
    """Речь → текст. mlx-whisper (чип Apple) если есть, иначе faster-whisper."""

    def __init__(self):
        self.mlx = None
        try:
            import mlx_whisper
            self.mlx = mlx_whisper
            self.name = f"mlx ({MLX_MODEL.split('/')[-1]})"
        except ImportError:
            from faster_whisper import WhisperModel
            self.fw = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8",
                                   cpu_threads=os.cpu_count() or 4)
            self.name = f"faster-whisper ({MODEL_SIZE})"
        self(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))  # прогрев: первый вызов всегда медленный

    def __call__(self, audio: np.ndarray) -> str:
        if self.mlx:
            r = self.mlx.transcribe(audio, path_or_hf_repo=MLX_MODEL, language="ru",
                                    initial_prompt=WHISPER_PROMPT, temperature=0.0,
                                    condition_on_previous_text=False, verbose=None)
            text = r.get("text", "").strip()
        else:
            segments, _ = self.fw.transcribe(audio, language="ru", beam_size=BEAM_SIZE,
                                             vad_filter=True, initial_prompt=WHISPER_PROMPT,
                                             condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
        if any(h in text.lower() for h in HALLUCINATIONS):
            return ""
        return text


def trim_silence(chunks: list, threshold: float, keep_start: int = 2, keep_end: int = 5) -> list:
    """Обрезает тишину в начале и в конце. В конце оставляем 0.5 с запаса:
    окончания слов («замет-КИ») тихие, их нельзя срезать."""
    loud = [i for i, c in enumerate(chunks) if rms(c) > threshold]
    if not loud:
        return chunks
    return chunks[max(0, loud[0] - keep_start): loud[-1] + keep_end + 1]


def record_command(q: queue.Queue, noise_floor: float, spoke_already: bool = False,
                   wait: float | None = None) -> list:
    """Пишет команду, пока ты говоришь.
    spoke_already=True — имя распознано, пока ты его ещё договаривал. Тогда первый
    кусок речи — это хвост имени, а после него даём до NAME_PAUSE_SEC на начало команды."""
    threshold = max(noise_floor * 2.5, 300.0)
    cs = CHUNK / SAMPLE_RATE
    chunks = []
    in_tail = spoke_already      # сейчас звучит хвост имени
    spoke = spoke_already
    tail_loud = silence = waited = 0.0
    while True:
        c = q.get()
        chunks.append(c)
        if rms(c) > threshold:
            if in_tail and silence > 0:
                in_tail = False          # после паузы снова речь — это уже команда
            spoke, silence = True, 0.0
            if in_tail:
                tail_loud += cs
                if tail_loud >= 0.7:     # «хвост» слишком длинный — значит, команда без паузы
                    in_tail = False
        elif spoke:
            silence += cs
        else:
            waited += cs
        if spoke and silence >= (NAME_PAUSE_SEC if in_tail else SILENCE_END_SEC):
            break
        if not spoke and waited >= (wait or WAIT_SPEECH_SEC):
            break
        if len(chunks) * cs >= MAX_COMMAND_SEC:
            break
    return chunks


def to_audio(chunks: list) -> np.ndarray:
    return np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32) / 32768.0


def listen_yes_no(q: queue.Queue, vosk_model) -> bool | None:
    """Ждёт короткий ответ. Vosk с узким словарём ловит «да» намного надёжнее Whisper."""
    grammar = json.dumps(YES_WORDS + NO_WORDS + ["[unk]"], ensure_ascii=False)
    rec = KaldiRecognizer(vosk_model, SAMPLE_RATE, grammar)
    deadline = time.time() + YES_NO_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            data = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if rec.AcceptWaveform(data):
            text = json.loads(rec.Result()).get("text", "")
        else:
            text = json.loads(rec.PartialResult()).get("partial", "")
        words = text.split()
        if any(w in YES_WORDS for w in words):
            print(f"   ответ: «{text}» → да")
            return True
        if any(w in NO_WORDS for w in words) or "не надо" in text:
            print(f"   ответ: «{text}» → нет")
            return False
    return None


def drain(q: queue.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def main() -> None:
    SetLogLevel(-1)
    print(f"\n=== Абракадабра запускается {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print("Загружаю модели...")
    vosk_model = Model(lang="ru")
    stt = Transcriber()
    voice = Voice()
    apps = installed_apps()
    print(f"Приложений: {len(apps)}. Голос: {voice.name}. Распознавание: {stt.name}")

    rec = KaldiRecognizer(vosk_model, SAMPLE_RATE)
    q: queue.Queue = queue.Queue()
    pre_roll = collections.deque(maxlen=int(PRE_ROLL_SEC * SAMPLE_RATE / CHUNK))
    state = {"noise": 200.0}

    def ask_yes_no(question: str) -> bool:
        voice.say(question, wait=True)
        drain(q)
        answer = listen_yes_no(q, vosk_model)
        if answer is None:
            voice.say("Не услышал ответ")
            return False
        return answer

    def ask_text(question: str) -> str:
        """Задаёт вопрос и распознаёт короткий свободный ответ."""
        voice.say(question, wait=True)
        drain(q)
        chunks = record_command(q, state["noise"], False, wait=4.0)
        thr = max(state["noise"] * 2.5, 300.0)
        if sum(rms(c) > thr for c in chunks) * CHUNK / SAMPLE_RATE < MIN_SPEECH_SEC:
            return ""
        answer = normalize(stt(to_audio(trim_silence(chunks, thr))))
        print(f"   ответ: «{answer}»")
        return answer

    actions = Actions(voice, apps, ask_yes_no, ask_text)

    def callback(indata, frames, time_info, status):
        q.put(bytes(indata))

    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=CHUNK,
                           dtype="int16", channels=1, callback=callback):
        voice.say("Готова к работе")   # без своего имени, иначе услышит сама себя
        print("\n👂 Слушаю. Ctrl+C — выход.\n")
        was_talking = False
        my_file = os.path.abspath(__file__)
        my_mtime = os.path.getmtime(my_file)
        last_check = time.time()

        def handle(chunks: list, t0: float, fast: bool, thr: float) -> None:
            """Распознаёт и выполняет команду."""
            loud_sec = sum(rms(c) > thr for c in chunks) * CHUNK / SAMPLE_RATE
            if loud_sec < MIN_SPEECH_SEC:
                # щелчок или шорох — не отправляем в Whisper, иначе он что-нибудь «услышит»
                drain(q)
                pre_roll.clear()
                rec.Reset()
                if not fast:
                    play(SOUND_ERROR)
                return
            t1 = time.time()
            command = stt(to_audio(chunks))
            if not fast and is_wake_only(command):
                # В запись попало только имя: дослушиваем команду
                print(f"…  «{command}» — только имя, слушаю дальше")
                more = record_command(q, state["noise"], False, wait=FOLLOWUP_WAIT_SEC)
                if sum(rms(c) > thr for c in more) * CHUNK / SAMPLE_RATE >= MIN_SPEECH_SEC:
                    command = stt(to_audio(trim_silence(more, thr)))
                t1 = time.time()
            t2 = time.time()
            action, data_ = parse(command)
            # В быстром режиме длинные фразы — это разговор, а не команда
            # (кроме команд с «хвостом»: поиск, диктовка, напоминание)
            if fast and len(normalize(command).split()) > 6 and \
                    action not in ("youtube", "google", "type", "remind", "timer"):
                action, data_ = None, None
            mark = "⚡" if fast else "📝"
            print(f"{mark} «{command}»")
            print(f"⚙️  {action}: {data_}   ⏱ запись {t1 - t0:.1f} с, распознавание {t2 - t1:.1f} с")
            if action is None:
                if not fast:
                    if is_wake_only(command) or len(normalize(command).split()) <= 2:
                        play(SOUND_ERROR)      # имя или обрывок — молча, только звук
                    elif command:
                        actions.fail("Не понял команду")
                # в быстром режиме обычные разговоры просто игнорируются
            else:
                if fast:
                    actions.fast_until = time.time() + FAST_TIMEOUT_SEC
                actions.run(action, data_)
            drain(q)
            pre_roll.clear()
            rec.Reset()

        try:
            while True:
                # Автообновление: если abra.py изменился, выходим с кодом 3,
                # и оболочка Abrakadabra.app запускает новую версию.
                if time.time() - last_check > 1.0:
                    last_check = time.time()
                    try:
                        if os.path.getmtime(my_file) != my_mtime:
                            print("🔄 Код обновился, перезапускаюсь...")
                            voice.say("Обновляюсь", wait=True)
                            os._exit(3)
                    except OSError:
                        pass

                data = q.get()

                # Пока говорит сама — не слушает (чтобы не реагировать на свой голос)
                if voice.is_talking():
                    was_talking = True
                    continue
                if was_talking:
                    was_talking = False
                    pre_roll.clear()
                    rec.Reset()

                pre_roll.append(data)
                level = rms(data)
                # Фон учим только по тихим кускам, иначе речь «поднимает» фон
                # и помощник перестаёт замечать голос (была задержка 4 с).
                if level < state["noise"] * 2:
                    state["noise"] = 0.95 * state["noise"] + 0.05 * level
                else:
                    state["noise"] *= 1.008     # подстройка, если стало шумнее (игра, видео)
                state["noise"] = max(50.0, min(state["noise"], 3000.0))
                thr = max(state["noise"] * 2.5, 300.0)

                # ── Быстрый режим: любая фраза — команда, имя не нужно ──
                if actions.fast:
                    if time.time() > actions.fast_until:
                        actions.fast = False
                        voice.say("Выхожу из быстрого режима")
                        rec.Reset()
                        continue
                    if level <= thr:
                        continue
                    t0 = time.time()
                    chunks = list(pre_roll)[-3:] + record_command(q, state["noise"], True)
                    chunks = trim_silence(chunks, thr)
                    handle(chunks, t0, fast=True, thr=thr)
                    continue

                # ── Обычный режим: ждём «Абракадабра» ──
                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result()).get("text", "")
                else:
                    text = json.loads(rec.PartialResult()).get("partial", "")
                if not (text and is_wake(text)):
                    continue

                play(SOUND_WAKE)
                t0 = time.time()
                speaking_now = any(rms(c) > thr for c in list(pre_roll)[-3:])
                chunks = list(pre_roll) + record_command(q, state["noise"], speaking_now)
                chunks = trim_silence(chunks, thr)
                handle(chunks, t0, fast=False, thr=thr)
        except KeyboardInterrupt:
            print("\nПока!")


if __name__ == "__main__":
    main()
