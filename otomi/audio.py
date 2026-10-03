"""音声ファイルの読み込み・リサンプリング・波形サムネイル生成。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

from . import config


@dataclass
class AudioData:
    samples: np.ndarray  # mono float32, config.SAMPLE_RATE
    duration: float  # 元ファイルの長さ (秒)
    samplerate: int  # 元ファイルのサンプルレート
    channels: int


def _load_soundfile(path: Path, max_seconds: float) -> tuple[np.ndarray, int, float, int]:
    with sf.SoundFile(str(path)) as f:
        sr, channels = f.samplerate, f.channels
        duration = f.frames / sr if f.frames > 0 else 0.0
        data = f.read(frames=int(max_seconds * sr), dtype="float32", always_2d=True)
    if duration == 0.0:
        duration = len(data) / sr
    return data, sr, duration, channels


def _load_pyav(path: Path, max_seconds: float) -> tuple[np.ndarray, int, float, int]:
    import av

    with av.open(str(path)) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        sr = stream.codec_context.sample_rate
        channels = stream.codec_context.channels
        resampler = av.AudioResampler(format="flt", layout="mono", rate=sr)
        chunks: list[np.ndarray] = []
        total = 0
        limit = int(max_seconds * sr)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                arr = out.to_ndarray().reshape(-1)
                chunks.append(arr)
                total += len(arr)
            if total >= limit:
                break
        duration = None
        if stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            duration = container.duration / 1_000_000
    data = np.concatenate(chunks)[:limit] if chunks else np.zeros(0, dtype=np.float32)
    if not duration:
        duration = len(data) / sr
    return data[:, None], sr, duration, channels


def load_audio(path: str | Path, max_seconds: float = config.MAX_DECODE_SECONDS) -> AudioData:
    """音声を mono / 48kHz / float32 で読み込む。soundfile で読めない形式は PyAV (ffmpeg) を使う。"""
    path = Path(path)
    try:
        data, sr, duration, channels = _load_soundfile(path, max_seconds)
    except Exception:
        data, sr, duration, channels = _load_pyav(path, max_seconds)

    mono = data.mean(axis=1).astype(np.float32) if data.ndim == 2 else data.astype(np.float32)
    if sr != config.SAMPLE_RATE and len(mono) > 0:
        mono = soxr.resample(mono, sr, config.SAMPLE_RATE).astype(np.float32)
    mono = np.nan_to_num(mono)
    return AudioData(samples=mono, duration=float(duration), samplerate=int(sr), channels=int(channels))


def make_clips(samples: np.ndarray) -> list[np.ndarray]:
    """CLAP に入力する 10 秒以内のクリップを作る。長い音は均等な位置から複数切り出す。"""
    clip_len = int(config.CLIP_SECONDS * config.SAMPLE_RATE)
    if len(samples) == 0:
        return [np.zeros(config.SAMPLE_RATE // 10, dtype=np.float32)]
    if len(samples) <= clip_len:
        return [samples]
    n = min(config.MAX_CLIPS_PER_FILE, math.ceil(len(samples) / clip_len))
    starts = np.linspace(0, len(samples) - clip_len, n).astype(int)
    return [samples[s : s + clip_len] for s in starts]


def compute_peaks(samples: np.ndarray, bins: int = config.PEAK_BINS) -> bytes:
    """波形サムネイル用に区間ごとの最大振幅を 0-255 に量子化して返す。"""
    if len(samples) == 0:
        return bytes(bins)
    a = np.abs(samples)
    edges = np.linspace(0, len(a), bins + 1).astype(int)
    peaks = np.array([a[s:e].max() if e > s else 0.0 for s, e in zip(edges[:-1], edges[1:])])
    top = peaks.max()
    if top > 0:
        peaks = peaks / top
    return (np.clip(peaks, 0, 1) * 255).astype(np.uint8).tobytes()
