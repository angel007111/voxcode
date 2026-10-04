"""Ядро голоса помощника: микрофон, слово «имя», связь с каналом voxcode, озвучка.

Используется панелью (voxcode_app.py) и фоновым режимом:
  pythonw listener.py                 — без окна (лог в listener.log)
  python listener.py --test <audio>   — проверить распознавание и «имя» на файле

Режим диктовки (по умолчанию включён):
  «имя» -> сигнал, дальше всё сказанное копится, но не выполняется.
  Пауза 2.5 с -> накопленное уходит помощнику как диктовка: законченную просьбу он выполняет сразу,
  оборванную фразу — только коротко подтверждает («Понял»).
  «всё / выполняй / поехали / нет» -> конец диктовки, помощник выполняет всё ещё не сделанное.
  40 с тишины -> диктовка закрывается без выполнения (текст у помощника уже есть).
Без режима диктовки: «<имя>, <команда>» сразу уходит помощнику, после ответа 10 с можно без «имя».
В конце ответа помощника двойной восходящий сигнал — микрофон снова слушает (в диктовке или эти 10 с).
Если помощник выключен — запускает его (voxcode-tray.ps1) и передаёт команду.

Перебивание: пока помощник говорит, микрофон слушает только «<имя>, …» (эхо колонок без этого слова
игнорируется) — речь обрывается, команда уходит как обычно. Выключается в config.json: "barge_in": false.

Команды, которые выполняются сразу, без сессии помощника:
  «<имя>, стоп / хватит / замолчи»          — замолчать, отменить диктовку
  «<имя>, пауза / жди / не слушай»          — пауза: реагирую только на «<имя>, продолжай»
  «<имя>, продолжай / слушай / старт»       — выйти из паузы
  «<имя>, выключи микрофон / стоп запись»   — закрыть микрофон совсем (включить — ▶ в панели)
"""
import json
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from voice import _add_cuda_dlls, synth, WHISPER_MODEL  # noqa: E402  (WHISPER_MODEL — запасная для CPU)
from audio_prep import prepare  # noqa: E402
from speaker_id import SpeakerID  # noqa: E402
from assistant import NAME, WAKE_CORE  # noqa: E402  (имя и слово-активатор — assistant.json, см. setup.ps1)

BASE = "http://127.0.0.1:8790"
CONFIG = HERE / "config.json"
MIC_OFF = HERE / "mic.off"          # общий с треем флаг «не слушать слово помощник»
SR = 16000
BLOCK = int(SR * 0.03)              # 30 мс
PRE_ROLL = 10                       # ~0.3 с до начала речи
END_SILENCE = 1.0
MAX_UTTERANCE = 20.0
MIN_UTTERANCE = 0.4
FOLLOW_UP = 10.0
WAKE_WAIT = 10.0                    # после одиночного «имя» столько ждём команду (без диктовки)
DICT_PAUSE = 2.5                    # пауза в диктовке, после которой фраза уходит помощнику
DICT_TIMEOUT = 40.0                 # тишина, после которой диктовка закрывается без выполнения
THINK_TIMEOUT = 60.0                # сколько максимум показывать «думаю», если ответа нет
ECHO_TAIL = 1.5                     # с после озвучки микрофон ещё глух: хвост звука (RDP отстаёт)
GAIN = 1.0                          # программное усиление микрофона (настраивается в панели)
THRESHOLD = 0.01                    # нижний порог громкости начала речи (после усиления)
LISTEN_MODEL = os.environ.get("VOXCODE_LISTEN_MODEL", "large-v3-turbo")
WAKE = re.compile(
    r"^\W*(?:(?:эй|слушай|и|а|ну|о|ой|так)\W+){0,2}(" + WAKE_CORE + r")\b\W*", re.I)
# Whisper в тишине и шуме «слышит» титры с YouTube
HALLUCINATION = re.compile(
    r"субтитр|dimatorzok|продолжение следует|спасибо за просмотр|подпис\w* на канал|редактор\w* субтитров|amara\.org",
    re.I)
# Команды сверяются целиком с нормализованной фразой (см. norm)
CMD_STOP = re.compile(r"(стоп|хватит|замолчи|замолкни|тихо|стой|отмена|отмени)( (стоп|хватит))?")
CMD_MIC_OFF = re.compile(
    r"(выключи|отключи|останови|стоп|закрой) (микрофон|запись|прослушку|прослушивание)|не записывай|стоп запись")
CMD_PAUSE = re.compile(r"(поставь на |на )?пауз[ау]|жди|подожди|спи|не слушай|перестань слушать|режим ожидания")
CMD_RESUME = re.compile(
    r"продолжай|продолжить|продолжаем|слушай|проснись|просыпайся|включись|старт|плей|play|start|я тут|работаем")
CMD_DONE = re.compile(
    r"(это )?все( (выполняй|делай|поехали|приступай))?|выполняй|приступай|поехали|делай|давай делай"
    r"|нет|нет все|нет спасибо|больше ничего|нет больше ничего|ничего")
