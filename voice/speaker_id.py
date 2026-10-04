"""Узнавание голоса владельца (SpeechBrain ECAPA): нужно, чтобы перебивать Зевса без слова «Зевс».

Образцы копятся сами: каждая фраза владельца, которую слушатель принял как команду (не во время
речи Зевса), добавляет «отпечаток» голоса в owner_voice.npy (последние MAX_SAMPLES).
Во время озвучки слушатель сравнивает звук с микрофона со средним отпечатком: похоже — это владелец.
Начать заново — удалить owner_voice.npy.
"""
import logging
import threading
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PROFILE = HERE / "owner_voice.npy"
MODEL_DIR = HERE / "models" / "ecapa"
MIN_SAMPLES = 3       # столько фраз нужно, чтобы начать узнавать
MAX_SAMPLES = 30
MIN_SECONDS = 1.0     # короче — отпечаток ненадёжный, в образцы не берём
SR = 16000

log = logging.getLogger("zews")
logging.getLogger("speechbrain").setLevel(logging.WARNING)  # иначе засоряет listener.log при каждом старте


class SpeakerID:
    def __init__(self, on_ready=None):
        self.on_ready = on_ready or (lambda: None)  # профиль только что набрался
        self.model = None
        self.lock = threading.Lock()
        self.samples = np.load(PROFILE) if PROFILE.exists() else np.zeros((0, 192), np.float32)
        self.mean = None
        self._update_mean()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            import torch  # noqa: F401
            from speechbrain.inference.speaker import EncoderClassifier
            self.model = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(MODEL_DIR), run_opts={"device": "cpu"})
            log.info("голос владельца: модель готова, образцов %d", len(self.samples))
        except Exception as e:
            log.error("голос владельца: модель не загрузилась (%s) — перебивание только по «Зевс»", e)

    def _update_mean(self):
        if len(self.samples) >= MIN_SAMPLES:
            m = self.samples.mean(axis=0)
            self.mean = m / (np.linalg.norm(m) or 1.0)
        else:
            self.mean = None

    @property
    def ready(self):
        return self.model is not None and self.mean is not None

    def embed(self, audio):
        import torch
        with self.lock, torch.no_grad():
            e = self.model.encode_batch(torch.from_numpy(np.ascontiguousarray(audio, np.float32))[None])
        e = e.squeeze().numpy().astype(np.float32)
        return e / (np.linalg.norm(e) or 1.0)

    def score(self, audio):
        """Похожесть на владельца (косинус, ~0.6+ — он), None — ещё не умеем."""
        if not self.ready:
            return None
        return float(self.embed(audio) @ self.mean)

    def compare(self, audio, other):
        """(похожесть на владельца, похожесть на other) по одному отпечатку; other — отпечаток голоса Зевса."""
        if not self.ready:
            return None, None
        e = self.embed(audio)
        return float(e @ self.mean), (float(e @ other) if other is not None else 0.0)

    def add(self, audio):
        """Фраза владельца -> новый образец (в фоне, чтобы не тормозить микрофон)."""
        if self.model is None or len(audio) < SR * MIN_SECONDS:
            return
        threading.Thread(target=self._add, args=(audio,), daemon=True).start()

    def _add(self, audio):
        try:
            e = self.embed(audio)
        except Exception as ex:
            log.error("голос владельца: %s", ex)
            return
        was_ready = self.mean is not None
        self.samples = np.vstack([self.samples, e[None]])[-MAX_SAMPLES:]
        np.save(PROFILE, self.samples)
        self._update_mean()
        if self.mean is not None and not was_ready:
            log.info("голос владельца: запомнил (%d образца)", len(self.samples))
            self.on_ready()
