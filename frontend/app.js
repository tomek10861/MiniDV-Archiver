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
// "14.09 21:32" from an ISO datetime (UTC, matching every timestamp this app shows)
const fmtDT = iso => iso && iso.length >= 16 ? `${iso.slice(8, 10)}.${iso.slice(5, 7)} ${iso.slice(11, 16)}` : '';
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
// ============ workspace: keeps unrelated people's footage from mixing ============
let currentWorkspace = (() => { try { return localStorage.getItem('workspace') || 'default'; } catch (_) { return 'default'; } })();
const wsQS = () => currentWorkspace === 'default' ? '' : `?workspace=${encodeURIComponent(currentWorkspace)}`;
function setWorkspace(name) {
  currentWorkspace = name || 'default';
  try { localStorage.setItem('workspace', currentWorkspace); } catch (_) {}
  tl.data = null; tl.year = null; tl.month = null; dupCache = null; tapesSig = null;
  people.list = null; people.person = null; people.merging = false;
  if (currentRoute().section === 'osoby') go('#osoby');
  refresh();
}
async function loadWorkspaces() {
  let names;
  try { names = await api('/api/workspaces'); } catch (_) { names = ['default']; }
  if (!names.includes(currentWorkspace)) currentWorkspace = 'default';
  const sel = $('#workspacePick');
  sel.innerHTML = names.map(n => `<option value="${n}"${n === currentWorkspace ? ' selected' : ''}>${n === 'default' ? L('workspace.default') : n}</option>`).join('');
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

// ===== list / grid toggle + "iPhone Photos" tap-to-view modal, shared by Kasety and Timeline =====
let sceneView = localStorage.getItem('sceneView') === 'grid' ? 'grid' : 'list';
function setSceneView(v) {
  sceneView = v;
  try { localStorage.setItem('sceneView', v); } catch (_) {}
}
function viewToggleHTML() {
  const btn = (v, icon, label) => `<button data-view="${v}" title="${label}" class="viewToggle px-2.5 py-1 ${sceneView === v
    ? 'bg-brand-500 text-white' : 'bg-white text-gray-600 hover:text-brand-500 dark:bg-white/[0.06] dark:text-gray-300'}">${icon}</button>`;
  return `<div class="inline-flex overflow-hidden rounded-lg border border-gray-300 text-theme-xs dark:border-gray-600">
    ${btn('list', '☰', L('view.list'))}${btn('grid', '▦', L('view.grid'))}
  </div>`;
}
function sceneGridHTML(items) {   // items: [{tapeId, s}] -- s is a full scene record
  if (!items.length) return `<p class="text-theme-sm text-gray-400">${L('scenes.empty')}</p>`;
  return `<div class="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">` +
    items.map((it, i) => {
      const s = it.s, tc = s.timecode || {}, rec = s.recording || {};
      return `<button data-mkey="${i}" class="${TLTILE}">
        <div class="aspect-square overflow-hidden rounded-xl bg-black">
          <img loading="lazy" src="/api/tapes/${encodeURIComponent(it.tapeId)}/files/thumbnails/${encodeURIComponent(s.scene_id)}.jpg" alt=""
            class="h-full w-full object-cover">
        </div>
        <div class="mt-1.5 px-0.5">
          <div class="truncate text-theme-xs font-semibold text-gray-800 group-hover:text-brand-500 dark:text-white/90">${it.tapeId}</div>
          <div class="truncate text-[11px] text-gray-500 dark:text-gray-400">${rec.datetime ? rec.datetime.replace('T', ' ').slice(0, 16) : ''} · ${L('tl.scene', { n: s.scene_index })}</div>
          <div class="truncate font-mono text-[11px] text-gray-400">${tc.start || ''}${tc.end ? ' – ' + tc.end : ''}</div>
        </div>
      </button>`;
    }).join('') + '</div>';
}
let modalItems = [], modalIdx = -1, modalOpenTape = false, modalSeek = null;
function openSceneModal(items, idx, openTapeLink, seekTo) {
  modalItems = items; modalIdx = idx; modalOpenTape = !!openTapeLink;
  modalSeek = seekTo > 0 ? seekTo : null;          // People view: start where the person shows up
  $('#scModal').classList.remove('hidden'); $('#scModal').classList.add('flex');
  renderSceneModal();
}
function closeSceneModal() {
  $('#scModal').classList.add('hidden'); $('#scModal').classList.remove('flex');
  const v = $('#scModalVideo'); v.pause(); v.removeAttribute('src'); v.load();
  modalItems = []; modalIdx = -1;
}
function renderSceneModal() {
  if (modalIdx < 0 || modalIdx >= modalItems.length) return;
  const { tapeId, s } = modalItems[modalIdx];
  const enc = encodeURIComponent(tapeId), sid = s.scene_id, f = s.files || {}, tc = s.timecode || {},
    rec = s.recording || {}, v = s.video || {}, arch = f.archive || {}, prox = f.proxy || {};
  const file = n => `/api/tapes/${enc}/files/${encodeURIComponent(sid)}.${n}`;
  const video = $('#scModalVideo');
  video.pause(); video.src = file('mp4'); video.load();
  if (modalSeek != null) {
    const at = modalSeek; modalSeek = null;
    video.addEventListener('loadedmetadata', () => { try { video.currentTime = at; } catch (_) {} }, { once: true });
  }
  video.play().catch(() => {});
  $('#scModalTitle').innerHTML = `<div class="truncate">${tapeId} · ${L('scene.n', { n: s.scene_index })}</div>
    <div class="mt-0.5 truncate font-mono text-theme-xs font-normal text-gray-500 dark:text-gray-400">
      ${tc.start || '?'} – ${tc.end || '?'} · ${durTxt(s.frame_count, v.standard)} ·
      ${L('scene.recorded', { dt: rec.datetime ? rec.datetime.replace('T', ' ') : '—' })}
    </div>`;
  $('#scModalActions').innerHTML = `
    <button class="fb ${BTN_FB}" data-tape="${tapeId}" data-scene="${sid}">${L('scene.fb')}</button>
    <button class="repair ${BTN}" data-tape="${tapeId}" data-scene="${sid}">${L('scene.repair')}</button>
    <a class="${LINK}" href="${file('dv.zst')}?dl=1">${L('scene.dv')}${arch.size_compressed ? ` (${mb(arch.size_compressed)})` : ''}</a>
    <a class="${LINK}" href="${file('mp4')}?dl=1">${L('scene.mp4')}${prox.size ? ` (${mb(prox.size)})` : ''}</a>
    <a class="${LINK}" href="${file('json')}?dl=1">${L('scene.json')}</a>
    ${modalOpenTape ? `<button data-open-tape="${enc}" data-open-scene="${encodeURIComponent(sid)}" class="${BTN}">${L('tl.openInTapes')}</button>` : ''}`;
  $('#scModalPrev').disabled = modalIdx <= 0;
  $('#scModalNext').disabled = modalIdx >= modalItems.length - 1;
  $('#scModalCount').textContent = `${modalIdx + 1} / ${modalItems.length}`;
}
function scModalStep(delta) {
  const n = modalIdx + delta;
  if (n < 0 || n >= modalItems.length) return;
  modalIdx = n; renderSceneModal();
}
$('#scModalClose').onclick = closeSceneModal;
$('#scModalPrev').onclick = () => scModalStep(-1);
$('#scModalNext').onclick = () => scModalStep(1);
$('#scModal').addEventListener('click', e => {
  if (e.target.id === 'scModal') { closeSceneModal(); return; }
  const fb = e.target.closest('.fb'); if (fb) { fbCompress(fb); return; }
  const rp = e.target.closest('.repair'); if (rp) { repairCompress(rp); return; }
  const ot = e.target.closest('[data-open-tape]');
  if (ot) {
    closeSceneModal();
    fromTimeline = tl.year && tl.month ? { year: tl.year, month: tl.month, tape: decodeURIComponent(ot.dataset.openTape) } : fromTimeline;
    const sc = ot.dataset.openScene;
    go('#kasety/' + ot.dataset.openTape + (sc ? '/' + sc : ''));
  }
});
document.addEventListener('keydown', e => {
  if ($('#scModal').classList.contains('hidden')) return;
  if (e.key === 'Escape') closeSceneModal();
  else if (e.key === 'ArrowLeft') scModalStep(-1);
  else if (e.key === 'ArrowRight') scModalStep(1);
});
let scTouchX = null;
$('#scModal').addEventListener('touchstart', e => { scTouchX = e.touches[0].clientX; }, { passive: true });
$('#scModal').addEventListener('touchend', e => {
  if (scTouchX == null) return;
  const dx = e.changedTouches[0].clientX - scTouchX; scTouchX = null;
  if (Math.abs(dx) > 50) scModalStep(dx > 0 ? -1 : 1);
});

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

let kasetyGridItems = [];             // [{tapeId, s}] backing the open tape's grid view, for the modal's prev/next
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
      <span class="ml-auto">${viewToggleHTML()}</span>
    </div>
    <div class="mt-2 flex flex-wrap items-center gap-2 text-theme-xs">
      <button id="tapeRename" class="${BTN}">${L('tape.rename')}</button>
      <button id="tapeLabel" class="${BTN}">${L('tape.label')}</button>
      <button id="tapeDate" class="${BTN}">${L('tape.date')}</button>
    </div>
    ${repairOptsHTML()}
    <div class="tape-tools mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-theme-xs">
      ${full ? `<button class="fulltape ${BTN_PRIMARY}">${L('tape.playFull')}${full.size ? ` (${mb(full.size)})` : ''}</button>` : `<span class="text-gray-400">${L('tape.fullSoon')}</span>`}
      <a class="${LINK}" href="/api/tapes/${e}/files/tape.json?dl=1">tape.json</a>
      <a class="${LINK}" href="/api/tapes/${e}/files/tape.sha256?dl=1">tape.sha256</a>
      <a class="${LINK}" href="/api/tapes/${e}/files/capture.log?dl=1">capture.log</a>
      ${full ? `<a class="${LINK}" href="/api/tapes/${e}/files/tape.mp4?dl=1">⬇ tape.mp4</a>` : ''}
      ${full ? `<button class="fb ${BTN_FB}" data-tape="${id}">${L('tape.fbFull')}</button>` : ''}
      <button class="repair ${BTN}" data-tape="${id}">${L('tape.repairFull')}</button>
      <button class="reprobe ${BTN}" data-tape="${id}">${L('tape.reprobe')}</button>
      <button class="backfillMeta ${BTN}" data-tape="${id}">${L('tape.backfillMeta')}</button>
      <button id="tapeDelete" class="${BTN_DANGER}">${L('tape.delete')}</button>
    </div>
    <div id="selBar" class="mt-3 hidden flex-wrap items-center gap-3 rounded-xl border border-brand-500/40 bg-brand-50/60 p-3 dark:bg-brand-500/[0.08]">
      <span class="text-sm">${L('sel.count')} <b id="selCount">0</b></span>
      <button id="selDownload" class="${BTN_PRIMARY}">${L('sel.download')}</button>
      <button id="selFb" class="${BTN_FB}">${L('sel.fb')}</button>
      <button id="selRepair" class="${BTN}">${L('sel.repair')}</button>
      <button id="selArchive" class="${BTN}">${L('sel.archive')}</button>
      <button id="selDelete" class="${BTN_DANGER}">${L('sel.delete')}</button>
      <button id="selAll" class="${BTN}">${L('sel.all')}</button>
      <button id="selClear" class="${BTN}">${L('sel.clear')}</button>
    </div>
    <div id="detailScenes" class="playable mt-4 ${sceneView === 'grid' ? '' : 'grid gap-3'}">${
      sceneView === 'grid'
        ? sceneGridHTML(kasetyGridItems = scenes.map(s => ({ tapeId: id, s })))
        : (scenes.map(s => sceneCard(id, s, !!full)).join('') || `<p class="text-theme-sm text-gray-400">${L('scenes.empty')}</p>`)
    }</div>`;
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
// "Napraw" options: a shared module-level state, not read off one fixed DOM id —
// the checkboxes render on more than one page (tape detail, timeline), and a
// setting made on either has to be honored everywhere a repair gets triggered,
// including views that never render the checkboxes at all (the scene modal).
// (Regression: an id-based read silently fell back to "off" outside tape detail —
// the timeline's own "Napraw" always ran plain, with no indication why.)
let repairOpts = { stabilize: false, upscale: false };
function repairOpt() { return { ...repairOpts }; }
function repairOptsHTML() {
  return `<div class="repair-opts mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-theme-xs text-gray-500 dark:text-gray-400">
    <span>${L('repair.optsLabel')}</span>
    <label class="inline-flex items-center gap-1.5"><input type="checkbox" class="repairStabilizeBox h-3.5 w-3.5 accent-brand-500" ${repairOpts.stabilize ? 'checked' : ''}>${L('repair.stabilize')}</label>
    <label class="inline-flex items-center gap-1.5"><input type="checkbox" class="repairUpscaleBox h-3.5 w-3.5 accent-brand-500" ${repairOpts.upscale ? 'checked' : ''}>${L('repair.upscale')}</label>
  </div>`;
}
document.addEventListener('change', e => {
  if (e.target.matches('.repairStabilizeBox')) repairOpts.stabilize = e.target.checked;
  else if (e.target.matches('.repairUpscaleBox')) repairOpts.upscale = e.target.checked;
});
const restoreSuffix = (stabilize, upscale) => '-RES' + (stabilize ? '-STAB' : '') + (upscale ? '-UP' : '');
function fbCompress(btn) { variantCompress(btn, { share: true }); }
function repairCompress(btn) { variantCompress(btn, { restore: true, ...repairOpt() }); }
function variantCompress(btn, opt) {
  const tape = btn.dataset.tape, scene = btn.dataset.scene || null, label = btn.textContent;
  const base = scene ? `/api/tapes/${encodeURIComponent(tape)}/scenes/${encodeURIComponent(scene)}` : `/api/tapes/${encodeURIComponent(tape)}`;
  const tok = (scene ? encodeURIComponent(scene) : 'TAPE') + (opt.restore ? restoreSuffix(opt.stabilize, opt.upscale) : '');
  const body = opt.restore ? { restore: true, stabilize: !!opt.stabilize, upscale: !!opt.upscale } : null;
  pollBuild(base + '/compress', body, btn, label,
    () => `/api/tapes/${encodeURIComponent(tape)}/compressed/${tok}.mp4`);
}
function downloadSelection(btn, opt) {
  if (!openTapeId || selected.size === 0) return;
  const tape = openTapeId, scenes = [...selected], label = btn.textContent;
  const ro = opt && opt.restore ? repairOpt() : {};
  const ext = opt && opt.archive ? 'zip' : 'mp4';
  pollBuild(`/api/tapes/${encodeURIComponent(tape)}/compress`,
    { scenes, share: !!(opt && opt.share), restore: !!(opt && opt.restore), archive: !!(opt && opt.archive), ...ro }, btn, label,
    j => `/api/tapes/${encodeURIComponent(tape)}/compressed/${j.token}.${ext}`);
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
async function deleteSelectedScenes(btn) {
  if (!openTapeId || selected.size === 0) return;
  const n = selected.size;
  if (!confirm(L('confirm.deleteScenes', { n, tape: openTapeId }))) return;
  if (!confirm(L('confirm.irreversible'))) return;
  // runs as a background build (Zadania) — deleting scenes rebuilds the whole-tape
  // proxy, which for a long tape is too slow to do inline in the HTTP request
  const label = btn.textContent;
  await pollBuild(`/api/tapes/${encodeURIComponent(openTapeId)}/scenes/delete`,
    { scenes: [...selected] }, btn, label, null);
  selected.clear();
  await reloadOpenTape();
}

$('#tapeGrid').addEventListener('click', e => {
  const tile = e.target.closest('.tile'); if (tile) go('#kasety/' + encodeURIComponent(tile.dataset.id));
});
$('#tapeDetail').addEventListener('click', e => {
  if (e.target.id === 'backToTl' && fromTimeline) { go('#timeline/' + fromTimeline.year + '/' + fromTimeline.month); return; }
  const vt = e.target.closest('.viewToggle');
  if (vt) { setSceneView(vt.dataset.view); detailSig = ''; renderTapeDetail(currentTape(), true); return; }
  const gt = e.target.closest('[data-mkey]');
  if (gt) { openSceneModal(kasetyGridItems, +gt.dataset.mkey); return; }
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
  if (e.target.id === 'selArchive') { downloadSelection(e.target, { archive: true }); return; }
  if (e.target.id === 'selDelete') { deleteSelectedScenes(e.target); return; }
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
  const bfm = e.target.closest('.backfillMeta');
  if (bfm) {
    // OK = also recompute scenes that already have a date tag (fixes the old
    // timezone bug on tapes processed before it existed); Cancel = just fill gaps
    const force = confirm(L('tape.backfillForceConfirm'));
    pollBuild(`/api/tapes/${encodeURIComponent(bfm.dataset.tape)}/backfill-meta`, { force }, bfm, bfm.textContent, null);
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
    try { tl.data = await api(`/api/timeline${wsQS()}`); }
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
  try { tlScenes = await api(`/api/timeline/${tl.year}/${tl.month}${wsQS()}`); }
  catch (e) { view.innerHTML = `<p class="text-error-500">${e.message}</p>`; return; }
  const tapeIds = [...new Set(tlScenes.map(s => s.tape_id))];
  await Promise.all(tapeIds.filter(id => !scenesCache[id]).map(async id => {
    try { scenesCache[id] = await api(`/api/tapes/${encodeURIComponent(id)}/scenes`); } catch (_) { scenesCache[id] = []; }
  }));
  renderTlScenes();
}

let tlGridItems = [];                 // [{tapeId, s}] backing the month's grid view, for the modal's prev/next
function renderTlScenes() {
  const view = $('#tlView');
  view.className = 'p-5';
  const withFull = tlScenes.map(s => ({ s, full: (scenesCache[s.tape_id] || []).find(x => x.scene_id === s.scene_id) }))
    .filter(x => x.full);
  view.innerHTML = `
    ${repairOptsHTML()}
    <div class="mt-3 flex items-center justify-end">${viewToggleHTML()}</div>` +
    (sceneView === 'grid'
      ? sceneGridHTML(tlGridItems = withFull.map(({ s, full }) => ({ tapeId: s.tape_id, s: full })))
      : `<div id="tlSelBar" class="mb-4 hidden flex-wrap items-center gap-3 rounded-xl border border-brand-500/40 bg-brand-50/60 p-3 dark:bg-brand-500/[0.08]">
          <span class="text-sm">${L('sel.count')} <b id="tlSelCount">0</b></span>
          <button id="tlBuild" class="${BTN_PRIMARY}">${L('tl.build')}</button>
          <button id="tlBuildRepair" class="${BTN}">${L('tl.buildRepair')}</button>
          <button id="tlArchive" class="${BTN}">${L('sel.archive')}</button>
          <button id="tlSelClear" class="${BTN}">${L('sel.clear')}</button>
          <span class="basis-full text-theme-xs text-gray-500 dark:text-gray-400 sm:basis-auto">${L('tl.buildHint')}</span>
        </div>
        <div id="tlSceneList" class="playable grid gap-3">` + withFull.map(({ s, full }) => {
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
        }).join('') + '</div>');
  syncTlSelBar();
}

function syncTlSelBar() {
  const bar = $('#tlSelBar'); if (!bar) return;
  bar.classList.toggle('hidden', tlSelected.size === 0);
  bar.classList.toggle('flex', tlSelected.size > 0);
  const c = $('#tlSelCount'); if (c) c.textContent = tlSelected.size;
}

async function buildTlPlaylist(btn, opt) {
  if (tlSelected.size === 0) return;
  // chronological order (date+time), not click order or which month was open when picked
  const items = [...tlSelected.values()]
    .sort((a, b) => (a.date + 'T' + (a.time || '99:99:99')).localeCompare(b.date + 'T' + (b.time || '99:99:99')))
    .map(({ tape_id, scene_id }) => ({ tape_id, scene_id }));
  const label = btn.textContent;
  const ro = opt && opt.restore ? repairOpt() : {};
  const ext = opt && opt.archive ? 'zip' : 'mp4';
  await pollBuild('/api/playlist/build',
    { items, restore: !!(opt && opt.restore), archive: !!(opt && opt.archive), ...ro }, btn, label,
    j => `/api/playlist/build/${j.token}.${ext}`);
}

$('#tlRefresh').onclick = () => loadTimeline(true);
$('#tlView').addEventListener('click', e => {
  const nav = e.target.closest('[data-go]');
  if (nav) { go(nav.dataset.go); return; }
  const vt = e.target.closest('.viewToggle');
  if (vt) { setSceneView(vt.dataset.view); renderTlScenes(); return; }
  const gt = e.target.closest('[data-mkey]');
  if (gt) { openSceneModal(tlGridItems, +gt.dataset.mkey, true); return; }
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
  if (e.target.id === 'tlBuildRepair') { buildTlPlaylist(e.target, { restore: true }); return; }
  if (e.target.id === 'tlArchive') { buildTlPlaylist(e.target, { archive: true }); return; }
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
    try { dupCache = await api(`/api/duplicates${wsQS()}`); }
    catch (e) { $('#dupView').innerHTML = `<p class="text-error-500">${e.message}</p>`; return; }
  }
  renderDuplicates();
}
function renderDuplicates() {
  const d = dupCache || { groups: [], unprobed: 0, scenes_indexed: 0 };
  const head = `<div class="mb-4 flex flex-wrap items-center gap-3 text-theme-xs text-gray-500 dark:text-gray-400">
    <span>${L('dup.summary', { g: d.groups.length, n: d.scenes_indexed, unp: d.unprobed ? L('dup.unprobed', { n: d.unprobed }) : '' })}</span>
    ${d.groups.length ? `<button id="dupKeepBestAll" class="${BTN_PRIMARY}">${L('dup.keepBestAll')}</button>` : ''}
    <button id="dupReprobeAll" class="${BTN}">${L('dup.scanAll')}</button>
    <button id="dupRefresh" class="${BTN}">${L('dup.refresh')}</button></div>`;
  const groups = d.groups.map((g, gi) => `
    <div class="mb-4 rounded-xl border border-gray-200 p-3 dark:border-gray-800">
      <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span class="text-theme-xs text-gray-500 dark:text-gray-400">${L('dup.groupCount', { n: g.count })}</span>
        <button class="dupDismiss ${BTN}" data-g="${gi}" title="${L('dup.notDuplicateHint')}">${L('dup.notDuplicate')}</button>
      </div>
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
async function deleteDuplicateMembers(drop, confirmMsg) {
  if (!drop.length) return;
  if (!confirm(confirmMsg)) return;
  if (!confirm(L('confirm.irreversible'))) return;
  const byTape = {};
  drop.forEach(m => (byTape[m.tape_id] = byTape[m.tape_id] || []).push(m.scene_id));
  // Each tape's deletion runs as its own background build (Zadania) -- deleting
  // scenes rebuilds the whole-tape proxy, which for a long tape (hundreds of
  // scenes) is far too slow to do inline for every tape in one blocking loop, and
  // used to silently stop partway through leaving some tapes untouched. Here we
  // only enqueue (fast); the worker processes them one at a time in the background.
  try {
    for (const [tid, sids] of Object.entries(byTape)) {
      await api(`/api/tapes/${encodeURIComponent(tid)}/scenes/delete`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scenes: sids }) });
    }
    await refresh();
    go('#zadania');
  } catch (e) { alert(e.message); }
}
async function resolveDuplicate(gi, keepIdx) {
  const g = (dupCache.groups || [])[gi]; if (!g) return;
  const keep = g.members[keepIdx], drop = g.members.filter((_, i) => i !== keepIdx);
  await deleteDuplicateMembers(drop, L('dup.confirmKeep', { keep: keep.tape_id, keepScore: keep.error_score ?? '?', n: drop.length,
      list: drop.map(m => L('dup.confirmItem', { tape: m.tape_id, idx: m.scene_index, score: m.error_score ?? '?' })).join('\n') }));
}
async function dismissDuplicateGroup(gi, btn) {
  const g = (dupCache.groups || [])[gi]; if (!g) return;
  if (!confirm(L('dup.confirmDismiss', { n: g.count }))) return;
  btn.disabled = true;
  try {
    await api('/api/duplicates/dismiss', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ members: g.members.map(m => ({ tape_id: m.tape_id, scene_id: m.scene_id })) }) });
    await loadDuplicates(true);   // re-fetch: dismissal can change group indices, not just this one
  } catch (e) { alert(e.message); btn.disabled = false; }
}
async function resolveAllDuplicates() {
  const groups = dupCache.groups || [];
  const drop = [];
  // server already picks "best" per group (fewest errors, then longer recording);
  // if that's ever missing for some reason, fall back to keeping the first member
  for (const g of groups) {
    const keepIdx = g.members.findIndex(m => m.best);
    g.members.forEach((m, i) => { if (i !== (keepIdx === -1 ? 0 : keepIdx)) drop.push(m); });
  }
  await deleteDuplicateMembers(drop, L('dup.confirmKeepAll', { g: groups.length, n: drop.length }));
}
$('#dupView').addEventListener('click', async e => {
  if (e.target.id === 'dupRefresh') { loadDuplicates(true); return; }
  if (e.target.id === 'dupKeepBestAll') { resolveAllDuplicates(); return; }
  if (e.target.id === 'dupReprobeAll') {
    e.target.disabled = true; e.target.textContent = L('build.wait.queued');
    try {
      for (const t of (window.__tapes || [])) {
        await api(`/api/tapes/${encodeURIComponent(t.tape_id)}/reprobe`,
          { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      }
    } catch (err) { alert(err.message); }
    finally { e.target.disabled = false; e.target.textContent = L('dup.scanAll'); }
    await refresh();
    go('#zadania');            // show the queue that just started — no blocking alert() here:
    return;                    // it freezes the 3s refresh loop, so "live progress" wouldn't be live
  }
  const k = e.target.closest('.dupkeep');
  if (k) resolveDuplicate(+k.dataset.g, +k.dataset.m);
  const dm = e.target.closest('.dupDismiss');
  if (dm) dismissDuplicateGroup(+dm.dataset.g, dm);
});

// ============ people: faces found in the footage, named like iPhone Photos ============
// Scoped to the current workspace (the server never matches faces across workspaces).
let people = { list: null, showAll: false, person: null, merging: false, scan: null };
const faceThumb = id => id ? `/api/faces/thumb/${id}.jpg` : '';
const avatar = (id, cls) => id
  ? `<img loading="lazy" src="${faceThumb(id)}" alt="" class="${cls} rounded-full bg-gray-100 object-cover dark:bg-gray-800">`
  : `<div class="${cls} grid place-items-center rounded-full bg-gray-100 text-gray-300 dark:bg-gray-800">?</div>`;
const mmss = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
const nRec = n => window.i18n.lang === 'pl'
  ? `${n} ${n === 1 ? 'nagranie' : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? 'nagrania' : 'nagrań')}`
  : `${n} ${n === 1 ? 'recording' : 'recordings'}`;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const postJSON = (url, body) => api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

async function loadPeople(force) {
  if (force || !people.list) {
    try {
      const [list, st] = await Promise.all([api(`/api/persons${wsQS()}`), api(`/api/faces/status${wsQS()}`)]);
      people.list = list; people.scan = st;
    } catch (e) { $('#peopleView').innerHTML = `<p class="text-error-500">${e.message}</p>`; return; }
  }
  renderFaceBar();
  renderSubbar();
  const r = currentRoute();
  if (r.person) openPerson(r.person); else renderPeople();
}

function renderFaceBar() {
  const st = people.scan; if (!st) return;
  const b = st.scan, running = b && (b.status === 'QUEUED' || b.status === 'RUNNING');
  if (!st.available) { $('#faceBar').innerHTML = `<p class="text-theme-xs text-orange-500">${L('people.unavailable')}</p>`; return; }
  const pct = running && typeof b.progress === 'number' ? Math.round(b.progress * 100) : null;
  $('#faceBar').innerHTML = `<div class="flex flex-wrap items-center gap-3 text-theme-xs text-gray-500 dark:text-gray-400">
      <span>${L('people.scanned', { n: st.scanned, total: st.scenes })}</span>
      ${st.scan_at ? `<span>${L('people.auto', { at: st.scan_at })}</span>` : ''}
      ${running
        ? `<span class="font-mono text-gray-700 dark:text-gray-200">${b.status === 'QUEUED' ? L('build.st.QUEUED') : `${b.stage ? L('people.stage', { n: b.stage, name: L('people.stage' + b.stage) }) + ' · ' : ''}${pct ?? 0}%`}${b.detail ? ' · ' + esc(b.detail) : ''}${b.eta_s ? ' · ' + L('job.eta', { t: fmtDuration(b.eta_s) }) : ''}</span>
           <button id="faceScanStop" class="${BTN_DANGER}">${L('people.scanStop')}</button>`
        : `<button id="faceScanStart" class="${BTN_PRIMARY}">${L('people.scanNow')}</button>`}
    </div>
    ${running ? `<div class="mt-2 h-1.5 max-w-md overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-brand-500" style="width:${pct ?? 0}%"></div></div>` : ''}`;
}

function personTile(p) {
  return `<button data-go="#osoby/${p.id}" class="group flex flex-col items-center gap-1.5 rounded-xl p-2 text-center hover:bg-gray-50 dark:hover:bg-white/[0.04]">
    ${avatar(p.cover, 'h-24 w-24 transition group-hover:ring-2 group-hover:ring-brand-500')}
    <span class="max-w-full truncate text-theme-sm font-medium ${p.name ? 'text-gray-800 dark:text-white/90' : 'text-gray-400'}">${p.name ? esc(p.name) : L('people.addName')}</span>
    <span class="text-[11px] text-gray-400">${nRec(p.scenes)}</span>
  </button>`;
}

function renderPeople() {
  const view = $('#peopleView'), list = people.list || [];
  const named = list.filter(p => p.name && !p.hidden);
  const unnamed = list.filter(p => !p.name && !p.hidden).sort((a, b) => b.scenes - a.scenes || b.tracks - a.tracks);
  const often = unnamed.filter(p => p.scenes >= 2), rare = unnamed.filter(p => p.scenes < 2);
  const hidden = list.filter(p => p.hidden);
  const grid = ps => `<div class="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-8">${ps.map(personTile).join('')}</div>`;
  const head = (t, extra) => `<div class="mb-2 mt-6 flex flex-wrap items-center gap-3 first:mt-0"><h3 class="text-theme-sm font-semibold text-gray-700 dark:text-gray-200">${t}</h3>${extra || ''}</div>`;
  if (!list.length) {
    view.innerHTML = `<p class="text-theme-sm text-gray-400">${L('people.empty')}</p>`;
    return;
  }
  view.innerHTML =
    (named.length ? head(L('people.named', { n: named.length })) + grid(named) : '')
    + (often.length || rare.length ? head(L('people.who'), rare.length
        ? `<button id="peopleShowAll" class="${BTN}">${people.showAll ? L('people.hideRare') : L('people.showRare', { n: rare.length })}</button>` : '')
      + grid(people.showAll ? [...often, ...rare] : often)
      + (!often.length && !people.showAll ? `<p class="text-theme-xs text-gray-400">${L('people.onlyRare')}</p>` : '') : '')
    + (hidden.length ? head(L('people.hidden', { n: hidden.length })) + grid(hidden) : '');
}

async function openPerson(id) {
  const view = $('#peopleView');
  if (!people.person || people.person.id !== id) view.innerHTML = `<p class="text-theme-sm text-gray-400">${L('loading')}</p>`;
  let p;
  try { p = await api(`/api/persons/${id}`); }
  catch (e) { view.innerHTML = `<p class="text-error-500">${e.message}</p>`; return; }
  if (currentRoute().person !== id) return;
  // full scene records for the modal player (same source as Kasety / timeline)
  const tapeIds = [...new Set(p.scenes.map(s => s.tape_id))];
  await Promise.all(tapeIds.filter(t => !scenesCache[t]).map(async t => {
    try { scenesCache[t] = await api(`/api/tapes/${encodeURIComponent(t)}/scenes`); } catch (_) { scenesCache[t] = []; }
  }));
  people.person = p;
  renderPerson();
}

let personItems = [];                 // [{tapeId, s}] for the modal's prev/next, one per scene of this person
function renderPerson() {
  const p = people.person, view = $('#peopleView'), cover = p.tracks[0] && p.tracks[0].id;
  personItems = p.scenes.map(sc => ({ tapeId: sc.tape_id,
    s: (scenesCache[sc.tape_id] || []).find(x => x.scene_id === sc.scene_id) || { scene_id: sc.scene_id, scene_index: sc.scene_index } }));
  const others = (people.list || []).filter(o => o.id !== p.id);
  view.innerHTML = `
    <div class="flex flex-wrap items-center gap-4">
      ${avatar(cover, 'h-24 w-24')}
      <div class="min-w-0">
        <h3 class="truncate text-xl font-semibold ${p.name ? 'text-gray-800 dark:text-white/90' : 'text-gray-400'}">${p.name ? esc(p.name) : L('people.unnamed')}</h3>
        <div class="text-theme-xs text-gray-500 dark:text-gray-400">${nRec(p.scenes.length)}</div>
        <div class="mt-2 flex flex-wrap gap-2">
          <button id="personName" class="${p.name ? BTN : BTN_PRIMARY}">${p.name ? L('people.rename') : L('people.addName')}</button>
          <button id="personMerge" class="${BTN}">${L('people.merge')}</button>
          <button id="personHide" class="${BTN}">${p.hidden ? L('people.unhide') : L('people.hide')}</button>
        </div>
      </div>
    </div>
    ${people.merging ? `<div class="mt-4 rounded-xl border border-brand-500/40 bg-brand-50/60 p-3 dark:bg-brand-500/[0.08]">
        <div class="mb-2 flex items-center gap-3 text-theme-sm">${L('people.mergePick')}<button id="mergeCancel" class="${BTN}">${L('sel.clear')}</button></div>
        <div class="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-8">${others.map(o => `
          <button data-merge-into="${o.id}" class="flex flex-col items-center gap-1 rounded-xl p-2 hover:bg-white dark:hover:bg-white/[0.06]">
            ${avatar(o.cover, 'h-16 w-16')}
            <span class="max-w-full truncate text-theme-xs ${o.name ? 'text-gray-800 dark:text-white/90' : 'text-gray-400'}">${o.name ? esc(o.name) : nRec(o.scenes)}</span>
          </button>`).join('') || `<p class="text-theme-xs text-gray-400">${L('people.noOthers')}</p>`}</div>
      </div>` : ''}
    <h4 class="mb-2 mt-6 text-theme-sm font-semibold text-gray-700 dark:text-gray-200">${L('people.faces', { n: p.tracks.length })}</h4>
    <p class="mb-2 text-theme-xs text-gray-400">${L('people.facesHint')}</p>
    <div class="flex flex-wrap gap-2">${p.tracks.map(t => `
      <div class="relative">
        <button data-face-seek="${t.id}" title="${esc(t.tape_id)} · ${mmss(t.first)}">${avatar(t.id, 'h-16 w-16')}</button>
        ${p.tracks.length > 1 ? `<button data-face-out="${t.id}" title="${L('people.notThis')}" class="absolute -right-1 -top-1 grid h-5 w-5 place-items-center rounded-full bg-gray-800 text-[11px] text-white hover:bg-error-500">✕</button>` : ''}
      </div>`).join('')}</div>
    <h4 class="mb-2 mt-6 text-theme-sm font-semibold text-gray-700 dark:text-gray-200">${L('people.recordings')}</h4>
    <div class="grid gap-3">${p.scenes.map((sc, i) => `
      <div class="flex gap-4 rounded-xl border border-gray-200 bg-gray-50 p-3 dark:border-gray-800 dark:bg-white/[0.02]">
        <button data-play="${i}" data-at="${sc.segments[0] ? sc.segments[0][0] : 0}" class="shrink-0">
          <img loading="lazy" src="/api/tapes/${encodeURIComponent(sc.tape_id)}/files/thumbnails/${encodeURIComponent(sc.scene_id)}.jpg" alt="" class="h-24 w-32 rounded-lg bg-black object-cover">
        </button>
        <div class="min-w-0 flex-1">
          <div class="truncate font-semibold text-gray-800 dark:text-white/90">${esc(sc.label || sc.tape_id)} · ${L('tl.scene', { n: sc.scene_index ?? '?' })}</div>
          <div class="text-theme-xs text-gray-500 dark:text-gray-400">${sc.date || L('tl.noDate')}${sc.time ? ' ' + sc.time.slice(0, 5) : ''}${sc.label ? ' · ' + esc(sc.tape_id) : ''}</div>
          <div class="mt-2 flex flex-wrap items-center gap-1.5 text-theme-xs">
            <span class="text-gray-400">${L('people.seenAt')}</span>
            ${sc.segments.map(([a, b]) => `<button data-play="${i}" data-at="${a}" class="${BTN} font-mono">▶ ${mmss(a)}${b > a ? '–' + mmss(b) : ''}</button>`).join('')}
            <a class="${LINK} ml-2" href="#kasety/${encodeURIComponent(sc.tape_id)}/${encodeURIComponent(sc.scene_id)}">${L('tl.openInTapes')}</a>
          </div>
        </div>
      </div>`).join('')}</div>`;
}

async function personAction(fn) {
  try { await fn(); } catch (e) { alert(e.message); }
}
$('#faceBar').addEventListener('click', e => {
  if (e.target.id === 'faceScanStart') personAction(async () => { await postJSON('/api/faces/scan'); await loadPeople(true); refresh(); });
  if (e.target.id === 'faceScanStop' && confirm(L('people.confirmStop'))) personAction(async () => { await postJSON('/api/faces/scan/cancel'); await loadPeople(true); });
});
$('#peopleView').addEventListener('click', e => {
  const nav = e.target.closest('[data-go]'); if (nav) { go(nav.dataset.go); return; }
  if (e.target.id === 'peopleShowAll') { people.showAll = !people.showAll; renderPeople(); return; }
  const p = people.person;
  if (e.target.id === 'personName') {
    const name = prompt(L('people.namePrompt'), p.name || '');
    if (name === null) return;
    const clash = name.trim() && (people.list || []).find(o => o.id !== p.id && (o.name || '').toLowerCase() === name.trim().toLowerCase());
    if (clash && !confirm(L('people.confirmSameName', { name: clash.name }))) return;
    personAction(async () => {
      const r = await postJSON(`/api/persons/${p.id}`, { name: name.trim() });
      people.list = null; go('#osoby/' + r.id);     // r.id differs when the name merged two people
    });
    return;
  }
  if (e.target.id === 'personHide') {
    personAction(async () => { await postJSON(`/api/persons/${p.id}`, { hidden: !p.hidden }); people.person = null; await loadPeople(true); });
    return;
  }
  if (e.target.id === 'personMerge' || e.target.id === 'mergeCancel') { people.merging = e.target.id === 'personMerge'; renderPerson(); return; }
  const into = e.target.closest('[data-merge-into]');
  if (into) {
    const dst = (people.list || []).find(o => o.id === +into.dataset.mergeInto);
    if (!confirm(L('people.confirmMerge', { name: dst && dst.name ? dst.name : L('people.unnamed') }))) return;
    personAction(async () => {
      const r = await postJSON(`/api/persons/${p.id}/merge`, { into: +into.dataset.mergeInto });
      people.merging = false; people.list = null; go('#osoby/' + r.id);   // p.id no longer exists
    });
    return;
  }
  const out = e.target.closest('[data-face-out]');
  if (out) {
    if (!confirm(L('people.confirmNotThis'))) return;
    personAction(async () => { await postJSON(`/api/faces/tracks/${out.dataset.faceOut}/move`, { person_id: null }); people.list = null; await loadPeople(true); });
    return;
  }
  const fs = e.target.closest('[data-face-seek]');
  if (fs) {
    const t = p.tracks.find(x => x.id === +fs.dataset.faceSeek);
    const i = t ? p.scenes.findIndex(sc => sc.tape_id === t.tape_id && sc.scene_id === t.scene_id) : -1;
    if (i >= 0) openSceneModal(personItems, i, true, t.first);
    return;
  }
  const pl = e.target.closest('[data-play]');
  if (pl) {
    const i = +pl.dataset.play;
    if (personItems[i]) openSceneModal(personItems, i, true, +pl.dataset.at);
  }
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
  if (b.mode === 'faces') return L('build.facesLabel');
  if (t.startsWith('REPROBE') || t === 'METABACKFILL' || t.startsWith('SEL-') || t.startsWith('DEL-') || ['TAPE', 'tape'].includes(t)) return b.tape_id;
  return `${b.tape_id} · ${t.replace(/-RES$/, '')}`;
};
const BUILD_DOWNLOADABLE = new Set(['concat', 'restore', 'share', 'archive']);
function buildDownloadUrl(b) {
  if (!BUILD_DOWNLOADABLE.has(b.mode)) return null;    // reprobe/backfill_meta/delete produce no file
  const ext = b.mode === 'archive' ? 'zip' : 'mp4';
  const base = b.tape_id === '_playlist' ? '/api/playlist/build'
    : `/api/tapes/${encodeURIComponent(b.tape_id)}/compressed`;
  return `${base}/${encodeURIComponent(b.token)}.${ext}`;
}
function activeBuilds(builds) {
  return (builds || []).filter(b => b.status === 'QUEUED' || b.status === 'RUNNING'
    || (b.updated_at && Date.now() - Date.parse(b.updated_at) < 5 * 60 * 1000));
}
let jobsSig = '';
function renderJobs(list, queue, builds) {
  builds = activeBuilds(builds);
  const sig = JSON.stringify([list.map(j => [j.tape_id, j.status, j.current_scene, j.captured_bytes, j.scene_index]),
    builds.map(b => [b.key, b.status, b.progress, b.detail])]);
  if (sig === jobsSig) return; jobsSig = sig;
  const jobRows = list.map(j => {
    const [txt, cls] = jobBadge(j, queue), run = !TERMINAL.has(j.status);
    const clearable = j.status === 'CANCELLED' || j.status === 'ERROR';
    const progress = processingProgress(j);
    const sceneInfo = progress || (j.current_scene ? ` · ${j.current_scene}` : '');
    return `<div class="jrow cursor-pointer rounded-xl border border-gray-200 p-3 hover:border-brand-500 dark:border-gray-800 dark:hover:border-brand-500" data-tape="${j.tape_id}" title="${L('job.viewLogs')}">
      <div class="flex flex-wrap items-center gap-2">
        <span class="${BADGE} ${cls}">${txt}</span>
        <strong class="text-gray-800 dark:text-white/90">${j.tape_id}</strong>
        <span class="font-mono text-theme-xs text-gray-600 dark:text-gray-300">${stPL(j.status)}${sceneInfo}${j.captured_bytes ? ` · ${dvDuration(j.captured_bytes)} · ${mb(j.captured_bytes)}` : ''}${j.dropped_frames ? ' · ' + L('job.drop', { n: j.dropped_frames }) : ''}</span>
        <span class="ml-auto font-mono text-[11px] text-gray-400" title="${L('job.created')}">${fmtDT(j.created_at)}</span>
        <span class="font-mono text-[11px] text-gray-400">${(j.updated_at || '').slice(11, 19)}</span>
        ${run ? `<button class="jstop ${BTN}" data-tape="${j.tape_id}">${L('job.stop')}</button>` : ''}
        ${clearable ? `<button class="jclear ${BTN_DANGER}" data-tape="${j.tape_id}" data-status="${j.status}">${L('job.clear')}</button>` : ''}
      </div>
      ${j.error ? `<div class="mt-1.5 text-theme-xs text-error-500">${j.error}</div>` : ''}
    </div>`;
  }).join('');
  const buildRows = builds.map(b => {
    const cls = b.status === 'ERROR' ? 'bg-error-50 text-error-700 dark:bg-error-500/15 dark:text-error-400'
      : b.status === 'READY' ? 'bg-success-50 text-success-700 dark:bg-success-500/15 dark:text-success-400'
      : b.status === 'QUEUED' || b.status === 'CANCELLED' ? 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200'
      : 'bg-blue-light-50 text-blue-light-700 dark:bg-blue-light-500/15 dark:text-blue-light-400';
    const pct = b.status === 'RUNNING' && typeof b.progress === 'number' ? Math.round(b.progress * 100) : null;
    const dlUrl = b.status === 'READY' ? buildDownloadUrl(b) : null;
    const live = b.status === 'QUEUED' || b.status === 'RUNNING';
    // the face scan reports which scene it's on, counts and an ETA, and can be stopped
    const extra = b.mode === 'faces' ? [live && b.stage && L('people.stage', { n: b.stage, name: L('people.stage' + b.stage) }),
      b.detail && esc(b.detail),
      live && b.total != null && (b.stage === 2 ? L('build.facesRefine', { done: b.done ?? 0, total: b.total })
        : L('build.facesCount', { done: b.done ?? 0, total: b.total, faces: b.faces ?? 0 })),
      live && b.eta_s && L('job.eta', { t: fmtDuration(b.eta_s) })].filter(Boolean).join(' · ') : '';
    return `<div class="rounded-xl border border-gray-200 p-3 dark:border-gray-800">
      <div class="flex flex-wrap items-center gap-2">
        <span class="${BADGE} ${cls}">${buildMode(b.mode)}</span>
        <strong class="text-gray-800 dark:text-white/90">${buildLabel(b)}</strong>
        <span class="font-mono text-theme-xs text-gray-600 dark:text-gray-300">${buildSt(b.status)}${pct != null ? ` · ${pct}%` : ''}${b.size ? ` · ${mb(b.size)}` : ''}</span>
        ${dlUrl ? `<a class="${BTN_PRIMARY}" href="${dlUrl}" download>${L('build.download')}</a>` : ''}
        <span class="ml-auto font-mono text-[11px] text-gray-400">${(b.updated_at || '').slice(11, 19)}</span>
        ${b.mode === 'faces' && live ? `<button class="bcancel ${BTN}">${L('job.stop')}</button>` : ''}
      </div>
      ${extra ? `<div class="mt-1 font-mono text-theme-xs text-gray-500 dark:text-gray-400">${extra}</div>` : ''}
      ${pct != null ? `<div class="mt-2 h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-brand-500" style="width:${pct}%"></div></div>` : ''}
      ${b.error ? `<div class="mt-1.5 text-theme-xs text-error-500">${b.error}</div>` : ''}
    </div>`;
  }).join('');
  $('#jobs').innerHTML = (jobRows + buildRows) || `<p class="text-theme-sm text-gray-400">${L('jobs.empty')}</p>`;
}
$('#jobs').addEventListener('click', async e => {
  if (e.target.closest('.bcancel')) {
    if (!confirm(L('people.confirmStop'))) return;
    try { await postJSON('/api/faces/scan/cancel'); refresh(); } catch (err) { alert(err.message); }
    return;
  }
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
    return;
  }
  const row = e.target.closest('.jrow');
  if (row) {
    go('#logi');
    $('#logPick').value = row.dataset.tape;
    refresh();
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

// ============ sysload footer widget ============
function loadPill(value, cores) {
  const ratio = value / cores;
  return ratio < 0.7 ? ['bg-success-50 text-success-700 dark:bg-success-500/15 dark:text-success-400', L('sysload.ok')]
    : ratio <= 1.0 ? ['bg-warning-50 text-warning-700 dark:bg-warning-500/15 dark:text-warning-400', L('sysload.busy')]
    : ['bg-error-50 text-error-700 dark:bg-error-500/15 dark:text-error-400', L('sysload.high')];
}
function renderSysload(sys) {
  if (!sys || !sys.load) return;
  $('#sysCores').textContent = L('sysload.cores', { n: sys.cores });
  [['1', sys.load['1m']], ['5', sys.load['5m']], ['15', sys.load['15m']]].forEach(([k, v]) => {
    $(`#loadV${k}`).textContent = v.toFixed(2);
    const [cls, label] = loadPill(v, sys.cores);
    $(`#loadP${k}`).className = `rounded-full px-2 py-0.5 text-[11px] font-medium ${cls}`;
    $(`#loadP${k}`).textContent = label;
  });
  const cpu = sys.cpu;
  $('#cpuUser').style.width = cpu.user + '%';
  $('#cpuSystem').style.width = cpu.system + '%';
  $('#cpuIowait').style.width = cpu.iowait + '%';
  $('#cpuUserPct').textContent = cpu.user.toFixed(1) + '%';
  $('#cpuSystemPct').textContent = cpu.system.toFixed(1) + '%';
  $('#cpuIowaitPct').textContent = cpu.iowait.toFixed(1) + '%';
  $('#cpuIdlePct').textContent = cpu.idle.toFixed(1) + '%';
  const m = sys.memory;
  $('#memSummary').textContent = `${gib(m.used)} / ${gib(m.total)}`;
  $('#memUsed').style.width = (m.total ? 100 * m.used / m.total : 0) + '%';
  $('#memCache').style.width = (m.total ? 100 * m.buff_cache / m.total : 0) + '%';
  $('#memUsedVal').textContent = gib(m.used);
  $('#memCacheVal').textContent = gib(m.buff_cache);
  $('#memFreeVal').textContent = gib(m.free);
  const swapLine = $('#swapLine');
  swapLine.classList.toggle('hidden', !m.swap_total);
  if (m.swap_total) swapLine.textContent = L('sysload.swap', { used: gib(m.swap_used), total: gib(m.swap_total) });
}

// ============ main refresh loop ============
async function refresh() {
  try {
    const [s, d, t] = await Promise.all([api('/api/status'), api('/api/storage'), api(`/api/tapes${wsQS()}`)]);
    window.__tapes = t;
    api('/api/sysload').then(renderSysload).catch(() => {});  // best-effort footer widget, never blocks the dashboard
    $('#captureTzDefault').textContent = L('capture.tzDefault', { tz: s.default_recording_tz || 'Europe/Warsaw' });
    if (!$('#workspacePick').options.length) loadWorkspaces();
    $('#captureWsNote').textContent = currentWorkspace === 'default' ? '' : L('capture.wsNote', { ws: currentWorkspace });
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

    // live preview: only (re)connect the MJPEG stream on CAPTURING onset/exit, not every 3s tick
    const capPreview = $('#capPreview'), showPreview = !!(cap && cap.status === 'CAPTURING');
    $('#capPreviewWrap').classList.toggle('hidden', !showPreview);
    if (showPreview && !capPreview.dataset.live) {
      capPreview.dataset.live = '1';
      capPreview.onerror = () => { delete capPreview.dataset.live; };
      capPreview.src = '/api/capture/preview.mjpg?t=' + Date.now();
    } else if (!showPreview && capPreview.dataset.live) {
      delete capPreview.dataset.live;
      capPreview.removeAttribute('src');
    }

    renderJobs(s.jobs || [], s.queue || [], s.compress || []);
    if (currentRoute().section === 'duplikaty' && dupCache) {
      const busy = (s.compress || []).some(b => b.mode === 'reprobe' && (b.status === 'QUEUED' || b.status === 'RUNNING'));
      if (!busy && refresh._reprobeWasBusy) loadDuplicates(true);   // auto-refresh once the scans finish
      refresh._reprobeWasBusy = busy;
    }
    if (currentRoute().section === 'osoby') {
      const fb = (s.compress || []).find(b => b.mode === 'faces');
      const fsig = fb ? [fb.status, fb.progress, fb.detail].join('|') : '';
      if (fsig !== refresh._faceSig) {
        const ended = refresh._faceSig && fb && !(fb.status === 'QUEUED' || fb.status === 'RUNNING');
        refresh._faceSig = fsig;
        if (people.scan) { people.scan.scan = fb || null; renderFaceBar(); }
        if (ended) loadPeople(true);
      }
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
    const tz = $('#captureTz').value;
    if (tz) body.timezone = tz;
    if (currentWorkspace !== 'default') body.workspace = currentWorkspace;
    await api('/api/capture/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    refresh();
  } catch (e) { alert(e.message); }
};
$('#cancel').onclick = () => api('/api/capture/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(refresh);
$('#workspacePick').onchange = e => setWorkspace(e.target.value);
$('#workspaceAdd').onclick = async () => {
  const name = prompt(L('workspace.prompt'));
  if (!name) return;
  try {
    await api('/api/workspaces', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
    await loadWorkspaces();
    setWorkspace(name.trim());
  } catch (e) { alert(e.message); }
};

// ============ hash router: one section at a time, deep-linkable ============
const SECTIONS = ['pulpit', 'zadania', 'kasety', 'duplikaty', 'timeline', 'osoby', 'logi', 'info'];

function currentRoute() {
  const parts = location.hash.replace(/^#/, '').split('/').map(decodeURIComponent);
  const section = SECTIONS.includes(parts[0]) ? parts[0] : 'pulpit';
  const r = { section, tape: null, scene: null, year: null, month: null, person: null };
  if (section === 'osoby' && /^\d+$/.test(parts[1] || '')) r.person = +parts[1];
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
  } else if (r.section === 'osoby' && r.person) {
    const p = (people.list || []).find(x => x.id === r.person);
    html = `<button data-go="#osoby" class="text-gray-600 hover:text-brand-500 dark:text-gray-300">${L('people.crumbAll')}</button>
      <span class="text-gray-300 dark:text-gray-600">/</span><span class="truncate text-gray-700 dark:text-gray-200">${p && p.name ? esc(p.name) : L('people.unnamed')}</span>`;
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
    if (r.section === 'osoby') { if (!r.person) { people.person = null; people.merging = false; } loadPeople(false); }
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
