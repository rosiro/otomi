"use strict";

const $ = (s) => document.querySelector(s);
const PAGE = 100;

const state = {
  ready: false,
  tree: [],
  filter: { kind: "all" }, // all | fav | group | category | similar
  items: [],
  total: 0,
  sel: -1,
  playingId: null,
  openGroups: new Set(JSON.parse(localStorage.getItem("otomi.openGroups") || "[]")),
  lastIndexing: false,
};

const audio = $("#audio");

// ---------------------------------------------------------------- API
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, ms = 2200) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

const fmtDur = (s) => {
  if (s == null) return "";
  if (s < 10) return s.toFixed(2) + "s";
  if (s < 60) return s.toFixed(1) + "s";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------------------------------------------------------------- status
async function pollStatus() {
  let st;
  try { st = await api("/api/status"); } catch { setTimeout(pollStatus, 3000); return; }
  const btn = $("#status");
  const p = st.progress;
  btn.classList.toggle("busy", st.indexing || p.phase === "loading");
  btn.classList.toggle("err", !!st.model_error);
  if (st.model_error) btn.textContent = "⚠ モデル読み込み失敗";
  else if (!st.ready) btn.textContent = "⏳ モデル読み込み中…";
  else if (st.indexing) btn.textContent = p.phase === "scanning" ? "🔎 スキャン中…" : `⚙ 解析中 ${p.done}/${p.total}`;
  else btn.textContent = `⚙ ${st.count.toLocaleString()} ファイル`;

  $("#progressText").textContent =
    st.indexing ? (p.phase === "scanning" ? "フォルダをスキャン中…" : `解析中 ${p.done} / ${p.total}  ${p.current || ""}`)
    : `${st.count.toLocaleString()} ファイル解析済み` + (st.errors ? ` / 読み込み失敗 ${st.errors} 件` : "") + (p.message ? ` — ${p.message}` : "");
  $("#progressBar").style.width = st.indexing && p.total ? `${(100 * p.done) / p.total}%` : st.indexing ? "5%" : "0";
  $("#catFile").textContent = st.categories_file;
  $("#modelInfo").textContent = `${st.model}  (${st.device || "-"})` + (st.model_error ? `\n${st.model_error}` : "");
  renderRoots(st.roots);

  const becameReady = st.ready && !state.ready;
  state.ready = st.ready;
  if (becameReady) {
    await loadTree();
    await search();
    if (!st.roots.length) openSettings();
  } else if (st.ready && (st.indexing || state.lastIndexing)) {
    // 解析中は定期的にカテゴリ件数を更新し、終わったら結果も更新する
    await loadTree();
    if (!st.indexing || state.items.length === 0) await search(true);
  }
  state.lastIndexing = st.indexing;
  setTimeout(pollStatus, st.indexing || !st.ready ? 1500 : 5000);
}

function renderRoots(roots) {
  const ul = $("#roots");
  const key = roots.join("\n");
  if (ul.dataset.key === key) return;
  ul.dataset.key = key;
  ul.innerHTML = roots.length ? "" : `<li class="muted">フォルダが登録されていません。効果音のフォルダを追加してください。</li>`;
  for (const r of roots) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${esc(r)}</span><button class="ghost">解除</button>`;
    li.querySelector("button").onclick = async () => {
      if (!confirmInline(li, "解除")) return;
      try { await api("/api/roots/remove", { method: "POST", body: { path: r } }); toast("登録を解除しました（ファイルは削除されません）"); }
      catch (e) { toast(e.message); }
      await loadTree(); await search();
    };
    ul.appendChild(li);
  }
}

// 2 度押しで確定 (ブラウザのダイアログを使わない)
function confirmInline(el, label) {
  const b = el.querySelector("button");
  if (b.dataset.armed) return true;
  b.dataset.armed = "1";
  b.textContent = "本当に解除？";
  setTimeout(() => { delete b.dataset.armed; b.textContent = label; }, 3000);
  return false;
}

// ---------------------------------------------------------------- sidebar
async function loadTree() {
  try { state.tree = await api("/api/categories"); } catch { return; }
  renderTree();
}

function renderTree() {
  const hide = $("#hideEmpty").checked;
  const f = state.filter;
  const ul = $("#tree");
  const total = state.tree.reduce((a, g) => a + g.count, 0);
  const node = (label, n, active, extra = "") =>
    `<div class="node ${active ? "active" : ""}" ${extra}><span>${label}</span><span class="n">${n ?? ""}</span></div>`;
  let html = `<li data-kind="all">${node("すべて", total.toLocaleString(), f.kind === "all")}</li>`;
  html += `<li data-kind="fav">${node("★ お気に入り", "", f.kind === "fav")}</li><li class="sep"></li>`;
  for (const g of state.tree) {
    if (hide && !g.count) continue;
    const open = state.openGroups.has(g.id) || (f.kind === "category" && f.id.startsWith(g.id + "/"));
    html += `<li class="group ${open ? "open" : ""}" data-group="${esc(g.id)}">`;
    html += node(`<span class="caret">▸</span>${esc(g.name)}`, g.count, f.kind === "group" && f.id === g.id, `data-kind="group" data-id="${esc(g.id)}"`);
    html += "<ul>";
    for (const c of g.items) {
      if (hide && !c.count) continue;
      html += `<li>${node(esc(c.name), c.count, f.kind === "category" && f.id === c.id, `data-kind="category" data-id="${esc(c.id)}"`)}</li>`;
    }
    html += "</ul></li>";
  }
  ul.innerHTML = html;
}

$("#tree").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-kind]");
  const node = e.target.closest(".node");
  if (!node) return;
  if (li) { setFilter({ kind: li.dataset.kind }); return; }
  const { kind, id } = node.dataset;
  if (kind === "group") {
    const groupLi = node.parentElement;
    const isActive = state.filter.kind === "group" && state.filter.id === id;
    // キャレット部分のクリック、または選択中グループの再クリックで開閉
    if (e.target.classList.contains("caret") || isActive) {
      groupLi.classList.toggle("open");
      if (groupLi.classList.contains("open")) state.openGroups.add(id); else state.openGroups.delete(id);
      localStorage.setItem("otomi.openGroups", JSON.stringify([...state.openGroups]));
      if (e.target.classList.contains("caret")) return;
    } else {
      state.openGroups.add(id);
    }
    if (!isActive) setFilter({ kind, id });
  } else if (kind === "category") {
    setFilter({ kind, id });
  }
});
$("#hideEmpty").addEventListener("change", renderTree);

function setFilter(f) {
  state.filter = f;
  renderTree();
  search();
}

// ---------------------------------------------------------------- search
let searchSeq = 0;
async function search(keepSelection = false) {
  if (!state.ready) return;
  const seq = ++searchSeq;
  const params = buildParams(0);
  let res;
  try { res = await api("/api/sounds?" + params); } catch (e) { toast(e.message); return; }
  if (seq !== searchSeq) return;
  const prevId = keepSelection && state.sel >= 0 ? state.items[state.sel]?.id : null;
  state.items = res.items;
  state.total = res.total;
  state.sel = prevId != null ? state.items.findIndex((it) => it.id === prevId) : -1;
  renderHead(res);
  renderList();
  if (!keepSelection) $("#list").scrollTop = 0;
}

function buildParams(offset) {
  const p = new URLSearchParams();
  const f = state.filter;
  const q = $("#q").value.trim();
  const name = $("#name").value.trim();
  if (q) p.set("q", q);
  if (name) p.set("name", name);
  if (f.kind === "group") p.set("group", f.id);
  if (f.kind === "category") p.set("category", f.id);
  if (f.kind === "fav") p.set("favorites", "true");
  if (f.kind === "similar") p.set("similar", f.id);
  p.set("offset", offset);
  p.set("limit", PAGE);
  return p;
}

async function loadMore() {
  const res = await api("/api/sounds?" + buildParams(state.items.length));
  state.items.push(...res.items);
  renderList(true);
}
$("#moreBtn").onclick = loadMore;
$("#list").addEventListener("scroll", (e) => {
  const el = e.target;
  if (el.scrollTop + el.clientHeight > el.scrollHeight - 300 && state.items.length < state.total && !loadMore.busy) {
    loadMore.busy = true;
    loadMore().finally(() => (loadMore.busy = false));
  }
});

function renderHead(res) {
  const f = state.filter;
  let title = "すべての音";
  if (f.kind === "fav") title = "★ お気に入り";
  if (f.kind === "group") title = state.tree.find((g) => g.id === f.id)?.name || f.id;
  if (f.kind === "category") {
    const g = state.tree.find((g) => f.id.startsWith(g.id + "/"));
    title = `${g?.name ?? ""} › ${g?.items.find((c) => c.id === f.id)?.name ?? f.id}`;
  }
  if (res.description) title = f.kind === "all" || f.kind === "similar" ? res.description : `${title} — ${res.description}`;
  $("#desc").innerHTML = esc(title) + (f.kind === "similar" ? ` <button id="clearSimilar" class="ghost">✕ 解除</button>` : "");
  $("#clearSimilar")?.addEventListener("click", () => setFilter({ kind: "all" }));
  $("#count").textContent = `${res.total.toLocaleString()} 件`;
}

let debounce;
for (const id of ["#q", "#name"]) {
  $(id).addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      if (state.filter.kind === "similar") state.filter = { kind: "all" };
      search();
    }, 300);
  });
}

// ---------------------------------------------------------------- list
function renderList(append = false) {
  const list = $("#list");
  const start = append ? list.children.length : 0;
  if (!append) list.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (let i = start; i < state.items.length; i++) frag.appendChild(rowEl(state.items[i], i));
  list.appendChild(frag);
  $("#more").classList.toggle("hidden", state.items.length >= state.total);
  const empty = $("#empty");
  empty.classList.toggle("hidden", state.items.length > 0);
  if (!state.items.length) {
    const hasRoots = $("#roots").dataset.key;
    empty.textContent = hasRoots ? "該当する音がありません" : "右上の ⚙ から効果音フォルダを追加してください";
  }
  requestAnimationFrame(() => list.querySelectorAll("canvas.wave").forEach((c) => drawWave(c)));
}

function rowEl(it, i) {
  const el = document.createElement("div");
  el.className = "item" + (i === state.sel ? " sel" : "") + (it.id === state.playingId ? " playing" : "");
  el.dataset.i = i;
  el.draggable = true;
  const prob = it.manual ? "手動" : `${Math.round((it.top.find((t) => t.id === it.category)?.prob ?? 0) * 100)}%`;
  const topTip = it.top.map((t) => `${t.group} › ${t.name}  ${Math.round(t.prob * 100)}%`).join("\n");
  el.innerHTML = `
    <button class="play" title="再生/停止 (Space)">${it.id === state.playingId ? "■" : "▶"}</button>
    <button class="fav ${it.favorite ? "on" : ""}" title="お気に入り (F)">${it.favorite ? "★" : "☆"}</button>
    <div class="meta" title="${esc(it.path)}">
      <div class="fname">${esc(it.name)}</div>
      <div class="folder">${esc(it.folder || "")}${it.score != null ? ` <span class="score">· ${it.score.toFixed(3)}</span>` : ""}</div>
    </div>
    <canvas class="wave" title="クリックした位置から再生"></canvas>
    <div class="dur">${fmtDur(it.duration)}</div>
    <div class="chip ${it.manual ? "manual" : ""}" title="${esc(topTip)}\n\nクリックでカテゴリを変更">
      <span class="g">${esc(it.group_name)}</span><span>${esc(it.category_name)}</span><span class="p">${prob}</span>
    </div>
    <div class="actions">
      <button data-act="similar" title="似た音を探す (S)">似た音</button>
      <button data-act="reveal" title="エクスプローラーで表示 (E)">📂</button>
      <button data-act="copy" title="パスをコピー">📋</button>
    </div>`;
  el.querySelector("canvas").peaks = it.peaks;
  return el;
}

function drawWave(canvas, progress = null) {
  const peaks = canvas.peaks || [];
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w) return;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const base = css.getPropertyValue("--wave"), played = css.getPropertyValue("--wave-played");
  const n = peaks.length || 1;
  const bw = w / n;
  for (let k = 0; k < peaks.length; k++) {
    const v = Math.max(1, (peaks[k] / 255) * (h - 2));
    ctx.fillStyle = progress != null && k / n < progress ? played : base;
    ctx.fillRect(k * bw, (h - v) / 2, Math.max(1, bw - 0.6), v);
  }
}

// 行内のクリック操作
$("#list").addEventListener("click", async (e) => {
  const row = e.target.closest(".item");
  if (!row) return;
  const i = +row.dataset.i;
  const it = state.items[i];
  select(i, false);
  if (e.target.closest(".play")) return togglePlay(it);
  if (e.target.closest(".fav")) return toggleFav(i);
  if (e.target.closest(".chip")) return openCatMenu(e.target.closest(".chip"), i);
  if (e.target.matches("canvas.wave")) {
    const r = e.target.getBoundingClientRect();
    return play(it, (e.clientX - r.left) / r.width);
  }
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act) return doAction(act, it);
  if ($("#autoplay").checked) play(it);
});
$("#list").addEventListener("dblclick", (e) => {
  const row = e.target.closest(".item");
  if (row && !e.target.closest("button,.chip,canvas")) play(state.items[+row.dataset.i]);
});

// DAW やエクスプローラーへのドラッグ&ドロップ (Chrome / Edge)
$("#list").addEventListener("dragstart", (e) => {
  const row = e.target.closest(".item");
  if (!row) return;
  const it = state.items[+row.dataset.i];
  const url = new URL(`/api/sounds/${it.id}/file`, location.href).href;
  e.dataTransfer.setData("DownloadURL", `application/octet-stream:${it.name}:${url}`);
  e.dataTransfer.setData("text/plain", it.path);
  e.dataTransfer.effectAllowed = "copy";
});

async function doAction(act, it) {
  if (act === "similar") {
    $("#q").value = "";
    setFilter({ kind: "similar", id: it.id });
  } else if (act === "reveal") {
    try { await api(`/api/sounds/${it.id}/reveal`, { method: "POST" }); } catch (e) { toast(e.message); }
  } else if (act === "copy") {
    try { await navigator.clipboard.writeText(it.path); toast("パスをコピーしました"); }
    catch { toast(it.path, 5000); }
  }
}

async function toggleFav(i) {
  const it = state.items[i];
  try {
    await api(`/api/sounds/${it.id}/favorite`, { method: "POST", body: { favorite: !it.favorite } });
    it.favorite = !it.favorite;
    const btn = $(`#list .item[data-i="${i}"] .fav`);
    btn.classList.toggle("on", it.favorite);
    btn.textContent = it.favorite ? "★" : "☆";
  } catch (e) { toast(e.message); }
}

