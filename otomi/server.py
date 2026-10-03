"""ローカル Web UI 用の FastAPI サーバー。"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .db import Database
from .indexer import IndexProgress, normalize_path, run_index

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
# ブラウザがそのまま再生できない形式は WAV に変換して返す
BROWSER_UNSUPPORTED = {".aif", ".aiff", ".aifc", ".wma", ".caf"}


class AppState:
    def __init__(self, db: Database, scan_on_start: bool):
        self.db = db
        self.encoder = None
        self.library = None
        self.model_error: str | None = None
        self.progress = IndexProgress()
        self.stop = threading.Event()
        self._index_thread: threading.Thread | None = None
        self._scan_on_start = scan_on_start

    def start(self) -> None:
        threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self) -> None:
        try:
            from .library import Library
            from .model import ClapEncoder

            self.progress.update(phase="loading", message="CLAP モデルを読み込み中…")
            self.encoder = ClapEncoder()
            self.library = Library(self.db, self.encoder)
            self.library.reload()
            self.progress.update(phase="idle", message="")
        except Exception as e:
            log.exception("モデルの読み込みに失敗しました")
            self.model_error = f"{type(e).__name__}: {e}"
            self.progress.update(phase="error", message=self.model_error)
            return
        if self._scan_on_start and self.db.roots():
            self.start_index()

    @property
    def ready(self) -> bool:
        return self.library is not None

    @property
    def indexing(self) -> bool:
        return self._index_thread is not None and self._index_thread.is_alive()

    def start_index(self, retry_errors: bool = False) -> bool:
        if not self.ready or self.indexing:
            return False
        self.stop.clear()

        def work():
            last_reload = [0]

            def on_chunk(done: int, total: int) -> None:
                # 解析途中でも結果を少しずつ UI に反映する
                if done - last_reload[0] >= 500:
                    last_reload[0] = done
                    self.library.reload()

            try:
                run_index(self.db, self.encoder, progress=self.progress, stop=self.stop,
                          on_chunk=on_chunk, retry_errors=retry_errors)
            except Exception as e:
                log.exception("インデックス作成に失敗しました")
                self.progress.update(phase="error", message=f"{type(e).__name__}: {e}")
            finally:
                self.library.reload()

        self._index_thread = threading.Thread(target=work, daemon=True)
        self._index_thread.start()
        return True


def _require(state: AppState):
    if not state.ready:
        raise HTTPException(503, state.model_error or "モデルを読み込み中です")
    return state.library


def _path_or_404(state: AppState, sound_id: int) -> str:
    path = _require(state).path_of(sound_id)
    if path is None or not os.path.exists(path):
        raise HTTPException(404, "ファイルが見つかりません")
    return path


def _to_wav(path: str) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception:
        from .audio import _load_pyav

        data, sr, _, _ = _load_pyav(Path(path), config.MAX_DECODE_SECONDS)
    sf.write(buf, data, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _reveal(path: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


class RootBody(BaseModel):
    path: str


class CategoryBody(BaseModel):
    category: str | None = None


class FavoriteBody(BaseModel):
    favorite: bool


class ScanBody(BaseModel):
    retry_errors: bool = False


def create_app(db_path: Path = config.DB_PATH, scan_on_start: bool = True, state: AppState | None = None) -> FastAPI:
    state = state or AppState(Database(db_path), scan_on_start)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not state.ready and state.model_error is None:
            state.start()
        yield
        state.stop.set()

    app = FastAPI(title="otomi", lifespan=lifespan)
    app.state.otomi = state

    @app.get("/api/status")
    def status():
        lib = state.library
        return {
            "ready": state.ready,
            "model": config.MODEL_NAME,
            "device": getattr(state.encoder, "device", None),
            "model_error": state.model_error,
            "indexing": state.indexing,
            "progress": state.progress.as_dict(),
            "count": len(lib) if lib else 0,
            "errors": state.db.count_errors(),
            "roots": state.db.roots(),
            "categories_file": str(config.categories_path()),
        }

    @app.get("/api/categories")
    def categories():
        return _require(state).category_tree()

    @app.post("/api/categories/reload")
    def reload_categories():
        lib = _require(state)
        try:
            lib.reload_categories()
        except Exception as e:
            raise HTTPException(400, f"カテゴリ定義の読み込みに失敗しました: {e}")
        return lib.category_tree()

    @app.get("/api/sounds")
    def sounds(
        q: str = "",
        name: str = "",
        category: str | None = None,
        group: str | None = None,
        similar: int | None = None,
        favorites: bool = False,
        offset: int = 0,
        limit: int = 100,
    ):
        return _require(state).search(
            q=q, name=name, category=category, group=group, similar=similar,
            favorites=favorites, offset=max(0, offset), limit=max(1, min(limit, 500)),
        )

    @app.get("/api/sounds/{sound_id}/audio")
    def sound_audio(sound_id: int):
        path = _path_or_404(state, sound_id)
        if os.path.splitext(path)[1].lower() in BROWSER_UNSUPPORTED:
            return Response(_to_wav(path), media_type="audio/wav")
        return FileResponse(path, media_type=mimetypes.guess_type(path)[0] or "application/octet-stream")

    @app.get("/api/sounds/{sound_id}/file")
    def sound_file(sound_id: int):
        path = _path_or_404(state, sound_id)
        return FileResponse(path, filename=os.path.basename(path))

    @app.post("/api/sounds/{sound_id}/reveal")
    def sound_reveal(sound_id: int):
        _reveal(_path_or_404(state, sound_id))
        return {"ok": True}

    @app.post("/api/sounds/{sound_id}/category")
    def sound_category(sound_id: int, body: CategoryBody):
        lib = _require(state)
        if lib.path_of(sound_id) is None:
            raise HTTPException(404)
        try:
            lib.set_user_category(sound_id, body.category)
        except KeyError:
            raise HTTPException(400, "不明なカテゴリです")
        return {"ok": True}

    @app.post("/api/sounds/{sound_id}/favorite")
    def sound_favorite(sound_id: int, body: FavoriteBody):
        lib = _require(state)
        if lib.path_of(sound_id) is None:
            raise HTTPException(404)
        lib.set_favorite(sound_id, body.favorite)
        return {"ok": True}

    @app.post("/api/roots")
    def add_root(body: RootBody):
        path = normalize_path(body.path.strip().strip('"'))
        if not os.path.isdir(path):
            raise HTTPException(400, f"フォルダが見つかりません: {path}")
        state.db.add_root(path)
        state.start_index()
        return {"roots": state.db.roots()}

    @app.post("/api/roots/remove")
    def remove_root(body: RootBody):
        if state.indexing:
            raise HTTPException(409, "解析中は削除できません")
        state.db.remove_root(body.path)
        if state.ready:
            state.library.reload()
        return {"roots": state.db.roots()}

    @app.post("/api/scan")
    def scan(body: ScanBody | None = None):
        if not state.ready:
            raise HTTPException(503, "モデルを読み込み中です")
        started = state.start_index(retry_errors=bool(body and body.retry_errors))
        return {"started": started}

    @app.post("/api/scan/stop")
    def scan_stop():
        state.stop.set()
        return {"ok": True}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
