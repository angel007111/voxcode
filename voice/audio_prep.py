"""Подготовка звука с микрофона перед Whisper (только numpy).

  prepare(audio)  — float32, 16 кГц, моно -> то же, но чище и ровнее по громкости:
    1. убрать постоянную составляющую;
    2. мягкое шумоподавление (spectral gating): профиль шума берётся из самых тихих
       кадров этой же фразы (пре-ролл и паузы), постоянный шум (кулер, гул, RDP-шипение)
       приглушается максимум на FLOOR, чтобы не появлялся «булькающий» звук;
    3. срез ниже HIGHPASS Гц (гул, удары по столу);
    4. нормализация: речь к TARGET_RMS, пики не выше 0.99 — без обрезки.
"""
import numpy as np

SR = 16000
N_FFT = 512                 # 32 мс
HOP = 128
HIGHPASS = 80               # Гц
NOISE_PCT = 15              # перцентиль кадров, считающийся шумом
OVERSUB = 1.5               # во сколько раз шум «переоценивать» при вычитании
FLOOR = 0.2                 # не глушить сильнее чем до 20% (≈ −14 дБ)
TARGET_RMS = 0.08
MAX_BOOST = 40.0


def _stft(x, win):
    frames = np.lib.stride_tricks.sliding_window_view(x, N_FFT)[::HOP]
    return np.fft.rfft(frames * win, axis=1)


def _istft(spec, win, length):
    frames = np.fft.irfft(spec, n=N_FFT, axis=1) * win
    out = np.zeros(length + N_FFT)
    norm = np.zeros(length + N_FFT)
    for i, f in enumerate(frames):
        s = i * HOP
        out[s:s + N_FFT] += f
        norm[s:s + N_FFT] += win ** 2
    norm[norm < 1e-8] = 1.0
    return (out / norm)[:length]


def denoise(x):
    if len(x) < N_FFT * 4:
        return x
    pad = N_FFT // 2
    xp = np.pad(x, pad, mode="reflect")
    win = np.hanning(N_FFT + 1)[:-1]
    spec = _stft(xp, win)
    mag = np.abs(spec)
    if len(mag) < 10:
        return x
    noise = np.percentile(mag, NOISE_PCT, axis=0)
    gain = 1.0 - OVERSUB * noise / np.maximum(mag, 1e-10)
    gain = np.clip(gain, FLOOR, 1.0)
    # сглаживание по времени — меньше «музыкального» шума
    k = np.ones(3) / 3
    gain = np.apply_along_axis(lambda g: np.convolve(g, k, mode="same"), 0, gain)
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    gain[:, freqs < HIGHPASS] = 0.0
    return _istft(spec * gain, win, len(xp))[pad:pad + len(x)]


def normalize(x):
    frames = x[: len(x) // 480 * 480].reshape(-1, 480)  # кадры по 30 мс
    if not len(frames):
        return x
    rms = np.sqrt((frames ** 2).mean(axis=1))
    loud = rms[rms >= np.percentile(rms, 70)]          # кадры с речью
    speech = float(np.sqrt((loud ** 2).mean())) if len(loud) else 0.0
    if speech < 1e-6:
        return x
    g = min(TARGET_RMS / speech, MAX_BOOST)
    peak = float(np.abs(x).max()) * g
    if peak > 0.99:
        g *= 0.99 / peak
    return x * g


def prepare(audio, do_denoise=True):
    x = np.asarray(audio, dtype=np.float64)
    x = x - x.mean()
    if do_denoise:
        x = denoise(x)
    return normalize(x).astype(np.float32)