function select(i, scroll = true) {
  const list = $("#list");
  list.querySelector(".item.sel")?.classList.remove("sel");
  state.sel = i;
  const row = list.querySelector(`.item[data-i="${i}"]`);
  if (row) {
    row.classList.add("sel");
    if (scroll) row.scrollIntoView({ block: "nearest" });
  }
}

// ---------------------------------------------------------------- playback
function rowOf(id) {
  const i = state.items.findIndex((it) => it.id === id);
  return i < 0 ? null : $(`#list .item[data-i="${i}"]`);
}

function setPlayingUI(id) {
  if (state.playingId != null) {
    const prev = rowOf(state.playingId);
    if (prev) { prev.classList.remove("playing"); prev.querySelector(".play").textContent = "▶"; drawWave(prev.querySelector("canvas")); }
  }
  state.playingId = id;
  const row = id != null ? rowOf(id) : null;
  if (row) { row.classList.add("playing"); row.querySelector(".play").textContent = "■"; }
}

function play(it, at = 0) {
  const src = `/api/sounds/${it.id}/audio`;
  if (audio.dataset.id !== String(it.id)) {
    audio.src = src;
    audio.dataset.id = it.id;
  }
  setPlayingUI(it.id);
  const start = () => {
    if (at > 0 && isFinite(audio.duration)) audio.currentTime = at * audio.duration;
    else audio.currentTime = 0;
    audio.play().catch((e) => { if (e.name !== "AbortError") toast("再生できませんでした: " + e.message); });
  };
  if (audio.readyState >= 1) start(); else audio.addEventListener("loadedmetadata", start, { once: true }), audio.load();
}

