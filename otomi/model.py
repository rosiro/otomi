"""CLAP モデルのラッパー。音声・テキストを共通の埋め込み空間に写像する。"""

from __future__ import annotations

import logging
import threading

import numpy as np

from . import config

log = logging.getLogger(__name__)


def _as_tensor(out):
    # transformers のバージョンによって get_*_features がテンソルか出力オブジェクトを返す
    if hasattr(out, "pooler_output"):
        return out.pooler_output
    return out


def _normalize(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(n, 1e-12)


class ClapEncoder:
    def __init__(self, model_name: str = config.MODEL_NAME, device: str | None = None):
        import torch
        from transformers import ClapModel, ClapProcessor

        self.torch = torch
        self.model_name = model_name
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        log.info("CLAP モデルを読み込み中: %s (%s)", model_name, self.device)
        self.processor = ClapProcessor.from_pretrained(model_name)
        self.model = ClapModel.from_pretrained(model_name).to(self.device).eval()
        self.logit_scale = float(self.model.logit_scale_a.exp().item())
        self._lock = threading.Lock()  # GPU 推論は直列化する

    def embed_audio(self, clips: list[np.ndarray]) -> np.ndarray:
        """48kHz mono のクリップ列 -> 正規化済み埋め込み (len(clips), D)"""
        fe = self.processor.feature_extractor
        inputs = fe(clips, sampling_rate=config.SAMPLE_RATE, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with self._lock, self.torch.inference_mode():
            emb = _as_tensor(self.model.get_audio_features(**inputs))
        return _normalize(emb.float().cpu().numpy())

    def embed_text(self, texts: list[str]) -> np.ndarray:
        """テキスト列 -> 正規化済み埋め込み (len(texts), D)"""
        tok = self.processor.tokenizer
        inputs = tok(texts, padding=True, truncation=True, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with self._lock, self.torch.inference_mode():
            emb = _as_tensor(self.model.get_text_features(**inputs))
        return _normalize(emb.float().cpu().numpy())