WAKE_ANY = re.compile(r"\b(" + WAKE_CORE + r")\b", re.I)  # имя помощника в любом месте фразы
VOICE_MARGIN = 0.1                  # насколько звук должен быть ближе к владельцу, чем к голосу помощника (config: voice_margin)
VOICE_SURE = 0.2                    # разница, при которой хватает одной проверки
VOICE_THRESHOLD = 0.15              # похожесть на голос владельца, с которой помощник замолкает (config: voice_threshold)
OWNER_WAIT = 8.0                    # с: перебил, но так ничего и не сказал — помощник договаривает
# Реплики-заполнители по смыслу фразы владельца (первое совпадение сверху), иначе — общие
FILLER_RULES = [
    (re.compile(r"браузер|сайт|страниц|хром", re.I), ["Давай посмотрим в браузере.", "Открываю браузер."]),
    (re.compile(r"запиш|запомни|добав|заметк|напомни", re.I), ["Записываю.", "Так, записываю."]),
    (re.compile(r"почт|письм|календар|встреч|лимит|задач|провер|посмотри|глянь|найди|покажи", re.I),
     ["Сейчас посмотрим.", "Сейчас гляну.", "Секунду, смотрю."]),
    (re.compile(r"\b(сделай|выполн|делай|запусти|открой|закрой|исправ|почини|перезапуст|продолжай|давай)", re.I),
     ["Сейчас сделаю.", "Начинаю.", "Принял, делаю."]),
    (re.compile(r"\?|\b(как|почему|зачем|что|когда|сколько|может)\b", re.I), ["Хм, сейчас подумаю.", "Так, секунду.", "Хороший вопрос."]),
]
FILLERS_ANY = ["Угу.", "Так…", "Секунду.", "Ага."]
# Ответ на одно «имя»: после долгого перерыва — приветствие по времени суток, иначе коротко
ACK_GREET = {"morning": ["Доброе утро! Как спалось?", "Доброе утро, слушаю."],
             "day": ["Привет! Слушаю.", "Привет, как дела?"],
             "evening": ["Добрый вечер! Слушаю.", "Привет! Как прошёл день?"],
             "night": ["Не спится? Слушаю.", "Тут я. Что случилось?"]}
ACK_SHORT = ["Да?", "Слушаю.", "Тут.", "Да, слушаю.", "М?"]
GREET_GAP = 3 * 3600                # с без разговора — дальше «имя» встречаю приветствием
FILLERS = sorted({t for _, ts in FILLER_RULES for t in ts} | set(FILLERS_ANY) | set(ACK_SHORT)
                 | {t for ts in ACK_GREET.values() for t in ts})
FILLER_DELAY = 1.2                  # с: ответа помощника ещё нет — короткая реплика, чтобы не молчать
FILLER_SOURCES = {"voice", "dictation", "dictation_end", "choice"}  # только на сказанное голосом
BARGE_MAX = 3.0                     # с: куски речи во время озвучки — короткие, чтобы «имя» был в начале
DONE_TAIL = re.compile(r"[\s,.!?…-]*\b(выполняй|приступай|поехали)\W*$", re.I)

log = logging.getLogger("voxcode")

_TR = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                     "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                     "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh",
                     "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", "x": "ks", "w": "v", "q": "k"})


def translit(s):
    """Грубая латинизация для сравнения слов на слух (не для показа)."""
    return s.translate(_TR)


def norm(text):
    """«Ну, стоп!» -> «стоп»: нижний регистр, без пунктуации и вежливых хвостов."""
    t = re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е"))
    t = " ".join(t.split())
    t = re.sub(r"^(ну|давай|так|а|и) ", "", t)
    return re.sub(r" (пожалуйста|плиз)$", "", t)


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def token():
    f = HERE / ".token"
    return f.read_text().strip() if f.exists() else ""


def audio_devices():
    """Устройства MME (без дублей WASAPI/WDM-KS): {'inputs': [...], 'outputs': [...]}"""
    res = {"inputs": [], "outputs": []}
    for d in sd.query_devices():
        if d["hostapi"] != 0 or "Sound Mapper" in d["name"]:
            continue
        if d["max_input_channels"] > 0:
            res["inputs"].append(d["name"])
        if d["max_output_channels"] > 0:
            res["outputs"].append(d["name"])
    return res


def device_index(name, kind):
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] == 0 and d["name"] == name and d[f"max_{kind}_channels"] > 0:
            return i
    return None


