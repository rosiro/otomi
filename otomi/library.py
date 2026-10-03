"""インデックス済みの音声をメモリに載せ、カテゴリ分類と検索を行う。"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass

import numpy as np

from . import categories as cat_mod
from . import config
from .db import Database

_CJK = re.compile(r"[぀-ヿ㐀-鿿＀-￯]")


@dataclass
class QueryPlan:
    kind: str  # "text" / "category" / "filename" / "none"
    description: str
    vector: np.ndarray | None = None
    filename: str | None = None


class Library:
    def __init__(self, db: Database, encoder):
        self.db = db
        self.encoder = encoder
        self._lock = threading.RLock()
        self.taxonomy = cat_mod.load_taxonomy(config.categories_path())
        self.cat_emb = cat_mod.category_embeddings(self.taxonomy, encoder.embed_text)
        self._text_cache: dict[str, np.ndarray] = {}
        self._empty()

    def _empty(self) -> None:
        self.ids = np.zeros(0, dtype=np.int64)
        self.paths: list[str] = []
        self.rows: list[dict] = []
        self.emb = np.zeros((0, self.cat_emb.shape[1]), dtype=np.float32)
        self.top_idx = np.zeros((0, 3), dtype=int)
        self.top_prob = np.zeros((0, 3), dtype=np.float32)
        self.effective: list[str] = []
        self.id_to_pos: dict[int, int] = {}

    # --- 読み込み・分類 -----------------------------------------------------
    def reload_categories(self) -> None:
        with self._lock:
            self.taxonomy = cat_mod.load_taxonomy(config.categories_path())
            self.cat_emb = cat_mod.category_embeddings(self.taxonomy, self.encoder.embed_text)
            self._classify()

    def reload(self) -> None:
        rows = self.db.load_indexed(self.encoder.model_name)
        with self._lock:
            if not rows:
                self._empty()
                return
            self.ids = np.array([r["id"] for r in rows], dtype=np.int64)
            self.paths = [r["path"] for r in rows]
            self.emb = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
            self.rows = [
                {
                    "id": r["id"],
                    "path": r["path"],
                    "root": r["root"],
                    "duration": r["duration"],
                    "samplerate": r["samplerate"],
                    "channels": r["channels"],
                    "peaks": list(r["peaks"] or b""),
                    "user_category": r["user_category"],
                    "favorite": bool(r["favorite"]),
                }
                for r in rows
            ]
            self.id_to_pos = {int(i): p for p, i in enumerate(self.ids)}
            self._classify()

    def _classify(self) -> None:
        self.top_idx, self.top_prob = cat_mod.classify(self.emb, self.cat_emb, self.encoder.logit_scale)
        ids = self.taxonomy.ids
        valid = set(ids)
        self.effective = [
            row["user_category"] if row["user_category"] in valid else ids[self.top_idx[i, 0]]
            for i, row in enumerate(self.rows)
        ]

    def __len__(self) -> int:
        return len(self.rows)

    # --- カテゴリ一覧 -------------------------------------------------------
    def category_tree(self) -> list[dict]:
        with self._lock:
            counts: dict[str, int] = {}
            for c in self.effective:
                counts[c] = counts.get(c, 0) + 1
            tree = []
            for g in self.taxonomy.groups:
                items = [{"id": c.id, "name": c.name, "count": counts.get(c.id, 0)} for c in g.items]
                tree.append({"id": g.id, "name": g.name, "count": sum(i["count"] for i in items), "items": items})
            return tree

    # --- 個別更新 -----------------------------------------------------------
    def set_user_category(self, sound_id: int, category: str | None) -> None:
        if category is not None and self.taxonomy.find(category) is None:
            raise KeyError(category)
        self.db.set_user_category(sound_id, category)
        with self._lock:
            pos = self.id_to_pos[sound_id]
            self.rows[pos]["user_category"] = category
            self.effective[pos] = category or self.taxonomy.ids[self.top_idx[pos, 0]]

    def set_favorite(self, sound_id: int, favorite: bool) -> None:
        self.db.set_favorite(sound_id, favorite)
        with self._lock:
            self.rows[self.id_to_pos[sound_id]]["favorite"] = favorite

    def path_of(self, sound_id: int) -> str | None:
        with self._lock:
            pos = self.id_to_pos.get(sound_id)
            return None if pos is None else self.paths[pos]

    # --- 検索 ---------------------------------------------------------------
    def text_vector(self, text: str) -> np.ndarray:
        key = text.strip().lower()
        if key not in self._text_cache:
            prompts = [t.format(text.strip()) for t in cat_mod.PROMPT_TEMPLATES]
            v = self.encoder.embed_text(prompts).mean(axis=0)
            if len(self._text_cache) > 512:
                self._text_cache.clear()
            self._text_cache[key] = v / np.linalg.norm(v)
        return self._text_cache[key]

    def plan_query(self, q: str) -> QueryPlan:
        """検索語の解釈を決める。

        CLAP のテキストエンコーダは英語のみなので、日本語の検索語はカテゴリ名と照合し、
        一致すればそのカテゴリの説明文で意味検索、一致しなければファイル名検索にする。
        """
        q = q.strip()
        if not q:
            return QueryPlan("none", "")
        if not _CJK.search(q):
            return QueryPlan("text", f"「{q}」で意味検索", vector=self.text_vector(q))
        hits = [i for i, c in enumerate(self.taxonomy.categories) if q in c.name or c.name in q]
        if not hits:
            hits = [i for i, c in enumerate(self.taxonomy.categories) if any(part and part in c.name for part in re.split(r"[・\s/]", q))]
        if hits:
            v = self.cat_emb[hits].mean(axis=0)
            names = "・".join(self.taxonomy.categories[i].name for i in hits[:4])
            return QueryPlan("category", f"カテゴリ「{names}」の説明文で意味検索", vector=v / np.linalg.norm(v))
        return QueryPlan("filename", f"ファイル名に「{q}」を含む音を検索 (意味検索は英語で入力してください)", filename=q)

    def search(
        self,
        *,
        q: str = "",
        name: str = "",
        category: str | None = None,
        group: str | None = None,
        similar: int | None = None,
        favorites: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        with self._lock:
            n = len(self.rows)
            mask = np.ones(n, dtype=bool)
            plan = self.plan_query(q)
            names = [name.lower()] if name.strip() else []
            if plan.filename:
                names.append(plan.filename.lower())
            for needle in names:
                mask &= np.array([needle in p.lower() for p in self.paths], dtype=bool)
            if category:
                mask &= np.array([c == category for c in self.effective], dtype=bool)
            elif group:
                prefix = group + "/"
                mask &= np.array([c.startswith(prefix) for c in self.effective], dtype=bool)
            if favorites:
                mask &= np.array([r["favorite"] for r in self.rows], dtype=bool)

            scores: np.ndarray | None = None
            description = plan.description
            if similar is not None and similar in self.id_to_pos:
                scores = self.emb @ self.emb[self.id_to_pos[similar]]
                mask[self.id_to_pos[similar]] = False
                description = f"「{os.path.basename(self.paths[self.id_to_pos[similar]])}」に似た音"
            elif plan.vector is not None:
                scores = self.emb @ plan.vector

            idx = np.nonzero(mask)[0]
            if scores is not None:
                idx = idx[np.argsort(-scores[idx], kind="stable")]
            elif category or group:
                conf = self._confidence()
                idx = idx[np.argsort(-conf[idx], kind="stable")]

            total = len(idx)
            page = idx[offset : offset + limit]
            items = [self._item(int(i), None if scores is None else float(scores[i])) for i in page]
            return {"total": total, "items": items, "description": description, "query_kind": plan.kind}

    def _confidence(self) -> np.ndarray:
        """effective カテゴリに対する確率 (手動設定は 1.0 扱い)"""
        index_of = {cid: k for k, cid in enumerate(self.taxonomy.ids)}
        conf = np.zeros(len(self.rows), dtype=np.float32)
        for i, c in enumerate(self.effective):
            if self.rows[i]["user_category"] == c:
                conf[i] = 1.0
            else:
                hit = np.nonzero(self.top_idx[i] == index_of[c])[0]
                conf[i] = self.top_prob[i, hit[0]] if len(hit) else 0.0
        return conf

    def _item(self, i: int, score: float | None) -> dict:
        row = self.rows[i]
        cats = self.taxonomy.categories
        eff = self.taxonomy.find(self.effective[i])
        top = [
            {"id": cats[j].id, "name": cats[j].name, "group": cats[j].group_name, "prob": round(float(p), 3)}
            for j, p in zip(self.top_idx[i], self.top_prob[i])
        ]
        rel = os.path.relpath(os.path.dirname(row["path"]), row["root"])
        return {
            "id": row["id"],
            "name": os.path.basename(row["path"]),
            "path": row["path"],
            "folder": "" if rel == "." else rel,
            "root": row["root"],
            "duration": row["duration"],
            "samplerate": row["samplerate"],
            "channels": row["channels"],
            "peaks": row["peaks"],
            "favorite": row["favorite"],
            "category": eff.id if eff else None,
            "category_name": eff.name if eff else None,
            "group_name": eff.group_name if eff else None,
            "manual": row["user_category"] is not None and eff is not None and row["user_category"] == eff.id,
            "top": top,
            "score": None if score is None else round(score, 4),
        }