function stop() {
  audio.pause();
  setPlayingUI(null);
}

function togglePlay(it) {
  if (state.playingId === it.id && !audio.paused) stop(); else play(it);
}

audio.addEventListener("ended", () => setPlayingUI(null));
audio.addEventListener("error", () => { if (state.playingId != null) { toast("このファイルは再生できませんでした"); setPlayingUI(null); } });
audio.addEventListener("timeupdate", () => {
  const row = state.playingId != null ? rowOf(state.playingId) : null;
  if (row && audio.duration) drawWave(row.querySelector("canvas"), audio.currentTime / audio.duration);
});
$("#volume").addEventListener("input", (e) => {
  audio.volume = +e.target.value;
  localStorage.setItem("otomi.volume", e.target.value);
});
$("#volume").value = localStorage.getItem("otomi.volume") ?? "0.8";
audio.volume = +$("#volume").value;
$("#autoplay").checked = localStorage.getItem("otomi.autoplay") !== "0";
$("#autoplay").addEventListener("change", (e) => localStorage.setItem("otomi.autoplay", e.target.checked ? "1" : "0"));

// ---------------------------------------------------------------- category menu
function openCatMenu(chip, i) {
  const it = state.items[i];
  const menu = $("#catMenu");
  const opts = [];
  opts.push(`<input placeholder="カテゴリを検索">`);
  opts.push(`<div class="hdr">AI の判定</div>`);
  for (const t of it.top) {
    opts.push(`<div class="opt ${t.id === it.category ? "cur" : ""}" data-id="${esc(t.id)}"><span>${esc(t.group)} › ${esc(t.name)}</span><span class="muted">${Math.round(t.prob * 100)}%</span></div>`);
  }
  if (it.manual) opts.push(`<div class="opt" data-id=""><span>↺ 自動判定に戻す</span></div>`);
  for (const g of state.tree) {
    opts.push(`<div class="hdr">${esc(g.name)}</div>`);
    for (const c of g.items) opts.push(`<div class="opt ${c.id === it.category ? "cur" : ""}" data-id="${esc(c.id)}" data-search="${esc(g.name + c.name + c.id)}"><span>${esc(c.name)}</span></div>`);
  }
  menu.innerHTML = opts.join("");
  const r = chip.getBoundingClientRect();
  menu.style.left = Math.min(r.left, window.innerWidth - 270) + "px";
  menu.style.top = Math.min(r.bottom + 4, window.innerHeight - Math.min(window.innerHeight * 0.6, 420) - 8) + "px";
  menu.classList.remove("hidden");
  const input = menu.querySelector("input");
  input.focus();
  input.addEventListener("input", () => {
    const v = input.value.trim().toLowerCase();
    menu.querySelectorAll(".opt[data-search]").forEach((o) => o.classList.toggle("hidden", v && !o.dataset.search.toLowerCase().includes(v)));
  });
  menu.onclick = async (e) => {
    const opt = e.target.closest(".opt");
    if (!opt) return;
    const id = opt.dataset.id || null;
    try {
      await api(`/api/sounds/${it.id}/category`, { method: "POST", body: { category: id } });
      closeCatMenu();
      toast(id ? "カテゴリを変更しました" : "自動判定に戻しました");
      await loadTree();
      await search(true);
    } catch (err) { toast(err.message); }
  };
}
function closeCatMenu() { $("#catMenu").classList.add("hidden"); }
document.addEventListener("mousedown", (e) => {
  if (!e.target.closest("#catMenu") && !e.target.closest(".chip")) closeCatMenu();
});

