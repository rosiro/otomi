"""CLAP モデルを使わない (偽のエンコーダで置き換えた) 高速なテスト。"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from otomi import audio, config
from otomi.categories import classify, load_taxonomy
from otomi.db import Database
from otomi.indexer import plan_jobs, run_index

SR = 48_000


class FakeEncoder:
    """テキストは単語ごとの固定ベクトル、音声は周波数帯ごとのベクトルを返す偽エンコーダ。"""

    model_name = "fake"
    logit_scale = 30.0
    dim = 16

    def _vec(self, seed: int) -> np.ndarray:
        v = np.random.default_rng(seed).normal(size=self.dim)
        return v / np.linalg.norm(v)

    def embed_text(self, texts):
        out = []
        for t in texts:
            t = t.lower()
            if "beep" in t or "tone" in t:
                out.append(self._vec(1))
            elif "noise" in t or "rain" in t:
                out.append(self._vec(2))
            else:
                out.append(self._vec(abs(hash(t)) % 10_000 + 100))
        return np.stack(out)

    def embed_audio(self, clips):
        out = []
        for c in clips:
            # 高周波成分 (ノイズ) が多ければ "rain"、そうでなければ "beep"
            hf = np.abs(np.diff(c)).mean() / (np.abs(c).mean() + 1e-9)
            out.append(self._vec(2) if hf > 0.5 else self._vec(1))
        return np.stack(out)


@pytest.fixture
def sounds(tmp_path):
    root = tmp_path / "se"
    (root / "sub").mkdir(parents=True)
    t = np.arange(SR) / SR
    sf.write(root / "beep.wav", 0.5 * np.sin(2 * np.pi * 440 * t), SR)
    sf.write(root / "sub" / "noise.flac", np.random.default_rng(0).uniform(-0.5, 0.5, SR // 2), SR)
    sf.write(root / "long_beep.aiff", 0.3 * np.sin(2 * np.pi * 880 * np.arange(25 * 22050) / 22050), 22050)
    (root / "broken.wav").write_bytes(b"not a wav file")
    (root / "._beep.wav").write_bytes(b"apple double")
    (root / "notes.txt").write_text("ignore me")
    return root


@pytest.fixture
def taxonomy_file(tmp_path, monkeypatch):
    p = tmp_path / "cats.toml"
    p.write_text(
        """
[tone]
name = "電子音"
[tone.items]
beep = { name = "ビープ", prompts = ["beep"] }
[nature]
name = "自然"
[nature.items]
rain = { name = "雨", prompts = ["rain", "noise"] }
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "categories_path", lambda: p)
    return p


def test_load_audio_resamples_and_reports_original(sounds):
    d = audio.load_audio(sounds / "long_beep.aiff")
    assert d.samplerate == 22050
    assert d.duration == pytest.approx(25.0, abs=0.01)
    assert len(d.samples) == pytest.approx(25 * SR, abs=10)


def test_make_clips_long_and_short():
    assert len(audio.make_clips(np.zeros(SR, dtype=np.float32))) == 1
    clips = audio.make_clips(np.zeros(35 * SR, dtype=np.float32))
    assert len(clips) == 4 and all(len(c) == 10 * SR for c in clips)
    assert len(audio.make_clips(np.zeros(0, dtype=np.float32))) == 1


def test_peaks_are_normalized():
    p = audio.compute_peaks(np.array([0, 0.1, -0.5, 0.25] * 100, dtype=np.float32), bins=10)
    assert len(p) == 10 and max(p) == 255


def test_default_taxonomy_is_valid():
    tax = load_taxonomy(config.DEFAULT_CATEGORIES_PATH)
    assert len(tax.categories) > 50
    assert len(set(tax.ids)) == len(tax.ids)
    assert all(c.prompts for c in tax.categories)


def test_classify_top_k():
    a = np.eye(3, dtype=np.float32)
    idx, prob = classify(a, a, logit_scale=10, top_k=2)
    assert idx[:, 0].tolist() == [0, 1, 2]
    assert np.all(prob[:, 0] > prob[:, 1])


def test_index_and_search(tmp_path, sounds, taxonomy_file):
    from otomi.library import Library

    db = Database(tmp_path / "db.sqlite")
    root = str(sounds)
    db.add_root(root)
    enc = FakeEncoder()

    prog = run_index(db, enc)
    assert prog.total == 4  # ._ ファイルと .txt は除外
    assert prog.errors == 1  # broken.wav

    # 2 回目は変更がないので何もしない
    jobs, removed = plan_jobs(db, [root], enc.model_name)
    assert jobs == [] and removed == 0

    lib = Library(db, enc)
    lib.reload()
    assert len(lib) == 3
    by_name = {it["name"]: it for it in lib.search(limit=10)["items"]}
    assert by_name["beep.wav"]["category"] == "tone/beep"
    assert by_name["long_beep.aiff"]["category"] == "tone/beep"
    assert by_name["noise.flac"]["category"] == "nature/rain"
    assert by_name["noise.flac"]["folder"] == "sub"

    # 英語の意味検索
    res = lib.search(q="rain")
    assert res["query_kind"] == "text" and res["items"][0]["name"] == "noise.flac"
    # 日本語はカテゴリ名と照合
    res = lib.search(q="雨")
    assert res["query_kind"] == "category" and res["items"][0]["name"] == "noise.flac"
    # 一致しない日本語はファイル名検索
    assert lib.search(q="存在しない")["total"] == 0
    # カテゴリ絞り込み・似た音
    assert lib.search(group="tone")["total"] == 2
    sim = lib.search(similar=by_name["beep.wav"]["id"])
    assert sim["items"][0]["name"] == "long_beep.aiff" and sim["total"] == 2

    # 手動カテゴリ・お気に入り (DB に保存され、再読み込み後も残る)
    lib.set_user_category(by_name["beep.wav"]["id"], "nature/rain")
    lib.set_favorite(by_name["beep.wav"]["id"], True)
    lib.reload()
    assert lib.search(category="nature/rain")["total"] == 2
    fav = lib.search(favorites=True)["items"]
    assert [it["name"] for it in fav] == ["beep.wav"] and fav[0]["manual"]

    # ファイル削除は次回スキャンで反映される
    (sounds / "beep.wav").unlink()
    jobs, removed = plan_jobs(db, [root], enc.model_name)
    assert removed == 1


def test_server_endpoints(tmp_path, sounds, taxonomy_file):
    from fastapi.testclient import TestClient

    from otomi.library import Library
    from otomi.server import AppState, create_app

    db = Database(tmp_path / "db.sqlite")
    db.add_root(str(sounds))
    enc = FakeEncoder()
    run_index(db, enc)
    state = AppState(db, scan_on_start=False)
    state.encoder = enc
    state.library = Library(db, enc)
    state.library.reload()

    with TestClient(create_app(state=state)) as client:
        st = client.get("/api/status").json()
        assert st["ready"] and st["count"] == 3 and st["errors"] == 1
        assert client.get("/").status_code == 200
        items = client.get("/api/sounds", params={"q": "beep"}).json()["items"]
        aiff = next(it for it in items if it["name"].endswith(".aiff"))
        r = client.get(f"/api/sounds/{aiff['id']}/audio")
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"  # AIFF は WAV に変換
        assert client.get("/api/sounds/99999/audio").status_code == 404
        assert client.post(f"/api/sounds/{aiff['id']}/category", json={"category": "nope/x"}).status_code == 400
        assert client.post("/api/roots", json={"path": str(tmp_path / "missing")}).status_code == 400
        tree = client.get("/api/categories").json()
        assert sum(g["count"] for g in tree) == 3
