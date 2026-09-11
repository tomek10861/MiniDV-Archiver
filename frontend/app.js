const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const scroller = document.querySelector('.overflow-y-auto');   // the scrolling content column
const gib = n => `${(n / 1024 ** 3).toFixed(1)} GiB`;
const mb = n => n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB` : `${(n / 1024 ** 2).toFixed(0)} MB`;
// PAL DV25 byte rate (144000 B/frame * 25 fps) -> how far into the tape a capture is
const dvDuration = bytes => {
  const s = Math.round((bytes || 0) / 3_600_000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h > 0 ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(sec).padStart(2, '0');
};
// coarse "1h 30m" / "8m 20s" duration, for processing ETAs (long, low-precision estimates)
const fmtDuration = totalSec => {
  const s = Math.max(0, Math.round(totalSec));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${sec}s` : `${sec}s`;
};
// per-scene progress + ETA for a processing job, "" if we don't have enough info yet
function processingProgress(j) {
  if (j.stage !== 'process' || !j.scene_total || !j.scene_index) return '';
  const pct = Math.min(100, Math.round(100 * j.scene_index / j.scene_total));
  let eta = '';
  if (j.processing_started_at) {
    const elapsed = (Date.now() - Date.parse(j.processing_started_at)) / 1000;
    if (elapsed > 0) {
      const remaining = (elapsed / j.scene_index) * (j.scene_total - j.scene_index);
      if (remaining > 1) eta = ' · ' + L('job.eta', { t: fmtDuration(remaining) });
    }
  }
  return ` · ${j.scene_index}/${j.scene_total} (${pct}%)${eta}`;
}
const TERMINAL = new Set(['COMPLETED', 'ERROR', 'CANCELLED']);
const stPL = s => (s ? L('state.' + s) : '—');
const durTxt = (frames, std) => {
  const t = Math.round(frames / (std === 'NTSC' ? 30000 / 1001 : 25));
  return `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`;
};
const badYear = d => d && !/^(19[89]\d|20[0-2]\d)-/.test(d);
const PL_MON = window.MON_SHORT;
function fmtTapeDates(t) {                       // iPhone-Photos style date / range
  const sane = d => /^(19[89]\d|20[0-2]\d)-\d\d-\d\d$/.test(d);
  if (sane(t.recording_date || '')) {
    const [y, m, d] = t.recording_date.split('-').map(Number);
    return `${d} ${PL_MON[m - 1]} ${y}`;
  }
  const ds = (t.scenes || []).map(s => (s.scene_id || '').slice(5, 15)).filter(sane).sort();
  if (!ds.length) return '';
  const [ay, am, ad] = ds[0].split('-').map(Number);
  const [by, bm, bd] = ds[ds.length - 1].split('-').map(Number);
  if (ds[0] === ds[ds.length - 1]) return `${ad} ${PL_MON[am - 1]} ${ay}`;
  if (ay !== by) return `${ay} – ${by}`;
  if (am !== bm) return `${ad} ${PL_MON[am - 1]} – ${bd} ${PL_MON[bm - 1]} ${ay}`;
  return `${ad}–${bd} ${PL_MON[am - 1]} ${ay}`;
}
async function api(path, opt) {
  const r = await fetch(path, opt); const j = await r.json();
  if (!r.ok) throw Error(j.error || r.statusText); return j;
}
// readable button / link styles (light + dark)
const BTN = 'rounded-lg border border-gray-300 bg-white px-2.5 py-1 text-theme-xs font-medium text-gray-700 hover:border-brand-500 hover:text-brand-500 dark:border-gray-600 dark:bg-white/[0.06] dark:text-gray-200 dark:hover:bg-white/[0.12]';
const BTN_FB = 'rounded-lg border border-blue-light-500 bg-blue-light-500/10 px-2.5 py-1 text-theme-xs font-medium text-blue-light-600 hover:bg-blue-light-500/20 dark:text-blue-light-400';
const BTN_DANGER = 'rounded-lg border border-error-500 bg-error-50 px-2.5 py-1 text-theme-xs font-medium text-error-600 hover:bg-error-100 dark:border-error-500/60 dark:bg-error-500/10 dark:text-error-400 dark:hover:bg-error-500/20';
const BTN_PRIMARY = 'rounded-lg bg-brand-500 px-3 py-1.5 text-theme-xs font-medium text-white hover:bg-brand-600';
const LINK = 'text-brand-500 hover:underline';
const BADGE = 'inline-flex items-center rounded-full px-2.5 py-0.5 text-theme-xs font-medium';

// ============ tape browser: album grid + detail ============
let openTapeId = null;
const selected = new Set();           // scene_ids selected in the open tape
const scenesCache = {};               // tape_id -> scenes[]

function tileMosaic(t) {
  const all = (t.scenes || []).map(s => s.scene_id);
  // like iPhone album covers: a few from the start, middle and end
  const n = all.length;
  const pick = n <= 4 ? all
    : [0, Math.round((n - 1) / 3), Math.round(2 * (n - 1) / 3), n - 1]
        .filter((v, i, a) => a.indexOf(v) === i).map(i => all[i]);
  const ids = pick;
  const cell = sid => sid
    ? `<img loading="lazy" class="h-full w-full object-cover" src="/api/tapes/${encodeURIComponent(t.tape_id)}/files/thumbnails/${encodeURIComponent(sid)}.jpg" alt="">`
    : `<div class="h-full w-full bg-gray-100 dark:bg-white/[0.04]"></div>`;
  const grid = ids.length <= 1
    ? cell(ids[0])
    : `<div class="grid h-full w-full grid-cols-2 grid-rows-2 gap-0.5">${[0, 1, 2, 3].map(i => cell(ids[i])).join('')}</div>`;
  return `<div class="aspect-square overflow-hidden rounded-lg bg-black">${grid}</div>`;
}

let gridSig = '';
function renderTapeGrid(list) {
  const sig = JSON.stringify(list.map(t => [t.tape_id, t.scene_count, t.capture_completed_at, t.label, t.recording_date]));
  if (sig !== gridSig) {
    gridSig = sig;
    $('#tapeGrid').innerHTML = list.map(t => {
      const date = fmtTapeDates(t);
      return `<button class="tile group rounded-xl border border-gray-200 p-2 text-left transition hover:border-brand-500 hover:shadow-theme-md dark:border-gray-800" data-id="${t.tape_id}">
        ${tileMosaic(t)}
        <div class="mt-2 px-0.5">
          <div class="truncate font-semibold text-gray-800 group-hover:text-brand-500 dark:text-white/90">${t.label || t.tape_id}</div>
          <div class="truncate text-theme-xs text-gray-500 dark:text-gray-400">${t.label ? t.tape_id + ' · ' : ''}${nScen(t.scene_count)}${date ? ` · ${date}` : ''}</div>
        </div>
      </button>`;
    }).join('') || `<p class="col-span-full text-theme-sm text-gray-400">${L('tapes.empty')}</p>`;
  }
  // keep an open detail view fresh
  if (openTapeId) {
    const t = list.find(x => x.tape_id === openTapeId);
    if (t) renderTapeDetail(t);
  }
}

