"""フォルダを走査して音声ファイルの CLAP 埋め込みを計算し、DB に保存する。"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import audio, config
from .db import Database

log = logging.getLogger(__name__)

FILES_PER_CHUNK = 32
CLIPS_PER_BATCH = 16


def normalize_path(p: str | Path) -> str:
    return os.path.normpath(os.path.abspath(str(p)))


@dataclass
class IndexProgress:
    phase: str = "idle"  # idle / scanning / embedding / done / error
    total: int = 0
    done: int = 0
    errors: int = 0
    removed: int = 0
    current: str = ""
    message: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        with self.lock:
            return {
                "phase": self.phase, "total": self.total, "done": self.done,
                "errors": self.errors, "removed": self.removed,
                "current": self.current, "message": self.message,
            }

    def update(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)


def iter_audio_files(root: str):
    """root 以下の音声ファイルを (path, size, mtime) で列挙する。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__MACOSX"]
        for name in filenames:
            if name.startswith("._"):  # macOS のリソースフォーク
                continue
            if os.path.splitext(name)[1].lower() not in config.AUDIO_EXTENSIONS:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            yield normalize_path(path), st.st_size, st.st_mtime


@dataclass
class Job:
    path: str
    root: str
    size: int
    mtime: float


def plan_jobs(db: Database, roots: list[str], model_name: str, retry_errors: bool = False):
    """新規・変更・モデル違いのファイルを洗い出す。消えたファイルは DB から削除する。"""
    jobs: list[Job] = []
    removed = 0
    for root in roots:
        if not os.path.isdir(root):
            log.warning("フォルダが見つかりません (スキップ): %s", root)
            continue
        known = db.file_states(root)
        seen: set[str] = set()
        for path, size, mtime in iter_audio_files(root):
            seen.add(path)
            state = known.get(path)
            if state is not None:
                k_size, k_mtime, k_model, k_err = state
                unchanged = k_size == size and abs(k_mtime - mtime) < 1e-3
                if unchanged and (k_err and not retry_errors or k_model == model_name):
                    continue
            jobs.append(Job(path, root, size, mtime))
        gone = [p for p in known if p not in seen]
        if gone:
            db.delete_paths(gone)
            removed += len(gone)
    return jobs, removed


def _prepare(job: Job):
    try:
        data = audio.load_audio(job.path)
        return data, audio.make_clips(data.samples), audio.compute_peaks(data.samples), None
    except Exception as e:  # 壊れたファイルなどは記録して先へ進む
        return None, None, None, f"{type(e).__name__}: {e}"


def run_index(
    db: Database,
    encoder,
    roots: list[str] | None = None,
    progress: IndexProgress | None = None,
    stop: threading.Event | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
    retry_errors: bool = False,
) -> IndexProgress:
    progress = progress or IndexProgress()
    roots = roots if roots is not None else db.roots()
    progress.update(phase="scanning", total=0, done=0, errors=0, removed=0, current="", message="")

    jobs, removed = plan_jobs(db, roots, encoder.model_name, retry_errors)
    progress.update(phase="embedding", total=len(jobs), removed=removed)
    log.info("解析対象: %d ファイル (削除: %d)", len(jobs), removed)

    chunks = [jobs[i : i + FILES_PER_CHUNK] for i in range(0, len(jobs), FILES_PER_CHUNK)]
    workers = max(2, min(8, (os.cpu_count() or 4) // 2))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pending = [ex.submit(_prepare, j) for j in chunks[0]] if chunks else []
        for ci, chunk in enumerate(chunks):
            results = [f.result() for f in pending]
            # GPU が計算している間に次のチャンクをデコードしておく
            pending = [ex.submit(_prepare, j) for j in chunks[ci + 1]] if ci + 1 < len(chunks) else []
            if stop is not None and stop.is_set():
                for f in pending:
                    f.cancel()
                progress.update(phase="done", message="中断しました")
                return progress
            _embed_chunk(db, encoder, chunk, results, progress)
            if on_chunk:
                on_chunk(progress.done, progress.total)

    progress.update(phase="done", current="")
    return progress


def _embed_chunk(db: Database, encoder, chunk: list[Job], results, progress: IndexProgress) -> None:
    clips: list[np.ndarray] = []
    owner: list[int] = []
    for i, (_, file_clips, _, err) in enumerate(results):
        if err is None:
            clips.extend(file_clips)
            owner.extend([i] * len(file_clips))

    embs = np.zeros((0, 0), dtype=np.float32)
    if clips:
        try:
            parts = [encoder.embed_audio(clips[s : s + CLIPS_PER_BATCH]) for s in range(0, len(clips), CLIPS_PER_BATCH)]
            embs = np.concatenate(parts)
        except Exception as e:
            log.exception("埋め込み計算に失敗しました")
            results = [(None, None, None, f"embedding failed: {e}") for _ in results]
            owner = []
    owner_arr = np.array(owner)

    errors = 0
    for i, (job, (data, _, peaks, err)) in enumerate(zip(chunk, results)):
        if err is not None:
            errors += 1
            log.warning("読み込み失敗: %s (%s)", job.path, err)
            db.upsert_sound(path=job.path, root=job.root, size=job.size, mtime=job.mtime, error=err)
            continue
        emb = embs[owner_arr == i].mean(axis=0)
        emb /= max(float(np.linalg.norm(emb)), 1e-12)
        db.upsert_sound(
            path=job.path, root=job.root, size=job.size, mtime=job.mtime,
            duration=data.duration, samplerate=data.samplerate, channels=data.channels,
            peaks=peaks, model=encoder.model_name, embedding=emb,
        )
    with progress.lock:
        progress.done += len(chunk)
        progress.errors += errors
        progress.current = chunk[-1].path