class Core:
    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda ev: None)
        self.cfg = load_config()
        self.model = None
        self.state = "loading"
        self.speaking = threading.Event()
        self.audio_lock = threading.Lock()  # колонки: писк и речь по очереди (см. play)
        self.mic_event = threading.Event()  # будит run(), когда микрофон снова нужен
        self.lock = threading.Lock()        # dict_buf/dictating: поток микрофона и таймер диктовки
        self.deaf_until = 0.0               # до этого момента микрофон не слушаем (эхо своей речи)
        self.follow_until = 0.0
        self.awaiting_until = 0.0
        self.thinking_until = 0.0
        self.ptt = False
        self.ptt_buf = []
        self.level = 0.0
        self.restart_stream = False
        self.online = False
        self.standby = False                # «<имя>, пауза»: жду только «<имя>, продолжай»
        self.calib = None                   # список RMS во время калибровки
        self.in_speech = False
        self.dictating = False
        self.dict_buf = []
        self.last_heard = 0.0
        self.barge_scores = []
        self.say_busy = False               # идёт ответ помощника (say) — реплика-заполнитель не нужна
        self.tts_emb = None                 # отпечаток голоса помощника (TTS) — чтобы отличать эхо от владельца
        self.last_talk = self._read_last_talk()  # когда владелец последний раз что-то говорил помощнику
        self.last_filler = None
        self.owner_pause = threading.Event()   # владелец заговорил во время речи — пауза
        self.pause_done = threading.Event()    # мик разобрал его фразу: решение в pause_resume
        self.pause_resume = True
        self.spk = SpeakerID(on_ready=lambda: self.emit(
            type="info", text=f"Запомнил твой голос — теперь можно перебивать меня без «{NAME}»"))

    # ---------- события ----------
    def emit(self, **ev):
        try:
            self.on_event(ev)
        except Exception:
            pass

    def set_state(self, s):
        if s != self.state:
            self.state = s
            self.emit(type="state", state=s)

    def idle_state(self):
        if not self.mic_on:
            return "micoff"
        if not self.online:
            return "offline"
        if self.standby:
            return "paused"
        if time.time() < self.thinking_until:
            return "thinking"
        if self.dictating:
            return "dictating"
        if time.time() < self.awaiting_until:
            return "wake"
        return "idle"

    # ---------- настройки ----------
    @property
    def wake_enabled(self):
        return not MIC_OFF.exists()

    def set_wake(self, on):
        if on:
            MIC_OFF.unlink(missing_ok=True)
        else:
            MIC_OFF.touch()
        self.emit(type="settings", **self.settings())

    @property
    def muted(self):
        return bool(self.cfg.get("muted"))

    def set_muted(self, on):
        self.cfg["muted"] = bool(on)
        save_config(self.cfg)
        if on:
            self.stop_speaking()
        self.emit(type="settings", **self.settings())

    @property
    def mic_on(self):
        return self.cfg.get("mic_on", True)

    def set_mic(self, on):
        """Полностью открыть/закрыть микрофон (кнопка ▶/⏸)."""
        self.cfg["mic_on"] = bool(on)
        save_config(self.cfg)
        if on:
            self.standby = False
            self.mic_event.set()
        else:
            self.cancel_dictation()
        log.info("микрофон %s", "включён" if on else "выключен")
        self.emit(type="settings", **self.settings())
        self.set_state(self.idle_state())

    @property
    def gain(self):
        return float(self.cfg.get("gain", GAIN))

    @property
    def threshold(self):
        return float(self.cfg.get("threshold", THRESHOLD))

    def set_sensitivity(self, gain=None, threshold=None):
        if gain is not None:
            self.cfg["gain"] = round(min(30.0, max(1.0, float(gain))), 2)
        if threshold is not None:
            self.cfg["threshold"] = round(min(0.08, max(0.002, float(threshold))), 4)
        save_config(self.cfg)
        self.emit(type="settings", **self.settings())

    @property
    def dictation_mode(self):
        return self.cfg.get("dictation", True)

    def set_dictation(self, on):
        self.cfg["dictation"] = bool(on)
        save_config(self.cfg)
        if not on:
            self.cancel_dictation()
        self.emit(type="settings", **self.settings())

    def set_device(self, kind, name):
        self.cfg[f"{kind}_device"] = name or None
        save_config(self.cfg)
        if kind == "input":
            self.restart_stream = True
        self.emit(type="settings", **self.settings())

    def settings(self):
        return {
            "wake": self.wake_enabled, "muted": self.muted, "mic_on": self.mic_on,
            "gain": self.gain, "threshold": self.threshold, "dictation": self.dictation_mode,
            "input_device": self.cfg.get("input_device"), "output_device": self.cfg.get("output_device"),
            "devices": audio_devices(),
        }

    # ---------- распознавание ----------
    def load_model(self):
        _add_cuda_dlls()
        from faster_whisper import WhisperModel
        try:
            self.model = WhisperModel(LISTEN_MODEL, device="cuda", compute_type="float16")
        except Exception as e:
            log.warning("CUDA недоступна (%s), работаю на CPU с моделью %s", e, WHISPER_MODEL)
            self.model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    def transcribe(self, audio):
        audio = prepare(audio, do_denoise=self.cfg.get("denoise", True))
        segments, _ = self.model.transcribe(
            audio, language="ru", beam_size=5, vad_filter=True,
            hotwords=NAME, condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if HALLUCINATION.search(text):
            log.info("отброшено (галлюцинация Whisper): %s", text)
            return ""
        return text

    # ---------- звук ----------
    def _out(self):
        return device_index(self.cfg.get("output_device"), "output")

    def play(self, snd, sr=24000):
        """Все звуки — по одному: sd.play у sounddevice общий, и наложение писка на речь
        вешало sd.wait() одного из потоков (а с ним и озвучку следующих ответов)."""
        with self.audio_lock:
            sd.play(np.asarray(snd, dtype=np.float32), sr, device=self._out())
            sd.wait()

    @staticmethod
    def tone(freq, dur, sr=24000):
        t = np.linspace(0, dur, int(sr * dur), False)
        # громко и с плоской вершиной: Ханнинг на всю длину съедал половину энергии — писк
        # был почти не слышен. Края 15 мс сглажены, чтобы не щёлкало.
        env = np.ones(len(t))
        fade = min(int(sr * 0.015), len(t) // 2)
        env[:fade], env[len(t) - fade:] = np.linspace(0, 1, fade), np.linspace(1, 0, fade)
        return 0.7 * np.sin(2 * np.pi * freq * t) * env

    def beep(self, freq=880, dur=0.12, freq2=None):
        """Писк; с freq2 — двойной восходящий одним звуком (два потока RDP обрезал бы дважды).
        Через RDP («Удаленное аудио») начало потока съедается — короткий писк пропадал целиком,
        поэтому впереди 0.3 с тишины, которую не жалко потерять."""
        if self.muted:
            return
        parts = [np.zeros(int(24000 * 0.3)), self.tone(freq, dur)]
        if freq2:
            parts += [np.zeros(int(24000 * 0.04)), self.tone(freq2, dur * 1.3)]
        try:
            self.play(np.concatenate(parts))
        except Exception as e:
            log.error("сигнал: %s", e)

    def ready_tail(self, sr=24000):
        """Сигнал «слушаю» (двойной восходящий) — приклеивается в конец речи тем же потоком:
        отдельный короткий звук RDP («Удаленное аудио») съедает целиком."""
        return np.concatenate([np.zeros(int(sr * 0.35)), self.tone(660, 0.14, sr),
                               np.zeros(int(sr * 0.04)), self.tone(990, 0.18, sr)])

    def say(self, text, ready=False):
        """Озвучить text; во время речи шлёт уровни для «пульса».
        ready — ответ помощника: в конце сигнал «слушаю», если после него микрофон ждёт без «имя»."""
        from faster_whisper import decode_audio

        if self.muted:
            return
        self.say_busy = True
        interrupted = False  # владелец перебил и сказал своё — хвост эха не ждём
        self.barge_scores = []  # похожесть звука на голос владельца во время этой речи (для настройки порога)
        mp3 = HERE / "out" / "speak.mp3"
        mp3.parent.mkdir(exist_ok=True)
        self.speaking_text = text
        self.speaking.set()
        self.set_state("speaking")
        try:
            synth(text, mp3, log=log.info)  # с повторами и запасным голосом
            audio = decode_audio(str(mp3), sampling_rate=24000)
            self.update_tts_print(mp3)
            step = 24000 // 20  # 50 мс
            env = [float(np.sqrt((audio[i:i + step] ** 2).mean())) for i in range(0, len(audio), step)]
            peak = max(env) or 1.0
            ready = ready and self.mic_on and self.wake_enabled and not self.standby
            if ready:
                log.info("сигнал: слушаю")
                # громкость сигнала — по пику речи: так он не тише и не резче голоса
                level = float(np.abs(audio).max()) or 0.7
                audio = np.concatenate([audio, self.ready_tail() * (level / 0.7)]).astype(np.float32)
            with self.audio_lock:
                pos = 0  # с какого сэмпла играем (после паузы — продолжение)
                while True:
                    self.owner_pause.clear()
                    sd.play(audio[pos:], 24000, device=self._out())
                    t0, first, paused = time.time(), pos // step, False
                    for i in range(first, len(env)):
                        if not self.speaking.is_set():
                            break
                        if self.owner_pause.is_set():
                            paused = True
                            break
                        self.emit(type="pulse", v=round(env[i] / peak, 3))
                        time.sleep(max(0.0, t0 + (i - first + 1) * 0.05 - time.time()))
                    if not paused:
                        sd.wait()
                        break
                    # владелец заговорил: пауза, пока он не договорит
                    sd.stop()
                    pos = max(0, pos + int((time.time() - t0) * 24000) - 24000 // 2)  # с полсекунды назад
                    self.emit(type="pulse", v=0)
                    self.speaking.clear()
                    self.set_state("recording")
                    # колонки на паузе свободны: иначе писк «диктовка» из потока микрофона
                    # ждал бы этот замок, а микрофон — писк (и решение приходило бы только по таймауту)
                    self.audio_lock.release()
                    try:
                        resume = self.pause_resume if self.pause_done.wait(OWNER_WAIT) else True
                    finally:
                        self.audio_lock.acquire()
                    self.pause_done.clear()
                    if not resume:
                        interrupted = True
                        break
                    log.info("перебивание: ничего не сказал — договариваю")
                    self.speaking.set()
                    self.set_state("speaking")
        except Exception as e:
            log.error("озвучка: %s", e)
        finally:
            self.emit(type="pulse", v=0)
            if self.barge_scores:
                sc = sorted(self.barge_scores)
                log.info("голос при озвучке: проверок %d, (владелец − помощник) медиана %.2f, топ %s",
                         len(sc), sc[len(sc) // 2], " ".join(f"{x:.2f}" for x in sc[-5:]))
            # после сигнала «слушаю» речь уже отзвучала — хвост короче, чтобы не съесть начало фразы
            tail = 0 if interrupted else 0.7 if ready else float(self.cfg.get("echo_tail", ECHO_TAIL))
            self.deaf_until = time.time() + tail
            self.last_heard = self.deaf_until  # пауза диктовки считается от конца речи помощника
            self.speaking.clear()
            self.say_busy = False
            self.set_state(self.idle_state())

    def barge(self, text):
        """Речь во время озвучки: «<имя>, …» обрывает помощника и уходит как обычная команда, остальное — эхо."""
        m = WAKE_ANY.search(text or "")
        if not m:
            log.info("при озвучке (не «имя»): %s", text)
            return
        if WAKE_ANY.search(getattr(self, "speaking_text", "")):
            return  # помощник сам произносит своё имя — это эхо, а не владелец
        log.info("перебили: %s", text)
        self.stop_speaking()
        self.handle(text[m.start():])

    def update_tts_print(self, mp3):
        """Отпечаток голоса помощника — по только что озвученному (первые 8 с), сглаженно."""
        if self.spk.model is None:
            return
        try:
            from faster_whisper import decode_audio
            e = self.spk.embed(decode_audio(str(mp3), sampling_rate=16000)[:16000 * 8])
        except Exception as ex:
            log.error("отпечаток голоса помощника: %s", ex)
            return
        m = e if self.tts_emb is None else 0.7 * self.tts_emb + 0.3 * e
        self.tts_emb = m / (np.linalg.norm(m) or 1.0)

    def is_echo(self, text):
        """Фраза — это мои же слова из колонок (большинство слов есть в том, что помощник сейчас говорит)."""
        # латиница и кириллица сравниваются в одной записи: «VoxCode» в речи ≈ «вокс-код» в распознанном
        words = re.findall(r"\w+", translit(text.lower()))
        said = set(re.findall(r"\w+", translit(getattr(self, "speaking_text", "").lower())))
        # слово могло оборваться («до…» из «добрый») или распознаться по-другому («voks» из «voxcode») —
        # сравниваем и по началу
        hit = sum(any(s == w or (len(w) >= 2 and s.startswith(w)) or (len(w) >= 4 and s[:4] == w[:4])
                      for s in said) for w in words)
        return bool(words) and hit >= max(1, len(words) * 0.6)

    # ---------- реплики-заполнители ----------
    def prepare_fillers(self):
        """Озвучить FILLERS заранее (тем же голосом), чтобы реплика звучала сразу, без сети."""
        d = HERE / "out" / "fillers"
        d.mkdir(parents=True, exist_ok=True)
        for t in FILLERS:
            f = self.filler_file(t)
            if not f.exists():
                try:
                    synth(t, f)
                except Exception as e:
                    log.error("заполнитель «%s»: %s", t, e)

    @staticmethod
    def filler_file(text):
        import hashlib
        return HERE / "out" / "fillers" / (hashlib.md5(text.encode()).hexdigest()[:10] + ".mp3")

    def filler_later(self, sent_at, said):
        """Через FILLER_DELAY, если помощник ещё молчит, — короткое «угу / секунду»."""
        time.sleep(FILLER_DELAY)
        if (self.muted or self.say_busy or self.speaking.is_set() or self.state != "thinking"
                or not self.cfg.get("fillers", True) or self.thinking_until < sent_at):
            return
        from faster_whisper import decode_audio
        import random
        texts = next((ts for rx, ts in FILLER_RULES if rx.search(said)), FILLERS_ANY)
        texts = [t for t in texts if t != self.last_filler and self.filler_file(t).exists()] or             [t for t in FILLERS_ANY if self.filler_file(t).exists()]
        if not texts:
            return
        self.last_filler = random.choice(texts)
        self.play_phrase(self.last_filler)

    def play_phrase(self, text):
        """Сыграть заранее озвученную фразу (out/fillers). Нет файла — False."""
        from faster_whisper import decode_audio
        f = self.filler_file(text)
        if self.muted or not f.exists():
            return False
        try:
            audio = decode_audio(str(f), sampling_rate=24000)
        except Exception:
            return False
        if self.tts_emb is None:
            self.update_tts_print(f)
        self.speaking_text = text
        self.speaking.set()
        try:
            self.play(audio)
        finally:
            if not self.say_busy:  # ответ помощника уже начался — его флаг не трогаем
                self.deaf_until = time.time() + 0.5
                self.speaking.clear()
        return True

    @staticmethod
    def _read_last_talk():
        try:
            return float((HERE / "out" / "last_talk").read_text())
        except (OSError, ValueError):
            return 0.0

    def touch_talk(self):
        """Запомнить время разговора (в файле — чтобы перезапуск панели не считался перерывом)."""
        self.last_talk = time.time()
        try:
            (HERE / "out" / "last_talk").write_text(str(self.last_talk))
        except OSError:
            pass

    def ack(self):
        """Ответ на одно «имя»: приветствие по времени суток после долгого перерыва, иначе «Да?»."""
        import random
        now = time.time()
        if now - self.last_talk > GREET_GAP:
            h = time.localtime().tm_hour
            part = "night" if h < 5 else "morning" if h < 12 else "day" if h < 18 else "evening"
            text = random.choice(ACK_GREET[part])
        else:
            text = random.choice([t for t in ACK_SHORT if t != self.last_filler] or ACK_SHORT)
        self.last_filler = text
        self.touch_talk()
        log.info("на «имя»: %s", text)
        threading.Thread(target=lambda: self.play_phrase(text) or self.beep(), daemon=True).start()

    def say_async(self, text):
        threading.Thread(target=self.say, args=(text,), daemon=True).start()

    def stop_speaking(self):
        self.speaking.clear()
        self.pause_resume = False  # если речь на паузе — не продолжать
        self.pause_done.set()
        try:
            sd.stop()
        except Exception:
            pass

    # ---------- канал ----------
    def channel_up(self):
        try:
            return requests.get(BASE + "/ping", headers={"X-VoxCode-Token": token()}, timeout=2).ok
        except requests.RequestException:
            return False

    def start_session(self):
        log.info("сессия не запущена — запускаю")
        self.emit(type="info", text=f"{NAME} выключен — запускаю…")
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
             "-File", str(HERE.parent / "voxcode-tray.ps1"), "-Minimized"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def send(self, text, source="voice", show=True):
        """Передать команду помощнику (в отдельном потоке)."""
        self.touch_talk()
        if show:
            self.emit(type="you", text=text, source=source)
        threading.Thread(target=self._send, args=(text, source), daemon=True).start()

    def _send(self, text, source):
        if not self.channel_up():
            self.start_session()
            self.say_async("Запускаюсь, секунду.")
            for _ in range(90):
                time.sleep(1)
                if self.channel_up():
                    break
            else:
                self.emit(type="info", text="Не удалось запустить помощника — загляни в его окно")
                self.say("Не получилось запуститься. Загляни в окно помощника.")
                return
        try:
            r = requests.post(BASE + "/say", data=text.encode("utf-8"),
                              headers={"X-VoxCode-Token": token(), "X-VoxCode-Source": source}, timeout=10)
            log.info("-> %s (%s): %s [%s]", NAME, source, text, r.status_code)
            self.thinking_until = time.time() + THINK_TIMEOUT
            self.set_state("thinking")
            if source in FILLER_SOURCES:
                threading.Thread(target=self.filler_later, args=(time.time() - 0.01, text), daemon=True).start()
        except requests.RequestException as e:
            self.emit(type="info", text=f"Не отправилось: {e}")

    def events_loop(self):
        while True:
            try:
                with requests.get(BASE + "/events", headers={"X-VoxCode-Token": token()},
                                  stream=True, timeout=(3, None)) as r:
                    if not r.ok:
                        raise requests.RequestException(r.status_code)
                    r.encoding = "utf-8"  # text/event-stream без charset -> requests берёт Latin-1
                    log.info("подключился к каналу voxcode")
                    self.online = True
                    self.set_state(self.idle_state())
                    for line in r.iter_lines(decode_unicode=True):
                        if line and line.startswith("data: "):
                            ev = json.loads(line[6:])
                            if ev.get("type") == "say":
                                log.info("<- %s: %s", NAME, ev["text"])
                                self.thinking_until = 0
                                self.emit(type="bot", text=ev["text"], details=ev.get("details") or "",
                                          options=ev.get("options") or [])
                                self.say(ev["text"], ready=True)
                                if not self.dictating:
                                    self.follow_until = time.time() + FOLLOW_UP
                                self.set_state(self.idle_state())
            except requests.RequestException:
                pass
            if self.online:
                self.online = False
                self.set_state(self.idle_state())
            time.sleep(3)

    # ---------- push-to-talk ----------
    def ptt_start(self):
        self.stop_speaking()
        self.ptt_buf = []
        self.ptt = True
        self.mic_event.set()  # если микрофон выключен — откроется на время записи
        self.set_state("recording")

    def ptt_stop(self):
        if not self.ptt:
            return
        self.ptt = False
        buf, self.ptt_buf = self.ptt_buf, []
        if len(buf) * 0.03 < MIN_UTTERANCE:
            self.set_state(self.idle_state())
            return

        def work():
            text = self.transcribe(np.concatenate(buf))
            log.info("услышал (кнопка): %s", text)
            if text:
                m = WAKE.match(text)
                self.send(text[m.end():].strip() if m and text[m.end():].strip() else text, "voice")
            else:
                self.emit(type="info", text="Не расслышал")
                self.set_state(self.idle_state())
        threading.Thread(target=work, daemon=True).start()

    # ---------- калибровка ----------
    def calibrate(self):
        if self.calib is not None:
            return
        if not self.mic_on:
            self.emit(type="calib", step="fail", text="Сначала включи микрофон (▶)")
            return
        threading.Thread(target=self._calibrate, daemon=True).start()

    def _calibrate(self):
        self.stop_speaking()
        self.emit(type="calib", step="noise", text="Помолчи 3 секунды…")
        self.calib = []
        time.sleep(3)
        noise = self.calib[5:]
        self.calib = []
        self.emit(type="calib", step="speech", text=f"Теперь скажи обычным голосом: «{NAME}, какие у меня задачи на сегодня»")
        time.sleep(4.5)
        speech, self.calib = self.calib, None
        n = float(np.median(noise)) if noise else 0.001
        s = float(np.percentile(speech, 90)) if speech else 0.0
        log.info("калибровка: шум %.4f, речь %.4f", n, s)
        if s < max(n * 2.5, 0.0005):
            self.emit(type="calib", step="fail",
                      text="Речь почти не слышна на фоне шума — проверь, тот ли микрофон выбран")
            return
        gain = min(30.0, max(1.0, 0.12 / s))
        thr = min(max(n * gain * 3, 0.004), s * gain * 0.35)
        self.set_sensitivity(gain, thr)
        self.emit(type="calib", step="done",
                  text=f"Готово: усиление ×{self.gain:g}, порог {self.threshold * 1000:.0f}")

    # ---------- обработка фраз ----------
    def local_command(self, command):
        """Команды, которые выполняем сами, без сессии помощника. True — команда съедена."""
        c = norm(command)
        if not c:
            return False
        if CMD_STOP.fullmatch(c):
            log.info("команда: стоп")
            self.stop_speaking()
            self.follow_until = self.awaiting_until = 0
            if self.cancel_dictation():
                self.emit(type="info", text="Диктовку остановил, ничего не выполнял")
            self.beep(440, 0.08)
            self.set_state(self.idle_state())
            return True
        if CMD_MIC_OFF.fullmatch(c):
            log.info("команда: выключить микрофон")

            def off():
                self.say("Выключаю микрофон. Включить можно кнопкой в панели.")
                self.set_mic(False)
            threading.Thread(target=off, daemon=True).start()
            return True
        if CMD_PAUSE.fullmatch(c):
            log.info("команда: пауза")
            self.cancel_dictation()
            self.standby = True
            self.follow_until = self.awaiting_until = 0
            self.say_async(f"Пауза. Скажи «{NAME}, продолжай», когда понадоблюсь.")
            return True
        if self.standby and CMD_RESUME.fullmatch(c):
            log.info("команда: продолжить")
            self.standby = False
            self.say_async("Слушаю.")
            return True
        return False

    def handle(self, text):
        """Разобрать фразу с микрофона. False — фраза ушла мимо (без «имя» или на паузе)."""
        now = time.time()
        if not text:
            return False
        m = WAKE.match(text)
        command = text[m.end():].strip() if m else text
        if not (m or self.dictating or now < self.awaiting_until or now < self.follow_until):
            log.debug("мимо: %s", text)
            return False
        if m and not command:  # просто «имя»
            if self.dictation_mode and not self.standby:
                self.dictate("")
            elif not self.dictating:
                self.awaiting_until = now + WAKE_WAIT
                self.set_state("wake")
                self.ack()
            return True
        if self.local_command(command):
            return True
        if self.standby:
            log.info("пауза, мимо: %s", command)
            return False
        if self.dictation_mode:
            self.dictate(command)
            return True
        self.awaiting_until = self.follow_until = 0
        self.beep(660, 0.08)
        self.send(command, "voice")
        return True

    # ---------- диктовка ----------
    def dictate(self, text):
        self.awaiting_until = self.follow_until = 0
        with self.lock:
            started = not self.dictating
            self.dictating = True
            self.last_heard = time.time()
            done = bool(text) and CMD_DONE.fullmatch(norm(text))
            tail = None if done else DONE_TAIL.search(text)
            if tail:
                text = text[:tail.start()].strip()
            if text and not done:
                self.dict_buf.append(text)
        if text and not done:
            self.emit(type="you", text=text, source="dictation")
        if started:
            log.info("диктовка: начало")
            self.emit(type="info", text="Говори. Законченную просьбу выполню после паузы, длинную — по «выполняй».")
            if text or done:
                self.beep()
            else:
                self.ack()  # просто «имя» — ответить голосом, а не только писком
        if done or tail:
            self.finish_dictation(text if done else "")
        else:
            self.set_state(self.idle_state())

    def finish_dictation(self, last_phrase=""):
        """Конец диктовки: помощник получает остаток и сигнал «выполняй»."""
        with self.lock:
            buf, self.dict_buf = self.dict_buf, []
            self.dictating = False
        log.info("диктовка: выполнить")
        self.beep(660, 0.08)
        self.emit(type="you", text=last_phrase or "выполняй", source="dictation_end")
        self.send(" ".join(buf + ([last_phrase] if last_phrase else [])) or "всё", "dictation_end", show=False)

    def cancel_dictation(self):
        with self.lock:
            was = self.dictating
            if self.dict_buf:
                log.info("диктовка отменена, не отправлено: %s", " ".join(self.dict_buf))
            self.dict_buf, self.dictating = [], False
        return was

    def dictation_loop(self):
        """Пауза 5 с — отдаём накопленное помощнику на уточнение; долгая тишина — закрываем диктовку."""
        while True:
            time.sleep(0.25)
            if not self.dictating or self.in_speech or self.speaking.is_set() or self.ptt:
                continue
            now = time.time()
            idle = now - self.last_heard
            chunk, closed = None, False
            with self.lock:
                if not self.dictating:
                    continue
                if self.dict_buf and idle >= DICT_PAUSE:
                    chunk, self.dict_buf = " ".join(self.dict_buf), []
                    self.last_heard = now
                elif not self.dict_buf and idle >= DICT_TIMEOUT and now >= self.thinking_until:
                    self.dictating, closed = False, True
            if chunk:
                self.send(chunk, "dictation", show=False)
            elif closed:
                log.info("диктовка: закрыта по тишине")
                self.emit(type="info", text="Диктовка закрыта по тишине — ничего не выполнял")
                self.beep(440, 0.08)
                self.set_state(self.idle_state())

    # ---------- микрофон ----------
    def listen(self):
        q = queue.Queue()

        def cb(indata, frames, t, status):
            # Пока помощник говорит (и чуть после), звук выбрасываем прямо при захвате,
            # иначе блоки копятся в очереди и разбираются уже после речи как живые.
            raw = indata[:, 0]
            if not self.ptt and self.speaking.is_set() and self.cfg.get("barge_in", True):
                self.level = float(np.sqrt((np.clip(raw * self.gain, -1.0, 1.0) ** 2).mean()))
                q.put(("barge", raw.copy()))  # перебивание: слушаем только «<имя>, …»
                return
            if not self.ptt and (self.speaking.is_set() or time.time() < self.deaf_until):
                self.level = 0.0
                q.put(None)
                return
            if self.calib is not None:
                self.calib.append(float(np.sqrt((raw ** 2).mean())))
            # Усиление — только для индикатора и порога речи. В Whisper идёт исходный звук:
            # с усилением ×10 громкая речь обрезалась бы и искажалась (см. audio_prep).
            self.level = float(np.sqrt((np.clip(raw * self.gain, -1.0, 1.0) ** 2).mean()))
            q.put(raw.copy())  # indata — буфер sounddevice, он переиспользуется

        dev = device_index(self.cfg.get("input_device"), "input")
        noise = 0.005
        pre, buf, in_speech, silence = [], [], False, 0.0
        barge = False  # сейчас разбираем речь во время озвучки
        owner = False  # владелец перебил голосом: дослушиваем его фразу, помощник на паузе
        roll, hop = [], 0  # последние ~1.5 с во время озвучки — для узнавания голоса
        prev_ok = False  # прошлая проверка тоже была «похоже на владельца»
        with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=BLOCK,
                            callback=cb, device=dev):
            name = sd.query_devices(dev if dev is not None else sd.default.device[0])["name"]
            log.info("слушаю микрофон: %s", name)
            self.emit(type="mic", device=name, ok=True)
            self.set_state(self.idle_state())
            while not self.restart_stream and (self.mic_on or self.ptt):
                try:
                    block = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if block is None:  # захват заглушён (хвост эха после речи помощника)
                    pre, buf, in_speech = [], [], False
                    continue
                is_barge = isinstance(block, tuple)
                if is_barge:
                    block = block[1]
                    if owner:
                        is_barge = False  # помощник ещё не успел замолчать — это уже фраза владельца
                if is_barge != barge:  # озвучка началась/кончилась — начинаем кусок заново
                    pre, buf, in_speech, barge, roll, hop, prev_ok = [], [], False, is_barge, [], 0, False
                if self.ptt:
                    self.ptt_buf.append(block)
                    continue
                if not self.wake_enabled or not self.mic_on or self.calib is not None:
                    pre, buf, in_speech = [], [], False
                    continue
                rms = float(np.sqrt((block ** 2).mean())) * self.gain
                thr = max(noise * 3, self.threshold)
                if barge and self.spk.ready:
                    # узнаём голос владельца каждые ~0.5 с по последним 1.5 с
                    roll = (roll + [block])[-50:]
                    hop += 1
                    if hop >= 16 and len(roll) >= 40:
                        hop = 0
                        win = np.concatenate(roll)
                        if float(np.sqrt((win ** 2).mean())) * self.gain > thr:
                            # эхо колонок похоже на голос помощника сильнее, чем на владельца; владелец — наоборот
                            sc, zs = self.spk.compare(win, self.tts_emb)
                            if sc is not None:
                                self.barge_scores.append(sc - zs)
                            ok = (sc is not None and sc >= float(self.cfg.get("voice_threshold", VOICE_THRESHOLD))
                                  and sc - zs >= float(self.cfg.get("voice_margin", VOICE_MARGIN)))
                            # уверенно (разница ≥ 0.2) — сразу; на грани — только две проверки подряд (~0.5 с):
                            # одиночные всплески эха в начале фразы так не срабатывают
                            sure, prev_ok = ok and (sc - zs >= VOICE_SURE or prev_ok), ok
                            if sure:
                                log.info("перебил голосом (владелец %.2f, помощник %.2f) — пауза", sc, zs)
                                owner, barge = True, False
                                self.follow_until = time.time() + FOLLOW_UP  # без «имя»
                                self.pause_done.clear()
                                self.owner_pause.set()
                                pre, buf, in_speech, silence = [], roll[-33:], True, 0.0  # последняя ~1 с — начало фразы
                                roll = []
                                continue
                    continue  # без «имя» и без голоса владельца — эхо, слова не разбираем
                if not in_speech:
                    if not barge:  # эхо колонок шум не меняет
                        noise = 0.95 * noise + 0.05 * rms if rms < thr else noise
                    pre = (pre + [block])[-PRE_ROLL:]
                    if rms > thr:
                        in_speech, buf, silence = True, pre[:], 0.0
                else:
                    buf.append(block)
                    silence = silence + 0.03 if rms < thr else 0.0
                    length = len(buf) * 0.03
                    if silence >= END_SILENCE or length >= (BARGE_MAX if barge else MAX_UTTERANCE):
                        in_speech, pre = False, []
                        if barge:
                            if length - silence >= MIN_UTTERANCE:
                                self.barge(self.transcribe(np.concatenate(buf)))
                        elif length - silence >= MIN_UTTERANCE:
                            self.in_speech = True  # пока распознаём — диктовку не отправляем
                            audio = np.concatenate(buf)
                            text = self.transcribe(audio)
                            handled = False
                            if text and owner and self.is_echo(text):
                                log.info("перебивание оказалось эхом: %s", text)
                                text = ""
                            if text:
                                log.info("услышал: %s", text)
                                handled = self.handle(text)
                                if not handled:
                                    self.emit(type="heard", text=text, why="paused" if self.standby else "nowake")
                                elif not owner:
                                    self.spk.add(audio)  # принятая команда — образец голоса владельца
                            if owner:  # помощник на паузе: сказал своё — молчим, нет — договаривает
                                owner = False
                                self.pause_resume = not handled
                                self.pause_done.set()
                        if owner:  # фраза вышла слишком короткой — помощник договаривает
                            owner = False
                            self.pause_resume = True
                            self.pause_done.set()
                self.in_speech = in_speech
        self.in_speech = False
        self.restart_stream = False

    def level_loop(self):
        while True:
            self.emit(type="level", v=round(min(1.0, self.level * 8), 3))
            if self.thinking_until and time.time() >= self.thinking_until and self.state == "thinking":
                self.thinking_until = 0  # ответа так и не было — не висим в «думаю»
                self.set_state(self.idle_state())
            time.sleep(0.07)

    def run(self, with_levels=False):
        """Блокирующий запуск: модель, канал, микрофон."""
        self.set_state("loading")
        self.load_model()
        threading.Thread(target=self.events_loop, daemon=True).start()
        threading.Thread(target=self.dictation_loop, daemon=True).start()
        threading.Thread(target=self.prepare_fillers, daemon=True).start()
        if with_levels:
            threading.Thread(target=self.level_loop, daemon=True).start()
        while True:
            if not (self.mic_on or self.ptt):
                self.level = 0.0
                self.emit(type="mic", device=None, ok=False, off=True)
                self.set_state(self.idle_state())
                self.mic_event.wait()
                self.mic_event.clear()
                continue
            try:
                self.listen()
            except Exception as e:  # микрофон пропал (например, RDP переподключился)
                log.error("микрофон: %s — повтор через 5 с", e)
                self.level = 0.0
                self.emit(type="mic", device=None, ok=False, error=str(e))
                time.sleep(5)


def single_instance():
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 8791))
    except OSError:
        return None
    return lock


def setup_logging():
    logging.basicConfig(filename=HERE / "listener.log", encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("faster_whisper", "httpx", "huggingface_hub", "urllib3", "pywebview"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        from faster_whisper import decode_audio
        sys.stdout.reconfigure(encoding="utf-8")
        core = Core()
        core.load_model()
        text = core.transcribe(decode_audio(sys.argv[2]))
        m = WAKE.match(text)
        print("текст:", text)
        print("команда:", text[m.end():].strip() if m else "(нет слова «имя»)")
        return
    lock = single_instance()
    if not lock:
        sys.exit(0)
    setup_logging()
    Core().run()


if __name__ == "__main__":
    main()
