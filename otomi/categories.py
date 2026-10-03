"""カテゴリ定義の読み込みと、CLAP によるゼロショット分類。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# 1 つの説明文から複数の文を作り、平均することで判定を安定させる
PROMPT_TEMPLATES = ["This is a sound of {}.", "{}", "{} sound effect"]


@dataclass
class Category:
    id: str  # "group/item"
    name: str
    group_id: str
    group_name: str
    prompts: list[str]


@dataclass
class Group:
    id: str
    name: str
    items: list[Category] = field(default_factory=list)


@dataclass
class Taxonomy:
    groups: list[Group]
    categories: list[Category]

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.categories]

    def find(self, cid: str) -> Category | None:
        return next((c for c in self.categories if c.id == cid), None)


def load_taxonomy(path: Path) -> Taxonomy:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    groups: list[Group] = []
    cats: list[Category] = []
    for gid, g in data.items():
        if not isinstance(g, dict) or "items" not in g:
            continue
        group = Group(id=gid, name=g.get("name", gid))
        for iid, item in g["items"].items():
            prompts = item.get("prompts") or [item.get("prompt") or iid]
            if isinstance(prompts, str):
                prompts = [prompts]
            cat = Category(
                id=f"{gid}/{iid}",
                name=item.get("name", iid),
                group_id=gid,
                group_name=group.name,
                prompts=list(prompts),
            )
            group.items.append(cat)
            cats.append(cat)
        groups.append(group)
    if not cats:
        raise ValueError(f"カテゴリが 1 つも定義されていません: {path}")
    return Taxonomy(groups=groups, categories=cats)


def category_embeddings(taxonomy: Taxonomy, embed_text) -> np.ndarray:
    """各カテゴリのテキスト埋め込み (C, D)。プロンプト×テンプレートの平均を正規化したもの。"""
    texts: list[str] = []
    owners: list[int] = []
    for i, cat in enumerate(taxonomy.categories):
        for p in cat.prompts:
            for t in PROMPT_TEMPLATES:
                texts.append(t.format(p))
                owners.append(i)
    embs: list[np.ndarray] = []
    for s in range(0, len(texts), 64):
        embs.append(embed_text(texts[s : s + 64]))
    all_emb = np.concatenate(embs)
    owners_arr = np.array(owners)
    out = np.stack([all_emb[owners_arr == i].mean(axis=0) for i in range(len(taxonomy.categories))])
    return out / np.linalg.norm(out, axis=1, keepdims=True)


def classify(audio_emb: np.ndarray, cat_emb: np.ndarray, logit_scale: float, top_k: int = 3):
    """各音声についてカテゴリ確率の上位 top_k を返す。

    returns: (indices (N, k), probs (N, k))
    """
    if len(audio_emb) == 0:
        return np.zeros((0, top_k), dtype=int), np.zeros((0, top_k), dtype=np.float32)
    logits = logit_scale * (audio_emb @ cat_emb.T)
    logits -= logits.max(axis=1, keepdims=True)
    probs = np.exp(logits)
    probs /= probs.sum(axis=1, keepdims=True)
    k = min(top_k, probs.shape[1])
    idx = np.argsort(-probs, axis=1)[:, :k]
    return idx, np.take_along_axis(probs, idx, axis=1)
