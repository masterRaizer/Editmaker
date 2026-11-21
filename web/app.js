"use strict";

const $ = (id) => document.getElementById(id);
const S = { projectId: null, cfg: null, env: null, sources: [], tracks: [], projects: [], project: null, variantId: null, jobs: [], selected: new Set() };

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const err = new Error((data && data.error) || (data && data.detail) || r.statusText);
    err.status = r.status; err.data = data;
    throw err;
  }
  return data;
}
const media = (p) => "/api/media?path=" + encodeURIComponent(p);
const fmtTime = (s) => (s == null ? "—" : `${Math.floor(s / 60)}:${(s % 60).toFixed(2).padStart(5, "0")}`);
const showErr = (el, e) => { el.textContent = e ? (e.message || String(e)) + (e.data && e.data.instructions ? "\n\n" + e.data.instructions : "") : ""; el.hidden = !e; };
const STATUS_RU = { draft: "draft", rendered: "rendered", approved: "approved", published: "published", failed: "failed" };

/* ---------------------------------------------------------------- init */
async function init() {
  const st = await api("/api/state");
  S.cfg = st.config; S.env = st.env;
  $("envState").textContent = `${st.env.nvenc ? "NVENC" + (st.env.gpu ? " · " + st.env.gpu : "") : "libx264 (CPU)"}`;
  const vibe = $("vibe");
  vibe.innerHTML = `<option value="auto">Авто по динамике видео</option>` +
    Object.entries(st.env.vibes).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  fillSetup();
  renderAuth(st.auth);
  await Promise.all([loadSources(), loadTracks(), loadProjects()]);
  if (new URLSearchParams(location.search).get("auth")) history.replaceState(null, "", "/");
  setInterval(pollJobs, 1200);
}

function fillSetup() {
  const c = S.cfg, w = c.watermark, r = c.render;
  renderFolders();
  $("outputFolder").value = c.output_folder;
  $("vibe").value = c.vibe;
  document.querySelector(`input[name=wmType][value=${w.type}]`).checked = true;
  $("wmText").value = w.text; $("wmImage").value = w.image_path;
  $("wmPos").value = w.position; $("wmColor").value = w.color.startsWith("#") ? w.color.toLowerCase() : "#" + w.color;
  $("wmOpacity").value = w.opacity; $("wmSize").value = w.size;
  $("wmIn").checked = w.in_video; $("wmOutro").checked = w.outro; $("wmDrift").checked = w.drift;
  $("resolution").value = r.resolution; $("fps").value = String(r.fps); $("duration").value = r.duration;
  $("variantCount").value = r.variants; $("quality").value = r.quality; $("encoder").value = r.encoder;
  syncWm();
}

function syncWm() {
  const img = document.querySelector("input[name=wmType]:checked").value === "image";
  $("wmTextBox").hidden = img; $("wmImageBox").hidden = !img;
  $("wmOpacityOut").textContent = Number($("wmOpacity").value).toFixed(2);
  $("wmSizeOut").textContent = Number($("wmSize").value).toFixed(3);
}

function watermarkFromForm() {
  return {
    type: document.querySelector("input[name=wmType]:checked").value,
    text: $("wmText").value, image_path: $("wmImage").value.trim(),
    position: $("wmPos").value, color: $("wmColor").value,
    opacity: Number($("wmOpacity").value), size: Number($("wmSize").value),
    in_video: $("wmIn").checked, outro: $("wmOutro").checked, drift: $("wmDrift").checked,
  };
}

