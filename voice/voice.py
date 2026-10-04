"""Голос Зевса: распознавание голосовых и озвучка ответов.

  python voice.py transcribe <audio>            -> печатает текст
  python voice.py speak "<текст>" [out.mp3]     -> печатает путь к mp3
  python voice.py voice "<текст>" <chat_id>     -> озвучивает и шлёт голосовым в Telegram
"""
import os
import site
import sys
import tempfile
import time
from pathlib import Path

WHISPER_MODEL = os.environ.get("ZEWS_WHISPER_MODEL", "small")
VOICE = os.environ.get("ZEWS_VOICE", "ru-RU-DmitryNeural")
# запасной голос: edge-tts порой отвечает пустотой на конкретном голосе, а соседние работают
FALLBACK_VOICE = os.environ.get("ZEWS_FALLBACK_VOICE", "en-US-AndrewMultilingualNeural")
_primary_bad_until = 0.0
# Google Cloud TTS (официальный API, бесплатно до 1 млн симв./мес для Chirp 3 HD):
# ключ — в voice/.google_tts_key (в .gitignore) или ZEWS_GOOGLE_TTS_KEY. Нет ключа — сразу edge-tts.
GOOGLE_VOICE = os.environ.get("ZEWS_GOOGLE_VOICE", "ru-RU-Chirp3-HD-Charon")
GOOGLE_KEY_FILE = Path(__file__).parent / ".google_tts_key"


def google_key():
    key = os.environ.get("ZEWS_GOOGLE_TTS_KEY", "").strip()
    if not key and GOOGLE_KEY_FILE.exists():
        key = GOOGLE_KEY_FILE.read_text(encoding="utf-8").strip()
    return key


def google_synth(text, path, voice=None, key=None):
    """Google Cloud TTS → mp3. Бросает исключение при любой ошибке (сеть, ключ, квота)."""
    import base64
    import requests

    voice = voice or GOOGLE_VOICE
    r = requests.post(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        params={"key": key or google_key()},
        json={"input": {"text": text},
              "voice": {"languageCode": "-".join(voice.split("-")[:2]), "name": voice},
              "audioConfig": {"audioEncoding": "MP3"}},
        timeout=(5, 20),
    )
    if not r.ok:
        raise RuntimeError(f"Google TTS {r.status_code}: {r.text[:200]}")
    Path(path).write_bytes(base64.b64decode(r.json()["audioContent"]))


def synth(text, path, log=None):
    """Озвучка → mp3: Google (если есть ключ), иначе/при сбое edge-tts —
    три попытки основным голосом, затем две запасным."""
    import asyncio
    import edge_tts

    if google_key():
        try:
            return google_synth(text, path)
        except Exception as e:
            if log:
                log(f"озвучка: Google не ответил ({e}), перехожу на edge-tts")

    global _primary_bad_until
    # основной сбоит — 10 минут сразу запасной: каждая неудачная попытка стоит ~2.5 с
    voices = ([] if time.time() < _primary_bad_until else [VOICE] * 3) + [FALLBACK_VOICE] * 2
    for attempt, v in enumerate(voices):
        try:
            asyncio.run(edge_tts.Communicate(text, v).save(str(path)))
            if v != VOICE and log:
                log(f"озвучка: основной голос сбоит, сказал голосом {v}")
            return
        except edge_tts.exceptions.NoAudioReceived:
            if v == VOICE and attempt == 2:
                _primary_bad_until = time.time() + 600
            if attempt == len(voices) - 1:
                raise
            time.sleep(0.5)


def _add_cuda_dlls():
    # cuBLAS/cuDNN из pip-пакетов nvidia-* (Windows не находит их сама)
    for base in [site.getusersitepackages(), *site.getsitepackages()]:
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            d = Path(base) / "nvidia" / sub / "bin"
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


def transcribe(path):
    _add_cuda_dlls()
    from faster_whisper import WhisperModel, decode_audio

    audio = decode_audio(path)

    def run(device, compute_type):
        model = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute_type)
        segments, _ = model.transcribe(audio, language="ru", vad_filter=True)
        return " ".join(s.text.strip() for s in segments)

    try:
        text = run("cuda", "float16")
    except Exception as e:  # нет GPU/CUDA — считаем на процессоре
        print(f"[cuda недоступна: {e}; работаю на CPU]", file=sys.stderr)
        text = run("cpu", "int8")
    print(text.strip())


def speak(text, out=None):
    out = out or os.path.join(tempfile.gettempdir(), "zews_reply.mp3")
    synth(text, out)
    print(out)


def _to_ogg_opus(src, dst):
    # Telegram показывает голосовое только для OGG/Opus
    import av

    with av.open(src) as inp, av.open(dst, "w", format="ogg") as outp:
        stream = outp.add_stream("libopus", rate=48000, layout="mono")
        stream.bit_rate = 32000
        for frame in inp.decode(audio=0):
            frame.pts = None
            for packet in stream.encode(frame):
                outp.mux(packet)
        for packet in stream.encode(None):
            outp.mux(packet)


def _bot_token():
    env = Path.home() / ".claude" / "channels" / "telegram" / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("TELEGRAM_BOT_TOKEN не найден")


def voice(text, chat_id):
    import requests

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    mp3, ogg = out_dir / "reply.mp3", out_dir / "reply.ogg"
    synth(text, mp3)
    _to_ogg_opus(str(mp3), str(ogg))
    with open(ogg, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{_bot_token()}/sendVoice",
            data={"chat_id": chat_id},
            files={"voice": ("reply.ogg", f, "audio/ogg")},
            timeout=60,
        )
    resp = r.json()
    if not resp.get("ok"):
        raise SystemExit(f"sendVoice: {resp.get('description')}")
    print(f"sent voice (id: {resp['result']['message_id']})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) >= 3 and sys.argv[1] == "transcribe":
        transcribe(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "speak":
        speak(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif len(sys.argv) >= 4 and sys.argv[1] == "voice":
        voice(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)