function sceneCard(id, s, hasFull, hideSelect) {
  const enc = encodeURIComponent(id), sid = s.scene_id, f = s.files || {}, tc = s.timecode || {},
    rec = s.recording || {}, v = s.video || {}, arch = f.archive || {}, prox = f.proxy || {};
  const at = ((s.source || {}).start_frame || 0) / (v.standard === 'NTSC' ? 30000 / 1001 : 25);
  const drops = (s.capture || {}).dropped_frames || 0, disc = ((s.capture || {}).source_discontinuities || []).length;
  const flags = [drops && L('flag.dropped', { n: drops }), disc && L('flag.disc', { n: disc }),
    badYear(rec.datetime) && L('flag.badClock')].filter(Boolean);
  const file = n => `/api/tapes/${enc}/files/${encodeURIComponent(sid)}.${n}`;
  const on = !hideSelect && selected.has(sid);
  return `<div data-scene-card="${sid}" class="flex gap-4 rounded-xl border ${on ? 'border-brand-500 bg-brand-50/60 dark:bg-brand-500/[0.08]' : 'border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-white/[0.02]'} p-3">
    <label class="relative shrink-0 cursor-pointer">
      <img loading="lazy" src="/api/tapes/${enc}/files/thumbnails/${encodeURIComponent(sid)}.jpg" alt=""
        class="h-24 w-32 rounded-lg bg-black object-cover ${hasFull ? 'group-[.playable]:cursor-pointer' : ''}">
      ${hideSelect ? '' : `<input type="checkbox" class="scenesel absolute left-1.5 top-1.5 h-4 w-4 accent-brand-500" data-scene="${sid}" ${on ? 'checked' : ''}>`}
    </label>
    <div class="min-w-0 flex-1">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
        <strong class="text-gray-800 dark:text-white/90">${L('scene.n', { n: s.scene_index })}</strong>
        <span class="font-mono text-theme-xs text-gray-500 dark:text-gray-400">${tc.start || '?'} – ${tc.end || '?'} · ${durTxt(s.frame_count, v.standard)} · ${L('scene.frames', { n: s.frame_count })}</span>
      </div>
      <div class="mt-1 text-theme-xs text-gray-500 dark:text-gray-400">${v.standard || ''} ${v.resolution || ''} ${v.frame_rate || ''} ·
        ${L('scene.recorded', { dt: rec.datetime ? rec.datetime.replace('T', ' ') : '—' })} ·
        DV ${mb(arch.size_uncompressed || 0)} → zst ${mb(arch.size_compressed || 0)}${arch.verified_byte_for_byte ? ' ✓' : ''}</div>
      ${flags.length ? `<div class="mt-1 text-theme-xs text-orange-500">${flags.join(' · ')}</div>` : ''}
      <div class="mt-2 flex flex-wrap items-center gap-2 text-theme-xs">
        ${hasFull ? `<button class="seek ${BTN}" data-seek="${at.toFixed(2)}">${L('scene.seek')}</button>` : ''}
        <button class="preview ${BTN}" data-src="${file('mp4')}">${L('scene.preview')}</button>
        <button class="fb ${BTN_FB}" data-tape="${id}" data-scene="${sid}">${L('scene.fb')}</button>
        <button class="repair ${BTN}" data-tape="${id}" data-scene="${sid}">${L('scene.repair')}</button>
        <a class="${LINK}" href="${file('dv.zst')}?dl=1">${L('scene.dv')}${arch.size_compressed ? ` (${mb(arch.size_compressed)})` : ''}</a>
        <a class="${LINK}" href="${file('mp4')}?dl=1">${L('scene.mp4')}${prox.size ? ` (${mb(prox.size)})` : ''}</a>
        <a class="${LINK}" href="${file('json')}?dl=1">${L('scene.json')}</a>
      </div>
    </div></div>`;
}

let fromTimeline = null;              // {year, month} when a tape was opened from the timeline
async function openTape(id, sceneId) {
  const switching = openTapeId !== id;
  openTapeId = id;
  if (switching) { selected.clear(); detailSig = ''; }
  $('#tapeGrid').classList.add('hidden');
  $('#tapeDetail').classList.remove('hidden');
  if (switching || !scenesCache[id]) {
    $('#tapeDetail').innerHTML = `<p class="text-theme-sm text-gray-400">${L('loading')}</p>`;
    try {
      scenesCache[id] = await api(`/api/tapes/${encodeURIComponent(id)}/scenes`);
    } catch (e) { $('#tapeDetail').innerHTML = `<p class="text-error-500">${e.message}</p>`; return; }
  }
  if (openTapeId !== id) return;      // route changed while we awaited
  renderTapeDetail(currentTape(), true);
  focusScene(sceneId);
}
function focusScene(sid) {
  const host = $('#tapeDetail');
  if (!sid) { host.focus({ preventScroll: true }); scroller?.scrollTo({ top: 0 }); return; }
  requestAnimationFrame(() => {
    const card = host.querySelector(`[data-scene-card="${CSS.escape(sid)}"]`);
    if (!card) { scroller?.scrollTo({ top: 0 }); return; }
    card.scrollIntoView({ block: 'center', behavior: 'smooth' });
    card.classList.add('ring-2', 'ring-brand-500');
    setTimeout(() => card.classList.remove('ring-2', 'ring-brand-500'), 2200);
    const pv = card.querySelector('.preview');
    if (pv && !(card.nextElementSibling && card.nextElementSibling.tagName === 'VIDEO')) pv.click();
  });
}
function closeTape() {
  openTapeId = null; selected.clear(); detailSig = ''; fromTimeline = null;
  $('#tapeDetail').dataset.tape = '';
  $('#tapeGrid').classList.remove('hidden');
  $('#tapeDetail').classList.add('hidden');
}

// preserve any open <video> (scene previews + whole-tape player) across a re-render
function snapshotVideos() {
  const host = $('#tapeDetail'), snap = { previews: [], full: null };
  host.querySelectorAll('#detailScenes > video').forEach(v => {
    const sid = v.previousElementSibling && v.previousElementSibling.dataset.sceneCard;
    if (sid) snap.previews.push({ sid, src: v.src, t: v.currentTime, paused: v.paused });
  });
  const fv = host.querySelector('video.tapefull');
  if (fv) snap.full = { t: fv.currentTime, paused: fv.paused };
  return snap;
}
function restoreVideos(snap) {
  const host = $('#tapeDetail');
  const resume = (v, s) => {
    const go = () => { try { v.currentTime = s.t; } catch (_) {} if (!s.paused) v.play().catch(() => {}); };
    v.readyState >= 1 ? go() : v.addEventListener('loadedmetadata', go, { once: true });
  };
  if (snap.full) { tapePlayer(); const fv = host.querySelector('video.tapefull'); if (fv) resume(fv, snap.full); }
  snap.previews.forEach(p => {
    const card = host.querySelector(`[data-scene-card="${CSS.escape(p.sid)}"]`);
    if (!card || (card.nextElementSibling && card.nextElementSibling.tagName === 'VIDEO')) return;
    const v = document.createElement('video'); v.controls = true; v.preload = 'metadata';
    v.className = 'mt-2 w-full max-w-2xl rounded-lg bg-black'; v.src = p.src;
    card.after(v); resume(v, p);
  });
}