/* ---------------------------------------------------------------- sources */
function renderFolders() {
  const ul = $("folderList");
  ul.innerHTML = "";
  S.cfg.source_folders.forEach((f, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span></span><button type="button" aria-label="Убрать папку">×</button>`;
    li.querySelector("span").textContent = f;
    li.querySelector("button").onclick = async () => {
      S.cfg = await api("/api/config", { method: "PUT", body: { source_folders: S.cfg.source_folders.filter((_, j) => j !== i) } });
      renderFolders();
    };
    ul.appendChild(li);
  });
}

async function loadSources() {
  S.sources = await api("/api/sources");
  const ul = $("clipList");
  ul.innerHTML = "";
  $("clipEmpty").hidden = S.sources.length > 0;
  $("clipBox").hidden = S.sources.length === 0;
  for (const s of S.sources) {
    const li = document.createElement("li");
    li.innerHTML = `<label><input type="checkbox"><span class="t"></span><span class="m"></span></label>`;
    const cb = li.querySelector("input");
    cb.checked = S.selected.has(s.id);
    cb.onchange = () => { cb.checked ? S.selected.add(s.id) : S.selected.delete(s.id); syncAll(); };
    li.querySelector(".t").textContent = s.title + (s.scene ? ` — ${s.scene}` : "");
    li.querySelector(".t").title = s.path;
    li.querySelector(".m").textContent = `${s.width}×${s.height} ${Number(s.fps).toFixed(2).replace(/\.00$/, "")}fps ${fmtTime(s.duration)} ${s.vcodec || ""}${s.has_audio ? "" : " без звука"}`;
    ul.appendChild(li);
  }
  syncAll();
}
function syncAll() { $("clipAll").checked = S.sources.length > 0 && S.selected.size === S.sources.length; }

/* ---------------------------------------------------------------- music */
async function loadTracks() {
  S.tracks = await api("/api/music");
  renderTrackSelect();
}
function renderTrackSelect(extraPath) {
  const sel = $("track"), cur = extraPath || sel.value || "auto";
  const vibe = $("vibe").value;
  const list = vibe === "auto" ? S.tracks : S.tracks.filter((t) => t.vibe === vibe);
  sel.innerHTML = `<option value="auto">Автоподбор по темпу видео</option><option value="none">Звук исходника</option>` +
    list.map((t) => `<option value="${escapeAttr(t.path)}">${escapeHtml(t.title)} · ${Math.round(t.bpm)} BPM · ${fmtTime(t.duration)}</option>`).join("");
  if (extraPath && !list.some((t) => t.path === extraPath)) {
    sel.insertAdjacentHTML("beforeend", `<option value="${escapeAttr(extraPath)}">${escapeHtml(extraPath.split(/[\\/]/).pop())}</option>`);
  }
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "auto";
  $("musicHint").hidden = S.tracks.length > 0;
}
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const escapeAttr = escapeHtml;

/* ---------------------------------------------------------------- projects / variants */
async function loadProjects(selectId) {
  S.projects = await api("/api/projects");
  const sel = $("projectSelect");
  sel.innerHTML = S.projects.length
    ? S.projects.map((p) => `<option value="${p.id}">${escapeHtml(p.name)} (${p.variant_count})</option>`).join("")
    : `<option value="">Проектов пока нет</option>`;
  const id = selectId || S.projectId || (S.projects[0] && S.projects[0].id);
  if (id) { sel.value = String(id); S.projectId = Number(id); await loadProject(id); }
}

async function loadProject(id) {
  S.projectId = Number(id);
  const data = await api(`/api/projects/${id}`);
  if (Number(id) !== S.projectId) return; // a newer selection won the race
  S.project = data;
  if (!S.project.variants.some((v) => v.id === S.variantId)) S.variantId = S.project.variants[0] && S.project.variants[0].id;
  renderProject();
}

function currentVariant() { return S.project && S.project.variants.find((v) => v.id === S.variantId); }
function jobFor(vid) { return S.jobs.find((j) => j.ref && j.ref.variant_id === vid && (j.status === "running" || j.status === "queued")); }

function renderProject() {
  const p = S.project;
  $("previewEmpty").hidden = !!(p && p.variants.length);
  $("previewBox").hidden = !(p && p.variants.length);
  if (!p) return;
  const tabs = $("variantTabs");
  tabs.innerHTML = "";
  for (const v of p.variants) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "tab"; b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(v.id === S.variantId));
    const job = jobFor(v.id);
    const st = job && job.kind === "render" ? `${job.status === "queued" ? "в очереди" : "рендер " + Math.round(job.progress * 100) + "%"}` : STATUS_RU[v.status];
    b.innerHTML = `<span class="n"></span><span class="status ${job ? "" : v.status}"></span>`;
    b.querySelector(".n").textContent = `${String(v.idx).padStart(2, "0")} ${v.style_name}`;
    b.querySelector(".status").textContent = st;
    b.onclick = () => { S.variantId = v.id; renderProject(); };
    tabs.appendChild(b);
  }
  renderVariant();
}

let shownFile = null;
function renderVariant() {
  const v = currentVariant();
  if (!v) return;
  const job = jobFor(v.id);
  const player = $("player");
  const file = v.status !== "draft" && v.file_path ? v.file_path + "#" + (v.fingerprint || "") : null;
  if (file !== shownFile) {
    shownFile = file;
    if (file) { player.src = media(v.file_path) + "&v=" + (v.fingerprint || ""); player.poster = v.thumb_path ? media(v.thumb_path) : ""; }
    else { player.removeAttribute("src"); player.load(); }
  }
  $("renderState").hidden = !job;
  if (job) {
    $("renderBar").style.width = Math.round(job.progress * 100) + "%";
    $("renderText").textContent = job.status === "queued" ? "в очереди" : `${Math.round(job.progress * 100)}% ${job.message || ""}`;
  }
  showErr($("variantError"), v.status === "failed" ? new Error(v.error || "Ошибка рендера") : null);
  drawTimeline(v.plan);
  renderInfo(v);
  renderMeta(v);
}

function renderInfo(v) {
  const p = v.plan || {}, r = v.render || {}, w = v.watermark || {}, pr = r.probe || {};
  const music = p.music ? `${p.music.title} с ${p.music.start.toFixed(2)} с` : (p.segments ? "звук исходника, −14 LUFS" : "—");
  const wm = w.type === "image" ? (w.image_path || "").split(/[\\/]/).pop() : w.text;
  const where = [w.in_video && "в ролике", w.outro && `в конце ${Number(w.outro_duration || 2).toFixed(1)} с`].filter(Boolean).join(", ") || "выключен";
  const rows = [
    ["Длительность", p.total_duration != null ? `${fmtTime(p.total_duration)} (монтаж ${fmtTime(p.main_duration)})` : "—"],
    ["BPM", p.bpm ? p.bpm.toFixed(1) : "—"],
    ["Склейки", p.cuts != null ? `${p.cuts}, средний план ${p.avg_shot.toFixed(2)} с, первая на ${fmtTime(p.hook_first_cut)}` : "—"],
    ["Музыка", music],
    ["Водяной знак", wm ? `${wm} — ${where}` : where],
    ["Видео", pr.width ? `${pr.width}×${pr.height} ${pr.fps}fps ${r.encoder_used || ""} ${pr.bitrate ? Math.round(pr.bitrate / 1000) + " kb/s" : ""}` : "—"],
    ["Файл", v.file_path || "—"],
  ];
  if (p.note) rows.push(["Примечание", p.note]);
  $("variantInfo").innerHTML = rows.map(([k, val]) => `<dt>${k}</dt><dd>${escapeHtml(val)}</dd>`).join("");
}

/* Beat ruler: grey ticks = beats, taller ticks = strong beats, red = cuts, band at the end = outro. */
function drawTimeline(plan) {
  const cv = $("timeline"), dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = 56;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext("2d");
  g.scale(dpr, dpr);
  g.clearRect(0, 0, w, h);
  if (!plan || !plan.total_duration) return;
  const css = getComputedStyle(document.documentElement);
  const T = plan.total_duration, x = (t) => 1 + (t / T) * (w - 2);
  const base = 34;
  g.fillStyle = css.getPropertyValue("--line"); g.fillRect(0, base, w, 1);
  g.fillStyle = css.getPropertyValue("--muted");
  const strong = new Set((plan.strong || []).map((t) => t.toFixed(2)));
  for (const b of plan.beats || []) { const s = strong.has(b.toFixed(2)); g.fillRect(Math.round(x(b)), base - (s ? 12 : 6), 1, s ? 12 : 6); }
  g.fillStyle = css.getPropertyValue("--cut");
  for (const b of (plan.boundaries || []).slice(1, -1)) g.fillRect(Math.round(x(b)) - 1, 6, 2, base - 6);
  if (plan.outro) {
    g.fillStyle = css.getPropertyValue("--field");
    g.fillRect(x(plan.main_duration), 6, x(T) - x(plan.main_duration), base - 6);
  }
  g.fillStyle = css.getPropertyValue("--muted");
  g.font = `11px ${css.getPropertyValue("--mono")}`;
  g.textBaseline = "top";
  for (let t = 0; t <= T; t += T > 30 ? 10 : 5) g.fillText(fmtTime(t).replace(/\.\d\d$/, ""), Math.min(x(t), w - 34), base + 6);
  if (plan.outro) g.fillText("концовка", Math.max(0, x(plan.main_duration) + 4), 10);
}
$("player").addEventListener("timeupdate", () => {
  const v = currentVariant(); if (!v || !v.plan) return;
  drawTimeline(v.plan);
  const cv = $("timeline"), g = cv.getContext("2d"), w = cv.clientWidth;
  const xx = 1 + ($("player").currentTime / v.plan.total_duration) * (w - 2);
  g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--text");
  g.fillRect(Math.round(xx), 0, 1, 48);
});

/* ---------------------------------------------------------------- metadata + approval */
let metaFor = null;
function renderMeta(v) {
  const has = v.metadata && (v.status !== "draft" && v.status !== "failed");
  $("metaEmpty").hidden = !!has;
  $("metaForm").hidden = !has;
  if (!has) return;
  const m = v.metadata, yt = m.youtube || {}, tt = m.tiktok || {};
  const key = v.id + ":" + v.updated_at;
  if (metaFor !== key && !$("metaForm").contains(document.activeElement)) {
    metaFor = key;
    document.querySelectorAll("input[name=platform]").forEach((cb) => (cb.checked = (m.platforms || []).includes(cb.value)));
    $("ytTitle").value = yt.title || ""; $("ytDesc").value = yt.description || "";
    $("ytTags").value = (yt.tags || []).join(", "); $("ytPrivacy").value = yt.privacy || "private";
    $("ttTitle").value = tt.title || ""; $("ttDesc").value = tt.description || "";
    $("ttTags").value = (tt.hashtags || []).join(" "); $("ttMode").value = tt.mode || "draft";
    $("ttPrivacy").value = tt.privacy || "SELF_ONLY";
    $("disclaimer").value = m.disclaimer || "";
    updateCounts();
  }
  const approved = (v.status === "approved" || v.status === "published") && v.approval_valid;
  $("approveBtn").hidden = approved;
  $("approveBtn").disabled = v.status !== "rendered";
  $("unapproveBtn").hidden = v.status !== "approved";
  const pubJob = S.jobs.find((j) => j.kind === "publish" && j.ref.variant_id === v.id && (j.status === "running" || j.status === "queued"));
  $("publishBtn").disabled = !approved || !!pubJob;
  $("publishBtn").textContent = pubJob ? `Загрузка ${Math.round(pubJob.progress * 100)}%` : "Опубликовать одобренное";
  const gate = $("gateText");
  gate.className = "gatetext" + (approved ? " ok" : "");
  gate.textContent = v.status === "published" ? "Опубликовано. Можно отправить на другую платформу."
    : approved ? `Одобрено ${new Date(v.approved_at * 1000).toLocaleString("ru")}. Публикация разрешена.`
    : (v.status === "approved" && !v.approval_valid) ? "Файл или текст изменились после одобрения. Одобрите заново."
    : "Публикация доступна только после одобрения. Проверьте ролик и тексты, затем нажмите «Одобрить».";
  $("pubList").innerHTML = (v.publications || []).map((p) => {
    const label = p.platform === "youtube" ? "YouTube" : "TikTok";
    const st = { published: "опубликовано", draft_sent: "черновик отправлен в TikTok", processing: "обрабатывается", error: "ошибка" }[p.status] || p.status;
    const link = p.url ? ` <a href="${escapeAttr(p.url)}" target="_blank" rel="noopener">${escapeHtml(p.url)}</a>` : "";
    const err = p.error ? `<div class="error">${escapeHtml(p.error)}</div>` : "";
    return `<li>${label}: ${st}${link}${err}</li>`;
  }).join("");
}

function metaFromForm() {
  const v = currentVariant(), old = (v.metadata && v.metadata.disclaimer) || "", neu = $("disclaimer").value.trim();
  const swap = (text) => {
    if (old === neu) return text;
    if (old && text.includes(old)) return text.replace(old, neu);
    return neu ? text.trimEnd() + "\n\n" + neu : text;
  };
  return {
    platforms: [...document.querySelectorAll("input[name=platform]:checked")].map((c) => c.value),
    disclaimer: neu,
    youtube: { title: $("ytTitle").value.trim(), description: swap($("ytDesc").value), privacy: $("ytPrivacy").value,
      tags: $("ytTags").value.split(",").map((t) => t.trim()).filter(Boolean) },
    tiktok: { title: $("ttTitle").value.trim(), description: swap($("ttDesc").value), privacy: $("ttPrivacy").value, mode: $("ttMode").value,
      hashtags: $("ttTags").value.split(/[\s,]+/).map((t) => t.trim()).filter(Boolean).map((t) => (t.startsWith("#") ? t : "#" + t)) },
  };
}

function updateCounts() {
  document.querySelectorAll("output.count").forEach((o) => {
    const el = $(o.dataset.for), max = Number(o.dataset.max);
    const n = o.dataset.for === "ytTags" ? el.value.split(",").map((t) => t.trim()).filter(Boolean).join(",").length : el.value.length;
    o.textContent = `${n}/${max}`;
    o.classList.toggle("over", n > max);
  });
}

async function saveMeta() {
  const v = currentVariant();
  const upd = await api(`/api/variants/${v.id}/metadata`, { method: "PUT", body: metaFromForm() });
  metaFor = null;
  replaceVariant(upd);
  return upd;
}
function replaceVariant(upd) {
  const i = S.project.variants.findIndex((x) => x.id === upd.id);
  if (i >= 0) S.project.variants[i] = upd;
  renderProject();
}

/* ---------------------------------------------------------------- jobs */
let lastActive = 0;
async function pollJobs() {
  try { S.jobs = await api("/api/jobs"); } catch { return; }
  const active = S.jobs.filter((j) => j.status === "running" || j.status === "queued");
  const js = $("jobState");
  js.hidden = active.length === 0;
  js.textContent = active.length ? `${active[0].title}: ${Math.round(active[0].progress * 100)}%` + (active.length > 1 ? ` (ещё ${active.length - 1})` : "") : "";
  if (active.length || lastActive) {
    if (S.projectId) await loadProject(S.projectId);
    if (lastActive && !active.length) { await loadSources(); await loadTracks(); await loadProjects(); }
  }
  for (const j of S.jobs) {
    if (j.status === "error" && !j._seen && (j.kind === "scan_sources" || j.kind === "scan_music")) {
      j._seen = true;
    }
  }
  lastActive = active.length;
}
async function waitJob(id) {
  for (;;) {
    const j = await api(`/api/jobs/${id}`);
    if (j.status === "done" || j.status === "error") return j;
    await new Promise((r) => setTimeout(r, 700));
  }
}

/* ---------------------------------------------------------------- picker */
let pickMode = null;
const PICK = {
  source: { title: "Папка с исходниками", files: "" },
  output: { title: "Папка вывода", files: "" },
  png: { title: "PNG водяного знака", files: "png" },
  track: { title: "Файл трека", files: "mp3,wav,flac,m4a,aac,ogg,opus" },
};
async function openPicker(mode) {
  pickMode = mode;
  $("pickerTitle").textContent = PICK[mode].title;
  $("pickerOk").hidden = !!PICK[mode].files;
  const start = mode === "output" ? $("outputFolder").value : mode === "png" ? $("wmImage").value.replace(/[\\/][^\\/]*$/, "") : "";
  $("picker").showModal();
  await browse(start);
}
async function browse(path) {
  showErr($("pickerError"), null);
  let d;
  try { d = await api(`/api/fs/list?path=${encodeURIComponent(path || "")}&files=${PICK[pickMode].files}`); }
  catch (e) { showErr($("pickerError"), e); if (path) return browse(""); return; }
  $("pickerPath").value = d.path;
  const ul = $("pickerList");
  ul.innerHTML = "";
  const add = (label, cls, fn) => { const li = document.createElement("li"); const b = document.createElement("button"); b.type = "button"; b.textContent = label; if (cls) b.className = cls; b.onclick = fn; li.appendChild(b); ul.appendChild(li); };
  if (d.parent !== null) add("..", "", () => browse(d.parent));
  d.dirs.forEach((x) => add(x.name + "/", "", () => browse(x.path)));
  d.files.forEach((x) => add(x.name, "file", () => pickDone(x.path)));
}
async function pickDone(path) {
  if (!path) return;
  $("picker").close();
  if (pickMode === "source") {
    if (!S.cfg.source_folders.includes(path)) S.cfg = await api("/api/config", { method: "PUT", body: { source_folders: [...S.cfg.source_folders, path] } });
    renderFolders();
  } else if (pickMode === "output") { $("outputFolder").value = path; S.cfg = await api("/api/config", { method: "PUT", body: { output_folder: path } }); }
  else if (pickMode === "png") { $("wmImage").value = path; }
  else if (pickMode === "track") { renderTrackSelect(path); }
}

/* ---------------------------------------------------------------- accounts */
function renderAuth(auth) {
  const box = $("authBox");
  box.innerHTML = "";
  for (const [plat, st] of Object.entries(auth)) {
    const name = plat === "youtube" ? "YouTube" : "TikTok";
    const div = document.createElement("div");
    div.className = "acct";
    const state = st.authorized ? "подключён" : st.configured ? "ключи есть, нужен вход" : "нет ключей API";
    div.innerHTML = `<div class="head"><span>${name}</span><span class="state ${st.authorized ? "ok" : ""}">${state}</span></div>`;
    if (st.configured) {
      const row = document.createElement("div"); row.className = "row";
      row.innerHTML = `<a href="/api/auth/${plat}/start"><button type="button">${st.authorized ? "Переподключить" : "Подключить " + name}</button></a>`;
      div.appendChild(row);
      if (plat === "tiktok") {
        const lab = document.createElement("label"); lab.htmlFor = "ttCode"; lab.textContent = "Код TikTok или адрес после входа";
        const pf = document.createElement("div"); pf.className = "pathfield";
        pf.innerHTML = `<input id="ttCode" class="mono"><button type="button">Отправить</button>`;
        pf.querySelector("button").onclick = async () => {
          try { await api("/api/auth/tiktok/code", { method: "POST", body: { code: $("ttCode").value } }); renderAuth((await api("/api/state")).auth); }
          catch (e) { alert(e.message); }
        };
        div.append(lab, pf);
      }
    }
    if (!st.authorized && st.instructions) {
      const det = document.createElement("details");
      det.innerHTML = `<summary>Как подключить</summary><pre></pre>`;
      det.querySelector("pre").textContent = st.instructions;
      div.appendChild(det);
    }
    box.appendChild(div);
  }
}

/* ---------------------------------------------------------------- events */
document.querySelectorAll("input[name=wmType]").forEach((r) => (r.onchange = syncWm));
$("wmOpacity").oninput = syncWm; $("wmSize").oninput = syncWm;
document.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = () => openPicker(b.dataset.pick)));
$("pickerGo").onclick = () => browse($("pickerPath").value);
$("pickerPath").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); browse($("pickerPath").value); } };
$("pickerOk").onclick = () => pickDone($("pickerPath").value);
$("vibe").onchange = () => renderTrackSelect();
$("projectSelect").onchange = (e) => e.target.value && loadProject(Number(e.target.value));
$("clipAll").onchange = (e) => { S.selected = e.target.checked ? new Set(S.sources.map((s) => s.id)) : new Set(); loadSources(); };
["ytTitle", "ytTags"].forEach((id) => ($(id).oninput = updateCounts));

$("scanSources").onclick = async (e) => {
  e.target.disabled = true;
  try {
    const { job } = await api("/api/sources/scan", { method: "POST", body: { folders: S.cfg.source_folders } });
    const j = await waitJob(job);
    if (j.status === "error") alert(j.error);
    else if (j.result.errors.length) alert("Не удалось прочитать:\n" + j.result.errors.join("\n"));
    await loadSources();
  } finally { e.target.disabled = false; }
};
$("scanMusic").onclick = async (e) => {
  e.target.disabled = true;
  try { const { job } = await api("/api/music/scan", { method: "POST" }); await waitJob(job); await loadTracks(); }
  finally { e.target.disabled = false; }
};
$("outputFolder").onchange = async () => { S.cfg = await api("/api/config", { method: "PUT", body: { output_folder: $("outputFolder").value.trim() } }); };

$("createBtn").onclick = async (e) => {
  showErr($("createError"), null);
  e.target.disabled = true;
  try {
    const body = {
      name: $("projectName").value.trim(),
      source_ids: [...S.selected],
      output_folder: $("outputFolder").value.trim(),
      vibe: $("vibe").value,
      track: $("track").value,
      watermark: watermarkFromForm(),
      variants: Number($("variantCount").value),
      render: { resolution: $("resolution").value, fps: Number($("fps").value), duration: Number($("duration").value),
        quality: $("quality").value, encoder: $("encoder").value, variants: Number($("variantCount").value) },
    };
    const res = await api("/api/projects", { method: "POST", body });
    S.variantId = null;
    await loadProjects(res.project.id);
    pollJobs();
  } catch (err) { showErr($("createError"), err); }
  finally { e.target.disabled = false; }
};
$("moreBtn").onclick = async () => { await api(`/api/projects/${S.project.id}/variants`, { method: "POST", body: { count: 2 } }); await loadProject(S.project.id); };
$("rerenderBtn").onclick = async () => { await api(`/api/variants/${S.variantId}/render`, { method: "POST" }); shownFile = null; await loadProject(S.project.id); };

$("metaForm").onsubmit = async (e) => { e.preventDefault(); try { await saveMeta(); } catch (err) { showErr($("publishError"), err); } };
$("regenBtn").onclick = async () => { metaFor = null; replaceVariant(await api(`/api/variants/${S.variantId}/metadata/regenerate`, { method: "POST" })); };
$("approveBtn").onclick = async () => {
  showErr($("publishError"), null);
  try { await saveMeta(); replaceVariant(await api(`/api/variants/${S.variantId}/approve`, { method: "POST" })); }
  catch (err) { showErr($("publishError"), err); }
};
$("unapproveBtn").onclick = async () => replaceVariant(await api(`/api/variants/${S.variantId}/unapprove`, { method: "POST" }));
$("publishBtn").onclick = async () => {
  showErr($("publishError"), null);
  const v = currentVariant();
  const platforms = [...document.querySelectorAll("input[name=platform]:checked")].map((c) => c.value);
  if (!platforms.length) return showErr($("publishError"), new Error("Отметьте YouTube, TikTok или обе платформы."));
  const names = platforms.map((p) => (p === "youtube" ? "YouTube" : "TikTok")).join(" и ");
  if (!confirm(`Опубликовать вариант ${String(v.idx).padStart(2, "0")} на ${names}?`)) return;
  try {
    const { job } = await api(`/api/variants/${v.id}/publish`, { method: "POST", body: { platforms } });
    await pollJobs();
    const j = await waitJob(job);
    if (j.status === "error") showErr($("publishError"), new Error(j.error));
    else {
      const errs = Object.entries(j.result).filter(([, r]) => r.status === "error").map(([p, r]) => `${p}: ${r.error}`);
      if (errs.length) showErr($("publishError"), new Error(errs.join("\n")));
    }
    await loadProject(S.project.id);
  } catch (err) { showErr($("publishError"), err); }
};
window.addEventListener("resize", () => { const v = currentVariant(); if (v) drawTimeline(v.plan); });

init().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<p class="error">Не удалось загрузить состояние: ${escapeHtml(e.message)}</p>`); });
