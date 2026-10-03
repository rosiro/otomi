"""コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import shutil
import sys

from . import config
from .db import Database
from .indexer import normalize_path


def _encoder():
    from .model import ClapEncoder

    return ClapEncoder()


def _index(db: Database, encoder, retry_errors: bool = False) -> None:
    from tqdm import tqdm

    from .indexer import IndexProgress, run_index

    bar = tqdm(unit="file", desc="解析")
    progress = IndexProgress()

    def on_chunk(done: int, total: int) -> None:
        bar.total = total
        bar.n = done
        bar.refresh()

    run_index(db, encoder, progress=progress, on_chunk=on_chunk, retry_errors=retry_errors)
    bar.close()
    print(f"完了: {progress.done} ファイルを解析, 失敗 {progress.errors}, 削除 {progress.removed}")


def cmd_serve(args) -> None:
    import uvicorn

    from .server import create_app

    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"otomi を起動しました: {url}  (Ctrl+C で終了)")
    uvicorn.run(create_app(scan_on_start=not args.no_scan), host=args.host, port=args.port, log_level="warning")


def cmd_add(args) -> None:
    db = Database(config.DB_PATH)
    for p in args.paths:
        path = normalize_path(p)
        if not os.path.isdir(path):
            sys.exit(f"フォルダが見つかりません: {path}")
        db.add_root(path)
        print(f"追加: {path}")
    if not args.no_index:
        _index(db, _encoder())


def cmd_remove(args) -> None:
    db = Database(config.DB_PATH)
    for p in args.paths:
        db.remove_root(normalize_path(p))
        print(f"削除: {normalize_path(p)}")


def cmd_roots(args) -> None:
    for r in Database(config.DB_PATH).roots():
        print(r)


def cmd_index(args) -> None:
    db = Database(config.DB_PATH)
    if not db.roots():
        sys.exit("フォルダが登録されていません。先に `otomi add <フォルダ>` を実行してください。")
    _index(db, _encoder(), retry_errors=args.retry_errors)


def _library():
    from .library import Library

    lib = Library(Database(config.DB_PATH), _encoder())
    lib.reload()
    return lib


def cmd_search(args) -> None:
    lib = _library()
    res = lib.search(q=args.query, category=args.category, limit=args.limit)
    print(f"{res['description']}  ({res['total']} 件)")
    for it in res["items"]:
        score = f"{it['score']:.3f}" if it["score"] is not None else "  -  "
        print(f"{score}  [{it['category_name']}]  {it['path']}")


def cmd_categories(args) -> None:
    lib = _library()
    for g in lib.category_tree():
        print(f"{g['name']} ({g['count']})")
        for c in g["items"]:
            if c["count"] or args.all:
                print(f"    {c['name']:<20} {c['count']:>6}   {c['id']}")


def cmd_export_csv(args) -> None:
    lib = _library()
    res = lib.search(limit=len(lib) or 1)
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["path", "group", "category", "category_id", "confidence", "manual",
                    "2nd", "2nd_prob", "3rd", "3rd_prob", "duration", "favorite"])
        for it in res["items"]:
            top = it["top"] + [{"name": "", "prob": ""}] * 3
            conf = 1.0 if it["manual"] else next((t["prob"] for t in it["top"] if t["id"] == it["category"]), "")
            w.writerow([it["path"], it["group_name"], it["category_name"], it["category"], conf, int(it["manual"]),
                        top[1]["name"], top[1]["prob"], top[2]["name"], top[2]["prob"],
                        f"{it['duration']:.3f}" if it["duration"] else "", int(it["favorite"])])
    print(f"{len(res['items'])} 件を書き出しました: {args.output}")


def cmd_export_folders(args) -> None:
    """カテゴリ別のフォルダにファイルをコピー (またはハードリンク) する。元ファイルは変更しない。"""
    lib = _library()
    res = lib.search(limit=len(lib) or 1)
    dest = os.path.abspath(args.dest)
    n = 0
    for it in res["items"]:
        folder = os.path.join(dest, _safe(it["group_name"]), _safe(it["category_name"]))
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, it["name"])
        stem, ext = os.path.splitext(target)
        k = 1
        while os.path.exists(target):
            target = f"{stem} ({k}){ext}"
            k += 1
        if args.mode == "hardlink":
            os.link(it["path"], target)
        else:
            shutil.copy2(it["path"], target)
        n += 1
    print(f"{n} 件を {dest} に書き出しました ({args.mode})")


def _safe(name: str) -> str:
    return "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name)


def cmd_init_categories(args) -> None:
    if config.USER_CATEGORIES_PATH.exists() and not args.force:
        sys.exit(f"既に存在します: {config.USER_CATEGORIES_PATH} (上書きするには --force)")
    config.USER_CATEGORIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(config.DEFAULT_CATEGORIES_PATH, config.USER_CATEGORIES_PATH)
    print(f"カテゴリ定義をコピーしました。編集してください: {config.USER_CATEGORIES_PATH}")


def main(argv: list[str] | None = None) -> None:
    if sys.platform == "win32":
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:
                pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for noisy in ("httpx", "urllib3", "huggingface_hub", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    p = argparse.ArgumentParser(prog="otomi", description="CLAP で効果音ライブラリを自動カテゴライズ・検索する")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Web UI を起動する")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-scan", action="store_true", help="起動時に新規・変更ファイルを解析しない")
    s.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("add", help="音声フォルダを登録して解析する")
    s.add_argument("paths", nargs="+")
    s.add_argument("--no-index", action="store_true", help="登録のみ行い解析しない")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("remove", help="登録したフォルダを解除する (ファイルは削除しない)")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("roots", help="登録フォルダの一覧")
    s.set_defaults(func=cmd_roots)

    s = sub.add_parser("index", help="登録フォルダを再スキャンし、新規・変更ファイルを解析する")
    s.add_argument("--retry-errors", action="store_true", help="読み込みに失敗したファイルも再試行する")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("search", help="テキストで検索する (英語推奨)")
    s.add_argument("query")
    s.add_argument("-c", "--category", help="カテゴリ ID で絞り込み (例: nature/rain)")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("categories", help="カテゴリごとの件数を表示する")
    s.add_argument("-a", "--all", action="store_true", help="0 件のカテゴリも表示")
    s.set_defaults(func=cmd_categories)

    s = sub.add_parser("export-csv", help="分類結果を CSV に書き出す")
    s.add_argument("output")
    s.set_defaults(func=cmd_export_csv)

    s = sub.add_parser("export-folders", help="カテゴリ別フォルダにコピー/ハードリンクで書き出す")
    s.add_argument("dest")
    s.add_argument("--mode", choices=["copy", "hardlink"], default="copy")
    s.set_defaults(func=cmd_export_folders)

    s = sub.add_parser("init-categories", help="カテゴリ定義を data/ にコピーしてカスタマイズ可能にする")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init_categories)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