const PL_MON2 = window.MON_GEN;
let detailSig = '';
function renderTapeDetail(t, force) {
  const id = t.tape_id, e = encodeURIComponent(id), full = t.proxy_full;
  const scenes = scenesCache[id] || [];
  const ft = fromTimeline && fromTimeline.tape === id ? fromTimeline : null;
  const sig = JSON.stringify([id, t.scene_count, t.label, t.recording_date, !!full,
    (full || {}).size, scenes.length, [...selected].sort(), ft && [ft.year, ft.month]]);
  if (!force && sig === detailSig && $('#tapeDetail').dataset.tape === id) return;
  detailSig = sig;
  $('#tapeDetail').dataset.tape = id;
  const snap = snapshotVideos();
  $('#tapeDetail').innerHTML = `
    ${ft ? `<button id="backToTl" class="mb-2 inline-flex items-center gap-1 text-theme-xs text-gray-500 hover:text-brand-500 dark:text-gray-400">${L('fromTimeline', { month: (window.MON_GEN || [])[ft.month - 1], year: ft.year })}</button>` : ''}
    <div class="flex flex-wrap items-center gap-3">
      <h3 class="text-lg font-semibold text-brand-500">${id}</h3>
      ${t.label ? `<span class="rounded bg-gray-100 px-2 py-0.5 text-theme-xs text-gray-700 dark:bg-gray-800 dark:text-gray-200">🏷️ ${t.label}</span>` : ''}
      <span class="font-mono text-theme-xs text-gray-500 dark:text-gray-400">${nScen(t.scene_count ?? scenes.length)} · ${t.recording_date ? '📅 ' + t.recording_date : (t.capture_completed_at || '').slice(0, 19).replace('T', ' ')}</span>
    </div>
    <div class="mt-2 flex flex-wrap items-center gap-2 text-theme-xs">
      <button id="tapeRename" class="${BTN}">${L('tape.rename')}</button>
      <button id="tapeLabel" class="${BTN}">${L('tape.label')}</button>
      <button id="tapeDate" class="${BTN}">${L('tape.date')}</button>
    </div>
    <div class="tape-tools mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-theme-xs">
      ${full ? `<button class="fulltape ${BTN_PRIMARY}">${L('tape.playFull')}${full.size ? ` (${mb(full.size)})` : ''}</button>` : `<span class="text-gray-400">${L('tape.fullSoon')}</span>`}
      <a class="${LINK}" href="/api/tapes/${e}/files/tape.json?dl=1">tape.json</a>
      <a class="${LINK}" href="/api/tapes/${e}/files/tape.sha256?dl=1">tape.sha256</a>
      <a class="${LINK}" href="/api/tapes/${e}/files/capture.log?dl=1">capture.log</a>
      ${full ? `<a class="${LINK}" href="/api/tapes/${e}/files/tape.mp4?dl=1">⬇ tape.mp4</a>` : ''}
      ${full ? `<button class="fb ${BTN_FB}" data-tape="${id}">${L('tape.fbFull')}</button>` : ''}
      <button class="repair ${BTN}" data-tape="${id}">${L('tape.repairFull')}</button>
      <button class="reprobe ${BTN}" data-tape="${id}">${L('tape.reprobe')}</button>
      <button id="tapeDelete" class="${BTN_DANGER}">${L('tape.delete')}</button>
    </div>
    <div id="selBar" class="mt-3 hidden flex-wrap items-center gap-3 rounded-xl border border-brand-500/40 bg-brand-50/60 p-3 dark:bg-brand-500/[0.08]">
      <span class="text-sm">${L('sel.count')} <b id="selCount">0</b></span>
      <button id="selDownload" class="${BTN_PRIMARY}">${L('sel.download')}</button>
      <button id="selFb" class="${BTN_FB}">${L('sel.fb')}</button>
      <button id="selRepair" class="${BTN}">${L('sel.repair')}</button>
      <button id="selDelete" class="${BTN_DANGER}">${L('sel.delete')}</button>
      <button id="selAll" class="${BTN}">${L('sel.all')}</button>
      <button id="selClear" class="${BTN}">${L('sel.clear')}</button>
    </div>
    <div id="detailScenes" class="playable mt-4 grid gap-3">${scenes.map(s => sceneCard(id, s, !!full)).join('') || `<p class="text-theme-sm text-gray-400">${L('scenes.empty')}</p>`}</div>`;
  restoreVideos(snap);
  syncSelBar();
}

function syncSelBar() {
  const bar = $('#selBar'); if (!bar) return;
  bar.classList.toggle('hidden', selected.size === 0);
  bar.classList.toggle('flex', selected.size > 0);
  const c = $('#selCount'); if (c) c.textContent = selected.size;
}

function tapePlayer(seekTo) {
  const host = $('#tapeDetail'); if (!openTapeId) return;
  let v = host.querySelector('video.tapefull');
  if (!v) {
    v = document.createElement('video'); v.controls = true; v.preload = 'metadata';
    v.className = 'tapefull mt-3 w-full max-w-3xl rounded-lg bg-black';
    v.src = `/api/tapes/${encodeURIComponent(openTapeId)}/files/tape.mp4`;
    host.querySelector('.tape-tools').after(v);
  }
  if (seekTo != null) {
    const go = () => { try { v.currentTime = seekTo; } catch (_) {} v.play().catch(() => {}); };
    v.readyState >= 1 ? go() : v.addEventListener('loadedmetadata', go, { once: true });
    v.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

// on-demand build: POST to start, poll QUEUED/RUNNING, then download the temp file
async function pollBuild(url, body, btn, label, downloadUrl) {
  btn.disabled = true;
  try {
    let j = await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    while (j.status === 'QUEUED' || j.status === 'RUNNING') {
      btn.textContent = j.status === 'QUEUED' ? L('build.wait.queued') : L('build.wait.running');
      await new Promise(r => setTimeout(r, 2500));
      j = await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    }
    if (j.status !== 'READY') throw Error(j.error || L('build.fail'));
    const href = downloadUrl && downloadUrl(j);
    if (href) {
      btn.textContent = `${label}${j.size ? ` (${mb(j.size)})` : ''}`;
      const a = document.createElement('a'); a.href = href; document.body.appendChild(a); a.click(); a.remove();
    } else {
      btn.textContent = label + ' ✓';
    }
  } catch (e) { alert(e.message); btn.textContent = label; }
  finally { btn.disabled = false; }
}
function fbCompress(btn) { variantCompress(btn, { share: true }); }
function repairCompress(btn) { variantCompress(btn, { restore: true }); }
function variantCompress(btn, opt) {
  const tape = btn.dataset.tape, scene = btn.dataset.scene || null, label = btn.textContent;
  const base = scene ? `/api/tapes/${encodeURIComponent(tape)}/scenes/${encodeURIComponent(scene)}` : `/api/tapes/${encodeURIComponent(tape)}`;
  const tok = (scene ? encodeURIComponent(scene) : 'TAPE') + (opt.restore ? '-RES' : '');
  pollBuild(base + '/compress', opt.restore ? { restore: true } : null, btn, label,
    () => `/api/tapes/${encodeURIComponent(tape)}/compressed/${tok}.mp4`);
}
function downloadSelection(btn, opt) {
  if (!openTapeId || selected.size === 0) return;
  const tape = openTapeId, scenes = [...selected], label = btn.textContent;
  pollBuild(`/api/tapes/${encodeURIComponent(tape)}/compress`,
    { scenes, share: !!(opt && opt.share), restore: !!(opt && opt.restore) }, btn, label,
    j => `/api/tapes/${encodeURIComponent(tape)}/compressed/${j.token}.mp4`);
}

async function reloadOpenTape() {
  if (!openTapeId) return;
  try { scenesCache[openTapeId] = await api(`/api/tapes/${encodeURIComponent(openTapeId)}/scenes`); } catch (_) {}
  await refresh();
  if (openTapeId) renderTapeDetail(currentTape());
}
async function deleteWholeTape(id, count) {
  if (!confirm(L('confirm.deleteTape', { id, n: count }))) return;
  if (prompt(L('prompt.deleteTapeName', { id })) !== id) { alert(L('alert.nameMismatch')); return; }
  try {
    await api(`/api/tapes/${encodeURIComponent(id)}`, { method: 'DELETE' });
    go('#kasety'); await refresh();
  } catch (e) { alert(e.message); }
}
async function renameTape(id) {
  const nn = (prompt(L('prompt.rename'), id) || '').trim();
  if (!nn || nn === id) return;
  try {
    const r = await api(`/api/tapes/${encodeURIComponent(id)}/rename`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ new_id: nn }) });
    await refresh(); go('#kasety/' + encodeURIComponent(r.tape_id));
  } catch (e) { alert(e.message); }
}
async function editTapeMeta(id, field) {
  const cur = currentTape()[field] || '';
  const q = field === 'label' ? L('prompt.label') : L('prompt.date');
  const v = prompt(q, cur);
  if (v === null) return;
  try {
    await api(`/api/tapes/${encodeURIComponent(id)}/meta`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ [field]: v.trim() }) });
    await reloadOpenTape();
  } catch (e) { alert(e.message); }
}
async function deleteSelectedScenes() {
  if (!openTapeId || selected.size === 0) return;
  const n = selected.size;
  if (!confirm(L('confirm.deleteScenes', { n, tape: openTapeId }))) return;
  if (!confirm(L('confirm.irreversible'))) return;
  try {
    await api(`/api/tapes/${encodeURIComponent(openTapeId)}/scenes`,
      { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scenes: [...selected] }) });
    selected.clear();
    await reloadOpenTape();
  } catch (e) { alert(e.message); }
}

