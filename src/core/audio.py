"""Beat / onset / BPM analysis with numpy only (no librosa dependency)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import ffmpeg

SR = 22050
HOP = 512
NFFT = 2048


def _stft_mag(y: np.ndarray) -> np.ndarray:
    # centered frames: frame i is centered on sample i*HOP
    y = np.pad(y, (NFFT // 2, NFFT // 2))
    if len(y) < NFFT:
        y = np.pad(y, (0, NFFT - len(y)))
    n_frames = 1 + (len(y) - NFFT) // HOP
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    win = np.hanning(NFFT).astype(np.float32)
    frames = y[idx] * win
    return np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)  # [frames, bins]


def onset_envelope(y: np.ndarray, sr: int = SR) -> tuple[np.ndarray, np.ndarray]:
    """Spectral-flux onset strength (full band) and low band (<150 Hz, bass/kick)."""
    S = np.log1p(10.0 * _stft_mag(y))
    flux = np.maximum(0.0, np.diff(S, axis=0))
    freqs = np.fft.rfftfreq(NFFT, 1.0 / sr)
    low = flux[:, freqs < 150].sum(axis=1)
    full = flux.sum(axis=1)
    full = np.concatenate([[0.0], full])
    low = np.concatenate([[0.0], low])
    # remove slow trend, keep peaks
    k = 16
    if len(full) > k:
        trend = np.convolve(full, np.ones(k) / k, mode="same")
        full = np.maximum(0.0, full - trend)
    for arr in (full, low):
        m = arr.max()
        if m > 0:
            arr /= m
    return full.astype(np.float32), low.astype(np.float32)


def estimate_tempo(env: np.ndarray, sr: int = SR, lo: float = 60.0, hi: float = 200.0, prior: float = 120.0) -> float:
    fr = sr / HOP
    e = env - env.mean()
    if not np.any(e):
        return prior
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    lags = np.arange(len(ac))
    min_lag = max(1, int(fr * 60.0 / hi))
    max_lag = min(len(ac) - 1, int(fr * 60.0 / lo))
    if max_lag <= min_lag:
        return prior
    cand = lags[min_lag:max_lag + 1]
    bpm = 60.0 * fr / cand
    weight = np.exp(-0.5 * (np.log2(bpm / prior) / 0.9) ** 2)
    score = ac[min_lag:max_lag + 1] * weight
    best = cand[int(np.argmax(score))]
    # parabolic refinement for sub-frame lag
    i = int(best)
    if 1 <= i < len(ac) - 1:
        a, b, c = ac[i - 1], ac[i], ac[i + 1]
        denom = a - 2 * b + c
        if denom != 0:
            best = i + 0.5 * (a - c) / denom
    return float(60.0 * fr / best)


def track_beats(env: np.ndarray, bpm: float, sr: int = SR, tightness: float = 100.0) -> np.ndarray:
    """Ellis (2007) dynamic programming beat tracker. Returns beat times in seconds."""
    fr = sr / HOP
    period = 60.0 * fr / bpm
    n = len(env)
    if n == 0:
        return np.array([])
    score = np.zeros(n, dtype=np.float64)
    back = -np.ones(n, dtype=np.int64)
    lo, hi = int(round(period / 2)), int(round(period * 2))
    for t in range(n):
        start, end = max(0, t - hi), t - lo
        best_val, best_i = 0.0, -1
        if end > start:
            prev = np.arange(start, end)
            pen = -tightness * (np.log((t - prev) / period)) ** 2
            vals = score[prev] + pen
            j = int(np.argmax(vals))
            best_val, best_i = float(vals[j]), int(prev[j])
        score[t] = env[t] + max(0.0, best_val)
        back[t] = best_i if best_val > 0 else -1
    # last beat: max score in final period
    tail = max(0, n - int(period))
    t = tail + int(np.argmax(score[tail:]))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = np.array(beats[::-1], dtype=np.float64)
    return beats / fr


def analyze_track(path: Path, max_seconds: float = 600.0) -> dict:
    y = ffmpeg.decode_audio(path, sr=SR, duration=max_seconds)
    return analyze_signal(y, SR)


def analyze_signal(y: np.ndarray, sr: int = SR) -> dict:
    duration = len(y) / sr
    if duration < 1.0 or float(np.abs(y).max(initial=0)) < 1e-4:
        return {"duration": duration, "bpm": 0.0, "beats": [], "strong": [], "bass": [], "energy": [], "confidence": 0.0}
    env, low = onset_envelope(y, sr)
    bpm = estimate_tempo(env, sr)
    # fold into 70..180 range typical for edits
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    beats = track_beats(env, bpm, sr)
    fr = sr / HOP
    idx = np.clip((beats * fr).round().astype(int), 0, len(env) - 1)
    strength = env[idx] if len(idx) else np.array([])
    bass = low[idx] if len(idx) else np.array([])
    thr = float(np.percentile(strength, 70)) if len(strength) else 1.0
    bthr = float(np.percentile(bass, 70)) if len(bass) else 1.0
    strong = [round(float(t), 3) for t, s in zip(beats, strength) if s >= thr]
    bass_hits = [round(float(t), 3) for t, s in zip(beats, bass) if s >= bthr and s > 0.05]
    # beat-to-beat regularity → confidence
    conf = 0.0
    if len(beats) > 4:
        ib = np.diff(beats)
        conf = float(max(0.0, 1.0 - np.std(ib) / (np.mean(ib) + 1e-6)))
    # RMS energy per second (for picking intense parts of the track)
    win = sr
    nsec = int(len(y) // win)
    energy = [round(float(np.sqrt(np.mean(y[i * win:(i + 1) * win] ** 2))), 5) for i in range(nsec)]
    return {
        "duration": round(duration, 3),
        "bpm": round(float(bpm), 2),
        "beats": [round(float(b), 3) for b in beats],
        "strong": strong,
        "bass": bass_hits,
        "energy": energy,
        "confidence": round(conf, 3),
    }


def grid_beats(bpm: float, duration: float, offset: float = 0.0) -> list[float]:
    period = 60.0 / bpm
    n = int((duration - offset) / period) + 1
    return [round(offset + i * period, 3) for i in range(max(0, n))]
