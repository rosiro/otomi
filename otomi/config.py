"""アプリ全体の設定値。環境変数で上書きできる。"""

from __future__ import annotations

import os
from pathlib import Path

# データ (SQLite DB) の保存先。既定はリポジトリ直下の data/
DATA_DIR = Path(os.environ.get("OTOMI_HOME", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "otomi.db"

# 使用する CLAP モデル (Hugging Face Hub の ID)。
# larger_clap_general は効果音・環境音などの一般音に強いチェックポイント。
MODEL_NAME = os.environ.get("OTOMI_MODEL", "laion/larger_clap_general")

# カテゴリ定義ファイル。ユーザー定義があればそちらを優先する。
DEFAULT_CATEGORIES_PATH = Path(__file__).resolve().parent / "categories.toml"
USER_CATEGORIES_PATH = DATA_DIR / "categories.toml"

AUDIO_EXTENSIONS = {
    ".wav", ".wave", ".mp3", ".flac", ".ogg", ".oga", ".opus",
    ".aif", ".aiff", ".aifc", ".m4a", ".aac", ".wma", ".caf",
}

# CLAP の入力仕様
SAMPLE_RATE = 48_000
CLIP_SECONDS = 10.0
# 長いファイルから切り出すクリップの最大数 (均等な位置から取り出して平均する)
MAX_CLIPS_PER_FILE = 4
# デコードする最大長 (秒)。これより長いファイルは先頭のみ解析する。
MAX_DECODE_SECONDS = 300.0
# 波形サムネイルの分解能
PEAK_BINS = 160


def categories_path() -> Path:
    return USER_CATEGORIES_PATH if USER_CATEGORIES_PATH.exists() else DEFAULT_CATEGORIES_PATH