$('#tapeGrid').addEventListener('click', e => {
  const tile = e.target.closest('.tile'); if (tile) go('#kasety/' + encodeURIComponent(tile.dataset.id));
});
$('#tapeDetail').addEventListener('click', e => {
  if (e.target.id === 'backToTl' && fromTimeline) { go('#timeline/' + fromTimeline.year + '/' + fromTimeline.month); return; }
  const cb = e.target.closest('.scenesel');
  if (cb) {
    cb.checked ? selected.add(cb.dataset.scene) : selected.delete(cb.dataset.scene);
    e.target.closest('.flex')?.classList.toggle('border-brand-500', cb.checked);
    syncSelBar(); return;
  }
  if (e.target.id === 'selClear') { selected.clear(); renderTapeDetail(currentTape()); return; }
  if (e.target.id === 'selAll') { (scenesCache[openTapeId] || []).forEach(s => selected.add(s.scene_id)); renderTapeDetail(currentTape()); return; }
  if (e.target.id === 'selDownload') { downloadSelection(e.target, {}); return; }
  if (e.target.id === 'selFb') { downloadSelection(e.target, { share: true }); return; }
  if (e.target.id === 'selRepair') { downloadSelection(e.target, { restore: true }); return; }
  if (e.target.id === 'selDelete') { deleteSelectedScenes(); return; }
  if (e.target.id === 'tapeDelete') { deleteWholeTape(openTapeId, (scenesCache[openTapeId] || []).length); return; }
  if (e.target.id === 'tapeRename') { renameTape(openTapeId); return; }
  if (e.target.id === 'tapeLabel') { editTapeMeta(openTapeId, 'label'); return; }
  if (e.target.id === 'tapeDate') { editTapeMeta(openTapeId, 'recording_date'); return; }
  const rp = e.target.closest('.repair'); if (rp) { repairCompress(rp); return; }
  const rpb = e.target.closest('.reprobe');
  if (rpb) {
    pollBuild(`/api/tapes/${encodeURIComponent(rpb.dataset.tape)}/reprobe`, {}, rpb, rpb.textContent, null);
    return;
  }
  const fb = e.target.closest('.fb'); if (fb) { fbCompress(fb); return; }
  if (e.target.closest('.fulltape')) {
    const v = $('#tapeDetail').querySelector('video.tapefull');
    if (v) v.remove(); else tapePlayer();
    return;
  }
  const seeker = e.target.closest('[data-seek]'); if (seeker) { tapePlayer(parseFloat(seeker.dataset.seek)); return; }
  const p = e.target.closest('.preview');
  if (p) {
    const card = p.closest('.flex'); let v = card.nextElementSibling;
    if (v && v.tagName === 'VIDEO') { v.remove(); return; }
    v = document.createElement('video'); v.controls = true; v.preload = 'metadata';
    v.className = 'mt-2 w-full max-w-2xl rounded-lg bg-black'; v.src = p.dataset.src;
    card.after(v); v.play().catch(() => {});
  }
});
const currentTape = () => (window.__tapes || []).find(x => x.tape_id === openTapeId) || { tape_id: openTapeId };

// ============ timeline: years -> months -> scenes (iPhone-Photos style) ============
const PL_MON_FULL = window.MON_LONG;
const tlThumb = t => t && t.scene_id
  ? `<img loading="lazy" class="h-full w-full object-cover" src="/api/tapes/${encodeURIComponent(t.tape_id)}/files/thumbnails/${encodeURIComponent(t.scene_id)}.jpg" alt="">`
  : '<div class="grid h-full w-full place-items-center bg-gray-100 text-gray-300 dark:bg-white/[0.04]">—</div>';
const nScen = n => window.i18n.lang === 'pl'
  ? `${n} ${n === 1 ? 'scena' : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? 'sceny' : 'scen')}`
  : `${n} ${n === 1 ? 'scene' : 'scenes'}`;
let tl = { level: 'years', year: null, month: null, data: null, loading: false };
let tlScenes = [];                    // cached scene list for the currently shown month, time-sorted
// "tapeId|sceneId" -> {tape_id, scene_id, date, time}; a Map (not just a Set of keys) so a build can span
// several months' worth of picks without losing entries once the visited month's tlScenes gets replaced
const tlSelected = new Map();
const tlKey = (t, s) => t + '|' + s;

async function loadTimeline(force) {
  if (tl.loading) return;
  tl.loading = true;
  if (force || !tl.data) {
    try { tl.data = await api('/api/timeline'); }
    catch (e) { $('#tlView').innerHTML = `<p class="text-error-500">${e.message}</p>`; tl.loading = false; return; }
  }
  tl.loading = false;
  renderTimeline();
}