// ---------------------------------------------------------------- settings
function openSettings() { $("#settings").classList.remove("hidden"); }
$("#status").onclick = openSettings;
$("#settings").addEventListener("click", (e) => {
  if (e.target.id === "settings" || e.target.closest("[data-close]")) $("#settings").classList.add("hidden");
});
$("#addRoot").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/roots", { method: "POST", body: { path: $("#rootPath").value } });
    $("#rootPath").value = "";
    toast("フォルダを追加しました。解析を開始します");
    pollStatus.kick?.();
  } catch (err) { toast(err.message, 4000); }
});
$("#scanBtn").onclick = async () => {
  try { const r = await api("/api/scan", { method: "POST", body: {} }); toast(r.started ? "スキャンを開始しました" : "既に解析中です"); }
  catch (e) { toast(e.message); }
};
$("#retryBtn").onclick = async () => {
  try { await api("/api/scan", { method: "POST", body: { retry_errors: true } }); toast("再試行を開始しました"); }
  catch (e) { toast(e.message); }
};
$("#stopBtn").onclick = () => api("/api/scan/stop", { method: "POST" }).then(() => toast("中断します…"));
$("#reloadCats").onclick = async () => {
  try {
    state.tree = await api("/api/categories/reload", { method: "POST" });
    renderTree();
    await search();
    toast("カテゴリ定義を再読み込みしました");
  } catch (e) { toast(e.message, 5000); }
};

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", (e) => {
  const typing = e.target.matches("input, textarea, select");
  if (e.key === "Escape") {
    closeCatMenu();
    $("#settings").classList.add("hidden");
    if (typing) e.target.blur();
    return;
  }
  if (typing) {
    if (e.key === "ArrowDown" && e.target.closest(".search")) { e.target.blur(); move(1); e.preventDefault(); }
    return;
  }
  if (!$("#settings").classList.contains("hidden")) return;
  const it = state.items[state.sel];
  switch (e.key) {
    case "ArrowDown": move(1); break;
    case "ArrowUp": move(-1); break;
    case " ": if (it) togglePlay(it); break;
    case "s": case "S": if (it) doAction("similar", it); break;
    case "f": case "F": if (it) toggleFav(state.sel); break;
    case "e": case "E": if (it) doAction("reveal", it); break;
    case "/": $("#q").focus(); break;
    default: return;
  }
  e.preventDefault();
});

async function move(d) {
  if (!state.items.length) return;
  let i = Math.max(0, Math.min(state.items.length - 1, state.sel + d));
  if (state.sel < 0) i = 0;
  if (i === state.items.length - 1 && state.items.length < state.total) await loadMore();
  select(i);
  if ($("#autoplay").checked) play(state.items[i]);
}

window.addEventListener("resize", () => {
  clearTimeout(window._rz);
  window._rz = setTimeout(() => document.querySelectorAll("canvas.wave").forEach((c) => drawWave(c)), 150);
});

pollStatus();
