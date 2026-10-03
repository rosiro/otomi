"""SQLite によるインデックスの永続化。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    path TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS sounds (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    root TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    duration REAL,
    samplerate INTEGER,
    channels INTEGER,
    peaks BLOB,
    model TEXT,
    embedding BLOB,
    error TEXT,
    user_category TEXT,
    favorite INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sounds_root ON sounds(root);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # --- roots -------------------------------------------------------------
    def roots(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self.conn.execute("SELECT path FROM roots ORDER BY path")]

    def add_root(self, path: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO roots(path) VALUES (?)", (path,))

    def remove_root(self, path: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM roots WHERE path = ?", (path,))
            self.conn.execute("DELETE FROM sounds WHERE root = ?", (path,))

    # --- sounds ------------------------------------------------------------
    def file_states(self, root: str) -> dict[str, tuple[int, float, str | None, bool]]:
        """path -> (size, mtime, model, has_error)"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT path, size, mtime, model, error IS NOT NULL FROM sounds WHERE root = ?", (root,)
            ).fetchall()
        return {r[0]: (r[1], r[2], r[3], bool(r[4])) for r in rows}

    def delete_paths(self, paths: list[str]) -> None:
        with self._lock, self.conn:
            self.conn.executemany("DELETE FROM sounds WHERE path = ?", [(p,) for p in paths])

    def upsert_sound(
        self,
        *,
        path: str,
        root: str,
        size: int,
        mtime: float,
        duration: float | None = None,
        samplerate: int | None = None,
        channels: int | None = None,
        peaks: bytes | None = None,
        model: str | None = None,
        embedding: np.ndarray | None = None,
        error: str | None = None,
    ) -> None:
        emb = embedding.astype(np.float32).tobytes() if embedding is not None else None
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO sounds (path, root, size, mtime, duration, samplerate, channels,
                                    peaks, model, embedding, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    root=excluded.root, size=excluded.size, mtime=excluded.mtime,
                    duration=excluded.duration, samplerate=excluded.samplerate,
                    channels=excluded.channels, peaks=excluded.peaks, model=excluded.model,
                    embedding=excluded.embedding, error=excluded.error
                """,
                (path, root, size, mtime, duration, samplerate, channels, peaks, model, emb, error),
            )

    def load_indexed(self, model: str) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                """SELECT id, path, root, duration, samplerate, channels, peaks, embedding,
                          user_category, favorite
                   FROM sounds WHERE model = ? AND embedding IS NOT NULL ORDER BY path""",
                (model,),
            ).fetchall()

    def count_errors(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM sounds WHERE error IS NOT NULL").fetchone()[0]

    def set_user_category(self, sound_id: int, category: str | None) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE sounds SET user_category = ? WHERE id = ?", (category, sound_id))

    def set_favorite(self, sound_id: int, favorite: bool) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE sounds SET favorite = ? WHERE id = ?", (int(favorite), sound_id))