const TLTILE = 'tltile group rounded-xl border border-gray-200 p-2 text-left transition hover:border-brand-500 hover:shadow-theme-md dark:border-gray-800';
async function renderTimeline() {
  const view = $('#tlView');
  tl.level = tl.month ? 'scenes' : tl.year ? 'year' : 'years';

  if (tl.level === 'years') {
    const yrs = (tl.data && tl.data.years) || [];
    view.className = 'grid grid-cols-2 gap-4 p-5 sm:grid-cols-3 lg:grid-cols-4';
    view.innerHTML =
      (yrs.map(y => `<button data-go="#timeline/${y.year}" class="${TLTILE}">
        <div class="aspect-square overflow-hidden rounded-xl bg-black">${tlThumb(y.thumb)}</div>
        <div class="mt-2 px-0.5"><div class="text-lg font-bold text-gray-800 group-hover:text-brand-500 dark:text-white/90">${y.year}</div>
        <div class="text-theme-xs text-gray-500 dark:text-gray-400">${nScen(y.count)}</div></div>
      </button>`).join('') || `<p class="col-span-full text-theme-sm text-gray-400">${L('tl.noDated')}</p>`)
      + ((tl.data && tl.data.undated) ? `<button data-go="#kasety" class="${TLTILE}">
        <div class="grid aspect-square place-items-center rounded-xl bg-gray-100 text-4xl text-gray-300 dark:bg-white/[0.04]">?</div>
        <div class="mt-2 px-0.5"><div class="font-semibold text-gray-800 group-hover:text-brand-500 dark:text-white/90">${L('tl.noDate')}</div>
        <div class="text-theme-xs text-gray-500 dark:text-gray-400">${L('tl.noDateSub', { n: nScen(tl.data.undated) })}</div></div></button>` : '');
    return;
  }
  if (tl.level === 'year') {
    const y = ((tl.data || {}).years || []).find(x => x.year === tl.year) || { months: [] };
    view.className = 'grid grid-cols-2 gap-4 p-5 sm:grid-cols-3 lg:grid-cols-4';
    view.innerHTML = y.months.map(m => `<button data-go="#timeline/${tl.year}/${m.month}" class="${TLTILE}">
      <div class="aspect-square overflow-hidden rounded-xl bg-black">${tlThumb(m.thumb)}</div>
      <div class="mt-2 px-0.5"><div class="font-semibold text-gray-800 group-hover:text-brand-500 dark:text-white/90">${PL_MON_FULL[m.month - 1]}</div>
      <div class="text-theme-xs text-gray-500 dark:text-gray-400">${nScen(m.count)}</div></div>
    </button>`).join('') || `<p class="col-span-full text-theme-sm text-gray-400">${L('tl.noScenes')}</p>`;
    return;
  }
  // scenes: same scene-card list as a tape's detail view (Kasety), just spanning a month
  // across tapes — reuses sceneCard() as-is for the thumbnail/preview/FB/repair/download row.
  view.className = 'p-5';
  view.innerHTML = `<p class="text-theme-sm text-gray-400">${L('loading')}</p>`;
  try { tlScenes = await api(`/api/timeline/${tl.year}/${tl.month}`); }
  catch (e) { view.innerHTML = `<p class="text-error-500">${e.message}</p>`; return; }
  const tapeIds = [...new Set(tlScenes.map(s => s.tape_id))];
  await Promise.all(tapeIds.filter(id => !scenesCache[id]).map(async id => {
    try { scenesCache[id] = await api(`/api/tapes/${encodeURIComponent(id)}/scenes`); } catch (_) { scenesCache[id] = []; }
  }));
  renderTlScenes();
}

function renderTlScenes() {
  const view = $('#tlView');
  view.className = 'p-5';
  view.innerHTML = `
    <div id="tlSelBar" class="mb-4 hidden flex-wrap items-center gap-3 rounded-xl border border-brand-500/40 bg-brand-50/60 p-3 dark:bg-brand-500/[0.08]">
      <span class="text-sm">${L('sel.count')} <b id="tlSelCount">0</b></span>
      <button id="tlBuild" class="${BTN_PRIMARY}">${L('tl.build')}</button>
      <button id="tlSelClear" class="${BTN}">${L('sel.clear')}</button>
      <span class="basis-full text-theme-xs text-gray-500 dark:text-gray-400 sm:basis-auto">${L('tl.buildHint')}</span>
    </div>
    <div id="tlSceneList" class="playable grid gap-3">` +
    tlScenes.map(s => {
      const full = (scenesCache[s.tape_id] || []).find(x => x.scene_id === s.scene_id);
      if (!full) return '';
      const key = tlKey(s.tape_id, s.scene_id), sel = tlSelected.has(key);
      return `<div data-tl-key="${key}" class="rounded-xl ${sel ? 'ring-2 ring-brand-500' : ''}">
        <div class="mb-1 flex items-center gap-2 text-theme-xs text-gray-400">
          <input type="checkbox" class="tlsel h-3.5 w-3.5 accent-brand-500" data-tape="${s.tape_id}" data-scene="${s.scene_id}" ${sel ? 'checked' : ''}>
          <span class="truncate">${s.tape_id}${s.label ? ' · ' + s.label : ''} · ${s.date}${s.time ? ' ' + s.time.slice(0, 5) : ''}</span>
          <button data-open-tape="${encodeURIComponent(s.tape_id)}" data-open-scene="${encodeURIComponent(s.scene_id || '')}"
            title="${L('tl.openInTapes')}" class="shrink-0 text-brand-500 hover:underline">${L('tl.openInTapes')}</button>
        </div>
        ${sceneCard(s.tape_id, full, false, true)}
      </div>`;
    }).join('') + '</div>';
  syncTlSelBar();
}

function syncTlSelBar() {
  const bar = $('#tlSelBar'); if (!bar) return;
  bar.classList.toggle('hidden', tlSelected.size === 0);
  bar.classList.toggle('flex', tlSelected.size > 0);
  const c = $('#tlSelCount'); if (c) c.textContent = tlSelected.size;
}

async function buildTlPlaylist(btn) {
  if (tlSelected.size === 0) return;
  // chronological order (date+time), not click order or which month was open when picked
  const items = [...tlSelected.values()]
    .sort((a, b) => (a.date + 'T' + (a.time || '99:99:99')).localeCompare(b.date + 'T' + (b.time || '99:99:99')))
    .map(({ tape_id, scene_id }) => ({ tape_id, scene_id }));
  const label = btn.textContent;
  await pollBuild('/api/playlist/build', { items }, btn, label, j => `/api/playlist/build/${j.token}.mp4`);
}

$('#tlRefresh').onclick = () => loadTimeline(true);
$('#tlView').addEventListener('click', e => {
  const nav = e.target.closest('[data-go]');
  if (nav) { go(nav.dataset.go); return; }
  const cb = e.target.closest('.tlsel');
  if (cb) {
    const key = tlKey(cb.dataset.tape, cb.dataset.scene);
    if (cb.checked) {
      const s = tlScenes.find(x => x.tape_id === cb.dataset.tape && x.scene_id === cb.dataset.scene);
      tlSelected.set(key, { tape_id: cb.dataset.tape, scene_id: cb.dataset.scene, date: s?.date || '', time: s?.time || '' });
    } else {
      tlSelected.delete(key);
    }
    cb.closest('[data-tl-key]')?.classList.toggle('ring-2', cb.checked);
    cb.closest('[data-tl-key]')?.classList.toggle('ring-brand-500', cb.checked);
    syncTlSelBar();
    return;
  }
  if (e.target.id === 'tlSelClear') { tlSelected.clear(); renderTlScenes(); return; }
  if (e.target.id === 'tlBuild') { buildTlPlaylist(e.target); return; }
  // same interaction as a tape's own scene list (Kasety): inline preview toggle, FB, repair
  const rp = e.target.closest('.repair'); if (rp) { repairCompress(rp); return; }
  const fb = e.target.closest('.fb'); if (fb) { fbCompress(fb); return; }
  const p = e.target.closest('.preview');
  if (p) {
    const card = p.closest('.flex'); let v = card.nextElementSibling;
    if (v && v.tagName === 'VIDEO') { v.remove(); return; }
    v = document.createElement('video'); v.controls = true; v.preload = 'metadata';
    v.className = 'mt-2 w-full max-w-2xl rounded-lg bg-black'; v.src = p.dataset.src;
    card.after(v); v.play().catch(() => {});
    return;
  }
  const ot = e.target.closest('[data-open-tape]');
  if (ot) {
    fromTimeline = { year: tl.year, month: tl.month, tape: decodeURIComponent(ot.dataset.openTape) };
    const sc = ot.dataset.openScene;
    go('#kasety/' + ot.dataset.openTape + (sc ? '/' + sc : ''));
  }
});

// ============ duplicates: same recording across re-captures, keep the cleanest ============
let dupCache = null;
async function loadDuplicates(force) {
  if (force || !dupCache) {
    try { dupCache = await api('/api/duplicates'); }
    catch (e) { $('#dupView').innerHTML = `<p class="text-error-500">${e.message}</p>`; return; }
  }
  renderDuplicates();
}
function renderDuplicates() {
  const d = dupCache || { groups: [], unprobed: 0, scenes_indexed: 0 };
  const head = `<div class="mb-4 flex flex-wrap items-center gap-3 text-theme-xs text-gray-500 dark:text-gray-400">
    <span>${L('dup.summary', { g: d.groups.length, n: d.scenes_indexed, unp: d.unprobed ? L('dup.unprobed', { n: d.unprobed }) : '' })}</span>
    <button id="dupReprobeAll" class="${BTN}">${L('dup.scanAll')}</button>
    <button id="dupRefresh" class="${BTN}">${L('dup.refresh')}</button></div>`;
  const groups = d.groups.map((g, gi) => `
    <div class="mb-4 rounded-xl border border-gray-200 p-3 dark:border-gray-800">
      <div class="mb-2 text-theme-xs text-gray-500 dark:text-gray-400">${L('dup.groupCount', { n: g.count })}</div>
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      ${g.members.map((m, mi) => `
        <div class="rounded-lg border ${m.best ? 'border-success-500 bg-success-50/40 dark:bg-success-500/[0.08]' : 'border-gray-200 dark:border-gray-800'} p-2">
          <img loading="lazy" class="mb-2 aspect-video w-full rounded bg-black object-cover"
               src="/api/tapes/${encodeURIComponent(m.tape_id)}/files/thumbnails/${encodeURIComponent(m.scene_id)}.jpg" alt="">
          <div class="truncate text-theme-xs font-semibold text-gray-800 dark:text-white/90">${m.tape_id} · ${L('tl.scene', { n: m.scene_index })}</div>
          <div class="text-[11px] text-gray-500 dark:text-gray-400">${m.date || '—'} · ${L('dup.frames', { n: m.frame_count ?? '?' })}</div>
          <div class="text-[11px] ${m.error_score ? 'text-orange-500' : 'text-success-600 dark:text-success-400'}">
            ${L('dup.errors', { score: m.error_score ?? '?' })}${m.decode_errors != null ? ' · ' + L('dup.errBreakdown', { d: m.decode_errors, dr: m.dropped_frames || 0, di: m.discontinuities || 0 }) : ''}${m.best ? L('dup.best') : ''}</div>
          <div class="mt-1.5 flex flex-wrap gap-1.5">
            <button class="dupkeep ${BTN}" data-g="${gi}" data-m="${mi}">${L('dup.keep')}</button>
            <a class="${LINK} text-[11px]" href="#kasety/${encodeURIComponent(m.tape_id)}/${encodeURIComponent(m.scene_id)}">${L('dup.preview')}</a>
          </div>
        </div>`).join('')}
      </div>
    </div>`).join('') || `<p class="text-theme-sm text-gray-400">${L('dup.none')}${d.unprobed ? L('dup.noneHint') : ''}</p>`;
  $('#dupView').innerHTML = head + groups;
}
async function resolveDuplicate(gi, keepIdx) {
  const g = (dupCache.groups || [])[gi]; if (!g) return;
  const keep = g.members[keepIdx], drop = g.members.filter((_, i) => i !== keepIdx);
  if (!confirm(L('dup.confirmKeep', { keep: keep.tape_id, keepScore: keep.error_score ?? '?', n: drop.length,
      list: drop.map(m => L('dup.confirmItem', { tape: m.tape_id, idx: m.scene_index, score: m.error_score ?? '?' })).join('\n') }))) return;
  if (!confirm(L('confirm.irreversible'))) return;
  const byTape = {};
  drop.forEach(m => (byTape[m.tape_id] = byTape[m.tape_id] || []).push(m.scene_id));
  try {
    for (const [tid, sids] of Object.entries(byTape)) {
      await api(`/api/tapes/${encodeURIComponent(tid)}/scenes`,
        { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scenes: sids }) });
    }
    await refresh(); await loadDuplicates(true);
  } catch (e) { alert(e.message); }
}
$('#dupView').addEventListener('click', async e => {
  if (e.target.id === 'dupRefresh') { loadDuplicates(true); return; }
  if (e.target.id === 'dupReprobeAll') {
    e.target.disabled = true; e.target.textContent = L('build.wait.queued');
    let n = 0;
    try {
      for (const t of (window.__tapes || [])) {
        await api(`/api/tapes/${encodeURIComponent(t.tape_id)}/reprobe`,
          { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        n++;
      }
    } catch (err) { alert(err.message); }
    finally { e.target.disabled = false; e.target.textContent = L('dup.scanAll'); }
    await refresh();
    go('#zadania');            // show the queue that just started
    alert(L('dup.reprobeDone', { n }));
    return;
  }
  const k = e.target.closest('.dupkeep');
  if (k) resolveDuplicate(+k.dataset.g, +k.dataset.m);
});

// ============ jobs ============
function jobBadge(j, queue) {
  if (j.stage === 'capture') return [L('job.badge.capture'), 'bg-brand-500 text-white'];
  if (j.status === 'QUEUED' || queue.includes(j.tape_id)) return [L('job.badge.queued'), 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200'];
  if (j.status === 'COMPLETED') return [L('job.badge.done'), 'bg-success-50 text-success-700 dark:bg-success-500/15 dark:text-success-400'];
  if (j.status === 'ERROR') return [L('job.badge.error'), 'bg-error-50 text-error-700 dark:bg-error-500/15 dark:text-error-400'];
  if (j.status === 'CANCELLED') return [L('job.badge.cancelled'), 'bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300'];
  return [L('job.badge.processing'), 'bg-blue-light-50 text-blue-light-700 dark:bg-blue-light-500/15 dark:text-blue-light-400'];
}
const buildMode = m => L('build.mode.' + m) || m.toUpperCase();
const buildSt = st => L('build.st.' + st) || st;
const buildLabel = b => {
  const t = b.token || '';
  if (t.startsWith('REPROBE') || t.startsWith('SEL-') || ['TAPE', 'tape'].includes(t)) return b.tape_id;
  return `${b.tape_id} · ${t.replace(/-RES$/, '')}`;
};
function activeBuilds(builds) {
  return (builds || []).filter(b => b.status === 'QUEUED' || b.status === 'RUNNING'
    || (b.updated_at && Date.now() - Date.parse(b.updated_at) < 5 * 60 * 1000));
}
let jobsSig = '';
function renderJobs(list, queue, builds) {
  builds = activeBuilds(builds);
  const sig = JSON.stringify([list.map(j => [j.tape_id, j.status, j.current_scene, j.captured_bytes, j.scene_index]),
    builds.map(b => [b.key, b.status])]);
  if (sig === jobsSig) return; jobsSig = sig;
  const jobRows = list.map(j => {
    const [txt, cls] = jobBadge(j, queue), run = !TERMINAL.has(j.status);
    const clearable = j.status === 'CANCELLED' || j.status === 'ERROR';
    const progress = processingProgress(j);
    const sceneInfo = progress || (j.current_scene ? ` · ${j.current_scene}` : '');
    return `<div class="rounded-xl border border-gray-200 p-3 dark:border-gray-800">
      <div class="flex flex-wrap items-center gap-2">
        <span class="${BADGE} ${cls}">${txt}</span>
        <strong class="text-gray-800 dark:text-white/90">${j.tape_id}</strong>
        <span class="font-mono text-theme-xs text-gray-600 dark:text-gray-300">${stPL(j.status)}${sceneInfo}${j.captured_bytes ? ` · ${dvDuration(j.captured_bytes)} · ${mb(j.captured_bytes)}` : ''}${j.dropped_frames ? ' · ' + L('job.drop', { n: j.dropped_frames }) : ''}</span>
        <span class="ml-auto font-mono text-[11px] text-gray-400">${(j.updated_at || '').slice(11, 19)}</span>
        ${run ? `<button class="jstop ${BTN}" data-tape="${j.tape_id}">${L('job.stop')}</button>` : ''}
        ${clearable ? `<button class="jclear ${BTN_DANGER}" data-tape="${j.tape_id}" data-status="${j.status}">${L('job.clear')}</button>` : ''}
      </div>
      ${j.error ? `<div class="mt-1.5 text-theme-xs text-error-500">${j.error}</div>` : ''}
    </div>`;
  }).join('');
  const buildRows = builds.map(b => {
    const cls = b.status === 'ERROR' ? 'bg-error-50 text-error-700 dark:bg-error-500/15 dark:text-error-400'
      : b.status === 'READY' ? 'bg-success-50 text-success-700 dark:bg-success-500/15 dark:text-success-400'
      : b.status === 'QUEUED' ? 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200'
      : 'bg-blue-light-50 text-blue-light-700 dark:bg-blue-light-500/15 dark:text-blue-light-400';
    return `<div class="rounded-xl border border-gray-200 p-3 dark:border-gray-800">
      <div class="flex flex-wrap items-center gap-2">
        <span class="${BADGE} ${cls}">${buildMode(b.mode)}</span>
        <strong class="text-gray-800 dark:text-white/90">${buildLabel(b)}</strong>
        <span class="font-mono text-theme-xs text-gray-600 dark:text-gray-300">${buildSt(b.status)}</span>
        <span class="ml-auto font-mono text-[11px] text-gray-400">${(b.updated_at || '').slice(11, 19)}</span>
      </div>
      ${b.error ? `<div class="mt-1.5 text-theme-xs text-error-500">${b.error}</div>` : ''}
    </div>`;
  }).join('');
  $('#jobs').innerHTML = (jobRows + buildRows) || `<p class="text-theme-sm text-gray-400">${L('jobs.empty')}</p>`;
}
$('#jobs').addEventListener('click', async e => {
  const b = e.target.closest('.jstop');
  if (b) {
    if (!confirm(L('job.confirmStop', { tape: b.dataset.tape }))) return;
    try {
      await api('/api/capture/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tape_id: b.dataset.tape }) });
      refresh();
    } catch (err) { alert(err.message); }
    return;
  }
  const c = e.target.closest('.jclear');
  if (c) {
    if (!confirm(L('job.confirmClear', { tape: c.dataset.tape, status: stPL(c.dataset.status) }))) return;
    try {
      await api(`/api/jobs/${encodeURIComponent(c.dataset.tape)}`, { method: 'DELETE' });
      refresh();
    } catch (err) { alert(err.message); }
  }
});

// ============ logs ============
let logPickSig = '';
function renderLogs(s) {
  const jobs = s.jobs || [];
  const opts = [];
  if (s.capture) opts.push(['__capture__', L('log.opt.capture', { tape: s.capture.tape_id })]);
  jobs.forEach(j => opts.push([j.tape_id, `${j.tape_id} — ${stPL(j.status)}`]));
  const sig = JSON.stringify(opts.map(o => o[0]));
  const pick = $('#logPick');
  if (sig !== logPickSig) {
    logPickSig = sig;
    const cur = pick.value;
    pick.innerHTML = opts.map(([v, txt]) => `<option value="${v}">${txt}</option>`).join('') || `<option>${L('logs.none')}</option>`;
    if ([...pick.options].some(o => o.value === cur)) pick.value = cur;
  }
  const key = pick.value;
  const job = key === '__capture__' ? s.capture : jobs.find(j => j.tape_id === key);
  const txt = (job && job.logs) || (s.capture ? s.capture.logs : '') || '—';
  const view = $('#logView');
  if (view.textContent !== txt) {
    const nearBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
    view.textContent = txt;
    if ($('#logFollow').checked && nearBottom) view.scrollTop = view.scrollHeight;
  }
}
$('#logPick').addEventListener('change', () => { logPickSig = ''; refresh(); });

// ============ main refresh loop ============
async function refresh() {
  try {
    const [s, d, t] = await Promise.all([api('/api/status'), api('/api/storage'), api('/api/tapes')]);
    window.__tapes = t;
    const c = s.camera, on = c.connected;
    $('#camPill').textContent = on ? L('cam.pill.online') : L('cam.pill.offline');
    $('#camPill').className = `inline-flex items-center rounded-full border px-3 py-1.5 text-theme-xs font-medium ${on ? 'border-success-500 text-success-600 dark:text-success-400' : 'border-error-500 text-error-600 dark:text-error-400'}`;
    $('#stCam').textContent = on ? (c.model_name || L('card.camera')) : L('cam.offline');
    $('#stCamSub').textContent = on ? `${c.transport} · ${c.mode === 'manual' ? L('cam.mode.manual') : L('cam.mode.auto')}`
      : (c.guid ? L('cam.guid', { x: c.guid }) : L('cam.noDevice'));
    $('#sideCam').textContent = on ? `${c.model_name || 'camera'}\n${c.device || ''}` : L('cam.side.offline');

    $('#diskPill').textContent = L('disk.free', { x: gib(d.free) });
    $('#stFree').textContent = gib(d.free);
    $('#stBar').style.width = `${100 * d.used / d.total}%`;
    $('#stHours').textContent = L('disk.hours', { h: d.estimated_dv_hours, min: gib(d.min_free) });

    const active = (s.jobs || []).filter(j => !TERMINAL.has(j.status));
    const bq = (s.compress || []).filter(b => b.status === 'QUEUED' || b.status === 'RUNNING');
    const total = active.length + bq.length;
    $('#stJobs').textContent = total;
    $('#stJobsSub').textContent = s.processing ? L('jobs.sub.processing', { tape: s.processing.tape_id })
      : bq.length ? L('jobs.sub.background', { n: bq.length, what: buildMode(bq[0].mode).toLowerCase() })
      : (active.length ? L('jobs.sub.running') : L('jobs.sub.none'));
    $('#navJobs').textContent = total || '';
    $('#navJobs').hidden = !total;
    $('#stTapes').textContent = t.length;
    $('#navTapes').textContent = t.length || '';
    $('#navTapes').hidden = !t.length;

    // timeline is derived from the tape set — refresh it when that changes and it's on screen
    const tsig = JSON.stringify(t.map(x => [x.tape_id, x.scene_count, x.recording_date, x.label]));
    if (tapesSig && tsig !== tapesSig && currentRoute().section === 'timeline') { tl.data = null; loadTimeline(false); }
    tapesSig = tsig;

    const avc = !!c.avc_enabled, m = $('#manual');
    $('#avc').hidden = !avc; $('#manualHint').hidden = avc; $('#manualSteps').hidden = avc;
    if (!avc) { m.checked = true; m.disabled = true; } else m.disabled = false;

    const cap = s.capture;
    $('#capState').textContent = cap ? stPL(cap.status) : L('state.IDLE');
    $('#capLog').textContent = cap ? (cap.logs || '—')
      : (s.processing ? L('capture.background', { tape: s.processing.tape_id, status: stPL(s.processing.status) }) + processingProgress(s.processing) : L('capture.idle'));
    $('#start').disabled = !!cap;

    const capProgress = $('#capProgress'), hasProgress = !!(cap && cap.captured_bytes);
    capProgress.classList.toggle('hidden', !hasProgress);
    capProgress.classList.toggle('flex', hasProgress);
    if (hasProgress) {
      $('#capElapsed').textContent = dvDuration(cap.captured_bytes);
      $('#capBytes').textContent = mb(cap.captured_bytes);
    }

    renderJobs(s.jobs || [], s.queue || [], s.compress || []);
    if (currentRoute().section === 'duplikaty' && dupCache) {
      const busy = (s.compress || []).some(b => b.mode === 'reprobe' && (b.status === 'QUEUED' || b.status === 'RUNNING'));
      if (!busy && refresh._reprobeWasBusy) loadDuplicates(true);   // auto-refresh once the scans finish
      refresh._reprobeWasBusy = busy;
    }
    renderTapeGrid(t);
    renderLogs(s);
  } catch (e) {
    $('#camPill').textContent = L('apiError'); $('#capLog').textContent = e.message;
  }
}

$$('[data-action]').forEach(b => b.onclick = async () => {
  try { await api(`/api/tape/${b.dataset.action}`, { method: 'POST' }); refresh(); } catch (e) { alert(e.message); }
});
$('#start').onclick = async () => {
  try {
    const tape = $('#tape').value.trim(), manual = $('#manual').checked;
    const body = { manual_transport: manual, rewind: !manual };
    if (tape) body.tape_id = tape;
    await api('/api/capture/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    refresh();
  } catch (e) { alert(e.message); }
};
$('#cancel').onclick = () => api('/api/capture/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(refresh);

// ============ hash router: one section at a time, deep-linkable ============
const SECTIONS = ['pulpit', 'zadania', 'kasety', 'duplikaty', 'timeline', 'logi', 'info'];

function currentRoute() {
  const parts = location.hash.replace(/^#/, '').split('/').map(decodeURIComponent);
  const section = SECTIONS.includes(parts[0]) ? parts[0] : 'pulpit';
  const r = { section, tape: null, scene: null, year: null, month: null };
  if (section === 'kasety' && parts[1]) { r.tape = parts[1]; r.scene = parts[2] || null; }
  if (section === 'timeline' && /^\d{4}$/.test(parts[1] || '')) { r.year = +parts[1]; if (/^\d{1,2}$/.test(parts[2] || '')) r.month = +parts[2]; }
  return r;
}
function go(hash) {
  if (('#' + hash.replace(/^#/, '')) === location.hash) applyRoute();
  else location.hash = hash;                       // triggers hashchange -> applyRoute (+ history entry)
}
function renderSubbar() {
  const r = currentRoute(), bar = $('#subbar');
  let html = '';
  if (r.section === 'kasety' && r.tape) {
    html = `<button data-go="#kasety" class="inline-flex items-center gap-1 text-gray-600 hover:text-brand-500 dark:text-gray-300">${L('tape.crumbAll')}</button>
      <span class="text-gray-300 dark:text-gray-600">/</span><span class="truncate text-gray-700 dark:text-gray-200">${r.tape}${r.scene ? ' ' + L('tape.crumbScene') : ''}</span>`;
  } else if (r.section === 'timeline' && r.year) {
    html = `<button data-go="#timeline" class="text-gray-600 hover:text-brand-500 dark:text-gray-300">${L('tl.crumbAll')}</button>
      <span class="text-gray-300 dark:text-gray-600">/</span>`
      + (r.month
        ? `<button data-go="#timeline/${r.year}" class="text-gray-600 hover:text-brand-500 dark:text-gray-300">${r.year}</button>
           <span class="text-gray-300 dark:text-gray-600">/</span><span class="text-gray-700 dark:text-gray-200">${PL_MON_FULL[r.month - 1]}</span>`
        : `<span class="text-gray-700 dark:text-gray-200">${r.year}</span>`);
  }
  bar.innerHTML = html;
  bar.classList.toggle('hidden', !html);
  bar.classList.toggle('flex', !!html);
}
let tapesSig = '';
function applyRoute() {
  const r = currentRoute();
  if (r.section !== 'kasety') fromTimeline = null;
  SECTIONS.forEach(id => $('#' + id).classList.toggle('hidden', id !== r.section));
  $$('.navlink').forEach(a => {
    const on = a.getAttribute('href') === '#' + r.section;
    a.classList.toggle('menu-item-active', on);
    a.classList.toggle('menu-item-inactive', !on);
    const svg = a.querySelector('svg');
    svg?.classList.toggle('menu-item-icon-active', on);
    svg?.classList.toggle('menu-item-icon-inactive', !on);
  });
  $('#hdrLoc').textContent = L('sec.' + r.section);

  if (r.section === 'kasety') {
    if (r.tape) openTape(r.tape, r.scene);
    else { closeTape(); scroller?.scrollTo({ top: 0 }); }
  } else if (r.section === 'timeline') {
    tl.year = r.year; tl.month = r.month;
    loadTimeline(false);
    scroller?.scrollTo({ top: 0 });
  } else {
    if (r.section === 'duplikaty') loadDuplicates(false);
    scroller?.scrollTo({ top: 0 });
  }
  renderSubbar();
}
$('#subbar').addEventListener('click', e => {
  const b = e.target.closest('[data-go]'); if (b) go(b.dataset.go);
});
addEventListener('hashchange', applyRoute);

const _lp = $('#langPick'); if (_lp) { _lp.value = window.i18n.lang; _lp.addEventListener('change', () => window.i18n.setLang(_lp.value)); }
refresh().then(() => { if (!location.hash) location.hash = '#pulpit'; applyRoute(); });
setInterval(refresh, 3000);
