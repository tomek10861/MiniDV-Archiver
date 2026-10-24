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
// Server timestamps (job/task times, last scan…) are UTC ISO strings; show them in
// the viewer's own time zone, not as raw UTC (which read as 1–2 h "behind" in Poland).
// Recording dates are naive wall-clock strings and never go through these.
const _localDate = iso => { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d : null; };
const _p2 = n => String(n).padStart(2, '0');
// "14.09 21:32"
const fmtDT = iso => { const d = _localDate(iso); return d ? `${_p2(d.getDate())}.${_p2(d.getMonth() + 1)} ${_p2(d.getHours())}:${_p2(d.getMinutes())}` : ''; };
// "21:32:05"
const fmtTime = iso => { const d = _localDate(iso); return d ? `${_p2(d.getHours())}:${_p2(d.getMinutes())}:${_p2(d.getSeconds())}` : ''; };
// "2026-09-14 21:32:05"
const fmtStamp = iso => { const d = _localDate(iso); return d ? `${d.getFullYear()}-${_p2(d.getMonth() + 1)}-${_p2(d.getDate())} ${fmtTime(iso)}` : ''; };
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
  people.list = null; people.person = null; people.merging = false; people.groupFilter = "";
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
// ---- photos & videos from cameras/phones: modal items are {tapeId: MEDIA, s: {scene_id, media}} ----
const MEDIA = '@media';
const mediaThumb = id => `/api/media/${id}/thumb.jpg`;
const mediaItem = m => ({ tapeId: MEDIA, s: { scene_id: String(m.id), media: m } });
const fmtDur = s => s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}` : '';
function mediaTile(m, i) {
  return `<button data-mkey="${i}" class="${TLTILE}">
    <div class="relative aspect-square overflow-hidden rounded-xl bg-black">
      <img loading="lazy" src="${mediaThumb(m.id)}" alt="" class="h-full w-full object-cover">
      ${m.kind === 'video' ? `<span class="absolute bottom-1 right-1 rounded bg-black/70 px-1.5 text-[11px] text-white">▶ ${fmtDur(m.duration)}</span>` : ''}
    </div>
    <div class="mt-1.5 px-0.5">
      <div class="truncate text-theme-xs font-semibold text-gray-800 group-hover:text-brand-500 dark:text-white/90">${esc(m.name)}</div>
      <div class="truncate text-[11px] text-gray-500 dark:text-gray-400">${m.date ? m.date + (m.time ? ' ' + m.time.slice(0, 5) : '') : L('tl.noDate')}</div>
    </div>
  </button>`;
}

function sceneGridHTML(items) {   // items: [{tapeId, s}] -- s is a full scene record, or a media item
  if (!items.length) return `<p class="text-theme-sm text-gray-400">${L('scenes.empty')}</p>`;
  return `<div class="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">` +
    items.map((it, i) => {
      if (it.s.media) return mediaTile(it.s.media, i);
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
  // People view: open paused on the frame where the person shows up, so a face caught
  // mid-pan can be stepped through frame by frame (the bar under the video)
  modalSeek = seekTo != null && seekTo >= 0 ? seekTo : null;
  $('#scModal').classList.remove('hidden'); $('#scModal').classList.add('flex');
  renderSceneModal();
}
function closeSceneModal() {
  $('#scModal').classList.add('hidden'); $('#scModal').classList.remove('flex');
  const v = $('#scModalVideo'); v.pause(); v.removeAttribute('src'); v.load();
  $('#scModalImg').removeAttribute('src');
  modalItems = []; modalIdx = -1;
}
function renderMediaModal(m) {
  const video = $('#scModalVideo'), img = $('#scModalImg'), photo = m.kind === 'photo';
  video.pause();
  video.classList.toggle('hidden', photo); img.classList.toggle('hidden', !photo); $('#scFrameBar').classList.toggle('hidden', photo);
  if (photo) { video.removeAttribute('src'); video.load(); img.src = `/api/media/${m.id}/view`; }
  else {
    img.removeAttribute('src');
    video.src = `/api/media/${m.id}/video`; video.load();
    modalFrame = 1 / (m.fps || 30);              // the copy's real rate (DVD copies are 50p)
    if (modalSeek != null) {
      const at = modalSeek; modalSeek = null;
      video.addEventListener('loadedmetadata', () => { try { video.currentTime = at; } catch (_) {} }, { once: true });
    } else video.play().catch(() => {});
  }
  $('#scModalTitle').innerHTML = `<div class="truncate">${esc(m.name)}</div>
    <div class="mt-0.5 truncate text-theme-xs font-normal text-gray-500 dark:text-gray-400">
      ${m.date ? m.date + (m.time ? ' ' + m.time.slice(0, 5) : '') + (m.date_source === 'file' ? ' ' + L('media.fileDate') : m.date_source === 'neighbors' ? ' ' + L('media.neighborDate') : '') : L('tl.noDate') + (m.date_source === 'scan' ? ' ' + L('media.scanNoDate') : '')}${m.camera ? ' · ' + esc(m.camera) : ''} · ${esc(m.album || '')}
      ${m.kind === 'video' && !m.playable ? ' · ' + L('media.notReady') : ''}
    </div>`;
  $('#scModalActions').innerHTML = `
    <a class="${LINK}" href="/api/media/${m.id}/original?dl=1">${L(m.parent_id ? 'media.clipDl' : 'media.original')}</a>
    ${m.parent_id ? `<a class="${LINK}" href="/api/media/${m.parent_id}/original?dl=1">${L('media.wholeFilm')}</a>` : ''}
    <button data-media-date="${m.id}" class="${BTN}">${L('people.tlSetDate')}</button>
    ${m.parent_id ? `<button data-media-date="${m.id}" data-whole="1" class="${BTN}">${L('media.wholeDate')}</button>` : ''}
    ${m.album ? `<button data-open-album="${encodeURIComponent(m.album)}" class="${BTN}">${L('media.openAlbum')}</button>` : ''}`;
  $('#scModalPrev').disabled = modalIdx <= 0;
  $('#scModalNext').disabled = modalIdx >= modalItems.length - 1;
  $('#scModalCount').textContent = `${modalIdx + 1} / ${modalItems.length}`;
}
function renderSceneModal() {
  if (modalIdx < 0 || modalIdx >= modalItems.length) return;
  const { tapeId, s } = modalItems[modalIdx];
  if (s.media) return renderMediaModal(s.media);
  $('#scModalVideo').classList.remove('hidden'); $('#scModalImg').classList.add('hidden'); $('#scFrameBar').classList.remove('hidden');
  const enc = encodeURIComponent(tapeId), sid = s.scene_id, f = s.files || {}, tc = s.timecode || {},
    rec = s.recording || {}, v = s.video || {}, arch = f.archive || {}, prox = f.proxy || {};
  const file = n => `/api/tapes/${enc}/files/${encodeURIComponent(sid)}.${n}`;
  const video = $('#scModalVideo');
  video.pause(); video.src = file('mp4'); video.load();
  // proxies show every field as a frame (bwdif send_field: 50p / 59.94p) unless the scene wasn't interlaced
  modalFrame = (v.standard === 'NTSC' ? 1001 / 30000 : 1 / 25) / (prox.deinterlace === 'none' ? 1 : 2);
  if (modalSeek != null) {
    const at = modalSeek; modalSeek = null;
    video.addEventListener('loadedmetadata', () => { try { video.currentTime = at; } catch (_) {} }, { once: true });
  } else {
    video.play().catch(() => {});
  }
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
$('#scModal').addEventListener('click', async e => {
  if (e.target.id === 'scModal') { closeSceneModal(); return; }
  const md = e.target.closest('[data-media-date]');
  if (md) {
    const m = modalItems[modalIdx].s.media;
    const v = prompt(L('people.datePrompt'), m.date || '');
    if (v === null) return;
    const val = v.trim() === '?' ? 'unknown' : v.trim();
    if (val && val !== 'unknown' && !/^\d{4}-\d{2}-\d{2}$/.test(val)) { alert(L('people.dateBad')); return; }
    const whole = !!md.dataset.whole;
    try {
      modalItems[modalIdx].s.media = await postJSON(`/api/media/${m.id}/date`, { date: val, whole });
      if (whole) modalItems.forEach(it => { const x = it.s && it.s.media; if (x && x.parent_id === m.parent_id) x.date = val === 'unknown' ? null : val || x.date; });
      renderSceneModal(); tl.data = null; mediaState.sig = '';
    }
    catch (err) { alert(err.message); }
    return;
  }
  const oa = e.target.closest('[data-open-album]');
  if (oa) { closeSceneModal(); go('#media/' + oa.dataset.openAlbum); return; }
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
// ---- frame stepping in the scene viewer ----
let modalFrame = 1 / 25;
function frameStep(n) {
  const v = $('#scModalVideo');
  v.pause();
  const end = isFinite(v.duration) ? v.duration : Infinity;
  v.currentTime = Math.min(end, Math.max(0, v.currentTime + n * modalFrame));
}
function showFrameTime() {
  const t = $('#scModalVideo').currentTime || 0;
  $('#scFrameTime').textContent = `${Math.floor(t / 60)}:${(t % 60).toFixed(2).padStart(5, '0')}`;
}
['timeupdate', 'seeked', 'loadedmetadata'].forEach(ev => $('#scModalVideo').addEventListener(ev, showFrameTime));
$('#scFrameBar').addEventListener('click', e => {
  const st = e.target.closest('[data-frame-step]');
  if (st) { frameStep(+st.dataset.frameStep); return; }
  if (e.target.closest('[data-frame-toggle]')) { const v = $('#scModalVideo'); v.paused ? v.play().catch(() => {}) : v.pause(); }
});

document.addEventListener('keydown', e => {
  if ($('#scModal').classList.contains('hidden')) return;
  if (e.key === ',' || e.key === '<') { e.preventDefault(); frameStep(e.shiftKey || e.key === '<' ? -5 : -1); return; }
  if (e.key === '.' || e.key === '>') { e.preventDefault(); frameStep(e.shiftKey || e.key === '>' ? 5 : 1); return; }
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
  const sig = JSON.stringify([id, t.scene_count, t.label, t.recording_date, t.undated_date, t.undated_scenes, !!full,
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
      <span class="font-mono text-theme-xs text-gray-500 dark:text-gray-400">${nScen(t.scene_count ?? scenes.length)} · ${t.recording_date ? '📅 ' + t.recording_date : fmtStamp(t.capture_completed_at)}</span>
      <span class="ml-auto">${viewToggleHTML()}</span>
    </div>
    <div class="mt-2 flex flex-wrap items-center gap-2 text-theme-xs">
      <button id="tapeRename" class="${BTN}">${L('tape.rename')}</button>
      <button id="tapeLabel" class="${BTN}">${L('tape.label')}</button>
      <button id="tapeDate" class="${BTN}">${L('tape.date')}</button>
      ${t.undated_scenes || t.undated_date ? `<button id="tapeUndated" class="${BTN}" title="${L('tape.undatedHint')}">${L('tape.undated', { n: t.undated_scenes || 0 })}${t.undated_date ? ': ' + t.undated_date : ''}</button>` : ''}
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
  const q = field === 'label' ? L('prompt.label') : field === 'undated_date' ? L('prompt.undated') : L('prompt.date');
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
  if (e.target.closest('#tapeUndated')) { editTapeMeta(openTapeId, 'undated_date'); return; }
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
const tlThumb = t => t && t.media_id
  ? `<img loading="lazy" class="h-full w-full object-cover" src="${mediaThumb(t.media_id)}" alt="">`
  : t && t.scene_id
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

// which sources the timeline shows: MiniDV scenes, camera/phone videos, photos (any mix)
const TL_SOURCES = ['dv', 'video', 'photo'];
let tlSources = (() => {
  try { const v = JSON.parse(localStorage.getItem('tlSources')); if (Array.isArray(v) && v.length) return v.filter(x => TL_SOURCES.includes(x)); } catch (_) {}
  return [...TL_SOURCES];
})();
const tlQS = () => {
  const q = wsQS();
  return tlSources.length === TL_SOURCES.length ? q : (q ? q + '&' : '?') + 'sources=' + tlSources.join(',');
};
function syncTlSources() {
  $$('#tlSourcesMenu [data-src]').forEach(cb => { cb.checked = tlSources.includes(cb.dataset.src); });
  $('#tlSourcesLabel').textContent = tlSources.length === TL_SOURCES.length ? L('tl.src.all')
    : tlSources.map(s => L('tl.src.' + s)).join(', ');
}
syncTlSources();
$('#tlSourcesBtn').addEventListener('click', e => { e.stopPropagation(); $('#tlSourcesMenu').classList.toggle('hidden'); });
document.addEventListener('click', e => { if (!e.target.closest('#tlSourcesMenu, #tlSourcesBtn')) $('#tlSourcesMenu').classList.add('hidden'); });
$('#tlSourcesMenu').addEventListener('change', e => {
  const picked = $$('#tlSourcesMenu [data-src]').filter(cb => cb.checked).map(cb => cb.dataset.src);
  if (!picked.length) { e.target.checked = true; return; }          // at least one source
  tlSources = picked;
  try { localStorage.setItem('tlSources', JSON.stringify(tlSources)); } catch (_) {}
  syncTlSources();
  tl.data = null;
  if (tl.month) renderTimeline(); else loadTimeline(true);
});

async function loadTimeline(force) {
  if (tl.loading) return;
  tl.loading = true;
  if (force || !tl.data) {
    try { tl.data = await api(`/api/timeline${tlQS()}`); }
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
  try { tlScenes = await api(`/api/timeline/${tl.year}/${tl.month}${tlQS()}`); }
  catch (e) { view.innerHTML = `<p class="text-error-500">${e.message}</p>`; return; }
  const tapeIds = [...new Set(tlScenes.filter(s => !s.media).map(s => s.tape_id))];
  await Promise.all(tapeIds.filter(id => !scenesCache[id]).map(async id => {
    try { scenesCache[id] = await api(`/api/tapes/${encodeURIComponent(id)}/scenes`); } catch (_) { scenesCache[id] = []; }
  }));
  renderTlScenes();
}

let tlGridItems = [];                 // [{tapeId, s}] backing the month's grid view, for the modal's prev/next
function renderTlScenes() {
  const view = $('#tlView');
  view.className = 'p-5';
  const withFull = tlScenes.map(s => ({ s, full: s.media ? { scene_id: s.scene_id, media: s.media }
    : (scenesCache[s.tape_id] || []).find(x => x.scene_id === s.scene_id) })).filter(x => x.full);
  tlGridItems = withFull.map(({ s, full }) => ({ tapeId: s.tape_id, s: full }));
  view.innerHTML = `
    ${repairOptsHTML()}
    <div class="mt-3 flex items-center justify-end">${viewToggleHTML()}</div>` +
    (sceneView === 'grid'
      ? sceneGridHTML(tlGridItems)
      : `<div id="tlSelBar" class="mb-4 hidden flex-wrap items-center gap-3 rounded-xl border border-brand-500/40 bg-brand-50/60 p-3 dark:bg-brand-500/[0.08]">
          <span class="text-sm">${L('sel.count')} <b id="tlSelCount">0</b></span>
          <button id="tlBuild" class="${BTN_PRIMARY}">${L('tl.build')}</button>
          <button id="tlBuildRepair" class="${BTN}">${L('tl.buildRepair')}</button>
          <button id="tlArchive" class="${BTN}">${L('sel.archive')}</button>
          <button id="tlSelClear" class="${BTN}">${L('sel.clear')}</button>
          <span class="basis-full text-theme-xs text-gray-500 dark:text-gray-400 sm:basis-auto">${L('tl.buildHint')}</span>
        </div>
        <div id="tlSceneList" class="playable grid gap-3">` + withFull.map(({ s, full }, i) => {
          if (s.media) {
            const key = tlKey(s.tape_id, s.scene_id), sel = tlSelected.has(key);
            return `<div data-tl-key="${key}" class="rounded-xl ${sel ? 'ring-2 ring-brand-500' : ''}">
            <div class="mb-1 flex items-center gap-2 text-theme-xs text-gray-400">
              <input type="checkbox" class="tlsel h-3.5 w-3.5 accent-brand-500" data-tape="${s.tape_id}" data-scene="${s.scene_id}" ${sel ? 'checked' : ''}>
              <span class="truncate">${s.media.kind === 'photo' ? L('tl.src.photo') : L('tl.src.video')} · ${s.date}${s.time ? ' ' + s.time.slice(0, 5) : ''}</span>
            </div>
            <div class="flex items-center gap-4 rounded-xl border border-gray-200 bg-gray-50 p-3 dark:border-gray-800 dark:bg-white/[0.02]">
            <button data-mkey="${i}" class="relative shrink-0"><img loading="lazy" src="${mediaThumb(s.media.id)}" alt="" class="h-24 w-32 rounded-lg bg-black object-cover">
              ${s.media.kind === 'video' ? `<span class="absolute bottom-1 right-1 rounded bg-black/70 px-1.5 text-[11px] text-white">▶ ${fmtDur(s.media.duration)}</span>` : ''}</button>
            <div class="min-w-0">
              <div class="truncate font-semibold text-gray-800 dark:text-white/90">${s.media.kind === 'photo' ? '📷' : '🎬'} ${esc(s.media.name)}</div>
              <div class="text-theme-xs text-gray-500 dark:text-gray-400">${s.date}${s.time ? ' ' + s.time.slice(0, 5) : ''} · ${esc(s.label || '')}</div>
              <button data-open-album="${encodeURIComponent(s.label || '')}" class="mt-1 text-theme-xs text-brand-500 hover:underline">${L('media.openAlbum')}</button>
            </div>
          </div></div>`;
          }
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
  const oa = e.target.closest('[data-open-album]');
  if (oa) { go('#media/' + oa.dataset.openAlbum); return; }
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
let people = { list: null, showAll: false, person: null, merging: false, scan: null, selecting: false, sel: new Set() };
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
      const [list, st, sug] = await Promise.all([api(`/api/persons${wsQS()}`), api(`/api/faces/status${wsQS()}`),
        api(`/api/faces/suggestions${wsQS()}`).catch(() => [])]);
      people.list = list; people.scan = st; people.suggest = sug;
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

// ---- groups (family / friends / neighbours / anything): a label on a person, named or not ----
const groupPresets = () => [L('people.grp.family'), L('people.grp.friends'), L('people.grp.acquaintances'),
  L('people.grp.neighbours'), L('people.grp.others')];
const groupChoices = () => [...new Set([...groupPresets(), ...(people.list || []).map(p => p.group).filter(Boolean)])];
// groups kept out of the default "Everyone" view; their own chip still shows them
const groupsOffByDefault = () => [L('people.grp.acquaintances')];
const inGroupFilter = p => !people.groupFilter ? !groupsOffByDefault().includes(p.group)
  : people.groupFilter === '__hidden' ? p.hidden
  : people.groupFilter === '__none' ? !p.group : p.group === people.groupFilter;

function personTile(p) {
  const label = p.name ? esc(p.name) : p.group ? esc(p.group) : L('people.addName');
  const sub = [nRec(p.scenes), p.name && p.group ? esc(p.group) : '', !p.name && p.group ? L('people.noName') : ''].filter(Boolean).join(' · ');
  // selection mode: tapping a tile ticks it instead of opening the person
  const sel = people.selecting, on = sel && people.sel.has(p.id);
  return `<button ${sel ? `data-sel-person="${p.id}"` : `data-go="#osoby/${p.id}"`} class="group relative flex flex-col items-center gap-1.5 rounded-xl p-2 text-center hover:bg-gray-50 dark:hover:bg-white/[0.04] ${on ? 'bg-brand-50 dark:bg-brand-500/[0.12]' : ''}">
    ${sel ? `<span class="absolute left-2 top-2 z-10 grid h-6 w-6 place-items-center rounded-full border-2 ${on ? 'border-brand-500 bg-brand-500 text-white' : 'border-gray-300 bg-white dark:border-gray-600 dark:bg-gray-900'} text-[13px]">${on ? '✓' : ''}</span>` : ''}
    ${avatar(p.cover, `h-24 w-24 transition group-hover:ring-2 group-hover:ring-brand-500 ${on ? 'ring-4 ring-brand-500' : ''}`)}
    <span class="max-w-full truncate text-theme-sm font-medium ${p.name ? 'text-gray-800 dark:text-white/90' : 'text-gray-400'}">${label}</span>
    <span class="text-[11px] text-gray-400">${sub}</span>
  </button>`;
}
function selectBar() {
  if (!people.selecting)
    return `<div class="mb-3"><button id="peopleSelect" class="${BTN}">${L('people.select')}</button></div>`;
  const n = people.sel.size;
  return `<div class="sticky top-16 z-20 mb-3 flex flex-wrap items-center gap-2 rounded-xl border border-brand-500/40 bg-brand-50 p-2 dark:bg-gray-900">
    <span class="text-theme-sm">${L('sel.count')} <b>${n}</b></span>
    <button id="peopleSelDissolve" class="${BTN}" ${n ? '' : 'disabled'} title="${L('people.dissolveHint')}">${L('people.dissolve')}</button>
    <button id="peopleSelDelete" class="${BTN_DANGER}" ${n ? '' : 'disabled'} title="${L('people.deleteHint')}">${L('people.deleteFaces')}</button>
    <button id="peopleSelCancel" class="${BTN}">${L('sel.clear')}</button>
  </div>`;
}
function groupChips() {
  const all = (people.list || []).filter(p => !p.hidden), counts = {};
  all.forEach(p => { if (p.group) counts[p.group] = (counts[p.group] || 0) + 1; });
  const groups = Object.keys(counts), hiddenN = (people.list || []).filter(p => p.hidden).length;
  if (!groups.length && !hiddenN) return '';
  const chip = (value, label, n) => `<button data-group-filter="${esc(value)}" class="rounded-full border px-3 py-1 text-theme-xs font-medium ${
    (people.groupFilter || '') === value ? 'border-brand-500 bg-brand-500 text-white' : 'border-gray-300 text-gray-600 hover:border-brand-500 dark:border-gray-600 dark:text-gray-300'}">${label}${n != null ? ` <span class="opacity-70">${n}</span>` : ''}</button>`;
  return `<div class="mb-4 flex flex-wrap items-center gap-2">${chip('', L('people.grp.all'))}${
    groups.sort((a, b) => counts[b] - counts[a]).map(g => chip(g, esc(g), counts[g])).join('')}${
    groups.length ? chip('__none', L('people.grp.none'), all.filter(p => !p.group).length) : ''}${
    hiddenN ? chip('__hidden', L('people.grp.hidden'), hiddenN) : ''}</div>`;
}

function renderPeople() {
  const view = $('#peopleView'), list = (people.list || []).filter(inGroupFilter);
  const named = list.filter(p => p.name && !p.hidden);
  const unnamed = list.filter(p => !p.name && !p.hidden).sort((a, b) => b.scenes - a.scenes || b.tracks - a.tracks);
  const often = unnamed.filter(p => p.scenes >= 2), rare = unnamed.filter(p => p.scenes < 2);
  const hidden = list.filter(p => p.hidden);
  const grid = ps => `<div class="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-8">${ps.map(personTile).join('')}</div>`;
  const head = (t, extra) => `<div class="mb-2 mt-6 flex flex-wrap items-center gap-3 first:mt-0"><h3 class="text-theme-sm font-semibold text-gray-700 dark:text-gray-200">${t}</h3>${extra || ''}</div>`;
  if (!list.length) {
    view.innerHTML = selectBar() + groupChips() + `<p class="text-theme-sm text-gray-400">${L(people.groupFilter ? 'people.grp.empty' : 'people.empty')}</p>`;
    return;
  }
  const sug = (people.suggest || []).filter(inGroupFilter), SUG_SHOWN = 12;
  view.innerHTML = selectBar() + groupChips() +
    (named.length ? head(L('people.named', { n: named.length })) + grid(named) : '')
    + (sug.length ? head(L('people.sugTitle', { n: sug.length }), `<span class="text-theme-xs text-gray-400">${L('people.sugHint')}</span>`)
      + `<div class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">${(people.sugAll ? sug : sug.slice(0, SUG_SHOWN)).map(suggestCard).join('')}</div>`
      + (sug.length > SUG_SHOWN ? `<button id="peopleSugAll" class="${BTN} mt-2">${people.sugAll ? L('people.hideRare') : L('people.sugMore', { n: sug.length - SUG_SHOWN })}</button>` : '') : '')
    + (often.length || rare.length ? head(L('people.who'), rare.length
        ? `<button id="peopleShowAll" class="${BTN}">${people.showAll ? L('people.hideRare') : L('people.showRare', { n: rare.length })}</button>` : '')
      + grid(people.showAll ? [...often, ...rare] : often)
      + (!often.length && !people.showAll ? `<p class="text-theme-xs text-gray-400">${L('people.onlyRare')}</p>` : '') : '')
    + (hidden.length ? head(L('people.hidden', { n: hidden.length })) + grid(hidden) : '');
}

// "is this <name>?": an unnamed person next to the named one it most likely is
function suggestCard(p) {
  const s = p.suggest;
  return `<div class="flex items-center gap-3 rounded-xl border border-gray-200 p-2 dark:border-gray-800">
    <button data-go="#osoby/${p.id}" title="${L('people.sugOpen')}">${avatar(p.cover, 'h-14 w-14')}</button>
    <div class="min-w-0 flex-1">
      <div class="text-theme-xs text-gray-400">${L('people.sugIs')}</div>
      <div class="flex items-center gap-1.5">${avatar(s.cover, 'h-7 w-7 shrink-0')}<b class="text-theme-sm leading-tight text-gray-800 dark:text-white/90">${esc(s.name)}?</b></div>
      <div class="text-[11px] text-gray-400">${nRec(p.scenes)}</div>
    </div>
    <div class="flex shrink-0 flex-col gap-1">
      <button data-sugg-yes="${p.id}:${s.id}" class="${BTN_PRIMARY}">${L('people.sugYes')}</button>
      <button data-sugg-no="${p.id}:${s.id}" class="${BTN}">${L('people.sugNo')}</button>
    </div>
  </div>`;
}

async function openPerson(id) {
  const view = $('#peopleView');
  if (!people.person || people.person.id !== id) view.innerHTML = `<p class="text-theme-sm text-gray-400">${L('loading')}</p>`;
  let p;
  try { p = await api(`/api/persons/${id}`); }
  catch (e) { view.innerHTML = `<p class="text-error-500">${e.message}</p>`; return; }
  if (currentRoute().person !== id) return;
  if (!people.person || people.person.id !== id) { people.tlRange = null; people.tlOpenSlot = null; }
  // full scene records for the modal player (same source as Kasety / timeline)
  const tapeIds = [...new Set(p.scenes.filter(s => !s.media).map(s => s.tape_id))];
  await Promise.all(tapeIds.filter(t => !scenesCache[t]).map(async t => {
    try { scenesCache[t] = await api(`/api/tapes/${encodeURIComponent(t)}/scenes`); } catch (_) { scenesCache[t] = []; }
  }));
  people.person = p;
  renderPerson();
}

let personItems = [];                 // [{tapeId, s}] for the modal's prev/next, one per scene of this person
function renderPerson() {
  const p = people.person, view = $('#peopleView'), cover = p.tracks[0] && p.tracks[0].id;
  personItems = p.scenes.map(sc => sc.media
    ? mediaItem({ ...sc.media, album: sc.label, date: sc.date, time: sc.time, playable: true })
    : ({ tapeId: sc.tape_id,
      s: (scenesCache[sc.tape_id] || []).find(x => x.scene_id === sc.scene_id) || { scene_id: sc.scene_id, scene_index: sc.scene_index } }));
  const others = (people.list || []).filter(o => o.id !== p.id);
  view.innerHTML = `
    <div class="flex flex-wrap items-center gap-4">
      ${avatar(cover, 'h-24 w-24')}
      <div class="min-w-0">
        <h3 class="truncate text-xl font-semibold ${p.name ? 'text-gray-800 dark:text-white/90' : 'text-gray-400'}">${p.name ? esc(p.name) : L('people.unnamed')}</h3>
        <div class="text-theme-xs text-gray-500 dark:text-gray-400">${nRec(p.scenes.length)}</div>
        ${p.suggest ? `<div class="mt-2 inline-flex flex-wrap items-center gap-2 rounded-xl border border-brand-500/40 bg-brand-50/60 px-3 py-1.5 text-theme-sm dark:bg-brand-500/[0.08]">
          ${L('people.sugIs')} ${avatar(p.suggest.cover, 'h-7 w-7')} <b class="text-gray-800 dark:text-white/90">${esc(p.suggest.name)}</b>?
          <button data-sugg-yes="${p.id}:${p.suggest.id}" class="${BTN_PRIMARY}">${L('people.sugYes')}</button>
          <button data-sugg-no="${p.id}:${p.suggest.id}" class="${BTN}">${L('people.sugNo')}</button>
        </div>` : ''}
        <div class="mt-2 flex flex-wrap items-center gap-2">
          <button id="personName" class="${p.name ? BTN : BTN_PRIMARY}">${p.name ? L('people.rename') : L('people.addName')}</button>
          <label class="inline-flex items-center gap-1.5 text-theme-xs text-gray-500 dark:text-gray-400">${L('people.grp.label')}
            <select id="personGroup" class="h-8 rounded-lg border border-gray-300 bg-white px-2 text-theme-xs text-gray-700 dark:border-gray-600 dark:bg-white/[0.06] dark:text-gray-200">
              <option value="">${L('people.grp.unset')}</option>
              ${[...new Set([...groupChoices(), ...(p.group ? [p.group] : [])])].map(g => `<option value="${esc(g)}" ${g === p.group ? 'selected' : ''}>${esc(g)}</option>`).join('')}
              <option value="__new">${L('people.grp.new')}</option>
            </select>
          </label>
          <button id="filmNew" class="${BTN_PRIMARY}">${L('film.make')}</button>
          <button id="personMerge" class="${BTN}">${L('people.merge')}</button>
          <button id="personHide" class="${BTN}">${p.hidden ? L('people.unhide') : L('people.hide')}</button>
          <button id="personDissolve" class="${BTN}" title="${L('people.dissolveHint')}">${L('people.dissolve')}</button>
          <button id="personDelete" class="${BTN_DANGER}" title="${L('people.deleteHint')}">${L('people.deleteFaces')}</button>
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
    <div id="personFilms" class="mt-6"></div>
    <div id="personTl" class="mt-6"></div>
    <h4 class="mb-2 mt-6 text-theme-sm font-semibold text-gray-700 dark:text-gray-200">${L('people.faces', { n: p.tracks.length })}</h4>
    <p class="mb-2 text-theme-xs text-gray-400">${L('people.facesHint')}</p>
    <div class="flex flex-wrap gap-2">${[...p.tracks].sort((a, b) =>          // oldest first; undated last
      `${a.date || '9999'}${a.time || ''}${String(a.first).padStart(6, '0')}`.localeCompare(`${b.date || '9999'}${b.time || ''}${String(b.first).padStart(6, '0')}`)).map(t => `
      <div class="relative">
        <button data-face-seek="${t.id}" title="${t.date ? fmtDay(t.date) + ' · ' : ''}${t.tape_id === MEDIA ? '' : esc(t.tape_id) + ' · ' + mmss(t.first)}">${avatar(t.id, 'h-16 w-16')}</button>
        ${p.tracks.length > 1 ? `<button data-face-out="${t.id}" title="${L('people.notThis')}" class="absolute -right-1 -top-1 grid h-5 w-5 place-items-center rounded-full bg-gray-800 text-[11px] text-white hover:bg-error-500">✕</button>` : ''}
      </div>`).join('')}</div>
    <h4 class="mb-2 mt-6 text-theme-sm font-semibold text-gray-700 dark:text-gray-200">${L('people.recordings')}</h4>
    <div class="grid gap-3">${p.scenes.map((sc, i) => `
      <div class="flex gap-4 rounded-xl border border-gray-200 bg-gray-50 p-3 dark:border-gray-800 dark:bg-white/[0.02]">
        <button data-play="${i}" data-at="${sc.segments[0] ? sc.segments[0][0] : 0}" class="shrink-0">
          <img loading="lazy" src="${sc.media ? mediaThumb(sc.media.id) : `/api/tapes/${encodeURIComponent(sc.tape_id)}/files/thumbnails/${encodeURIComponent(sc.scene_id)}.jpg`}" alt="" class="h-24 w-32 rounded-lg bg-black object-cover">
        </button>
        <div class="min-w-0 flex-1">
          <div class="truncate font-semibold text-gray-800 dark:text-white/90">${sc.media
            ? `${sc.media.kind === 'photo' ? '📷' : '🎬'} ${esc(sc.media.name || '')}`
            : `${esc(sc.label || sc.tape_id)} · ${L('tl.scene', { n: sc.scene_index ?? '?' })}`}</div>
          <div class="text-theme-xs text-gray-500 dark:text-gray-400">${sc.date || L('tl.noDate')}${sc.time ? ' ' + sc.time.slice(0, 5) : ''}${sc.media ? ' · ' + esc(sc.label || '') : sc.label ? ' · ' + esc(sc.tape_id) : ''}</div>
          <div class="mt-2 flex flex-wrap items-center gap-1.5 text-theme-xs">
            ${sc.media && sc.media.kind === 'photo'
              ? `<button data-play="${i}" data-at="0" class="${BTN}">${L('people.showPhoto')}</button>`
              : `<span class="text-gray-400">${L('people.seenAt')}</span>
                ${sc.segments.map(([a, b]) => `<button data-play="${i}" data-at="${a}" class="${BTN} font-mono">▶ ${mmss(a)}${b > a ? '–' + mmss(b) : ''}</button>`).join('')}`}
            ${sc.media
              ? `<a class="${LINK} ml-2" href="#media/${encodeURIComponent(sc.label || '')}">${L('media.openAlbum')}</a>`
              : `<a class="${LINK} ml-2" href="#kasety/${encodeURIComponent(sc.tape_id)}/${encodeURIComponent(sc.scene_id)}">${L('tl.openInTapes')}</a>`}
          </div>
        </div>
      </div>`).join('')}</div>`;
  renderPersonTimeline();
  if (films.pid !== p.id) { films.pid = p.id; films.list = []; films.open = false; films.changing = null; loadFilms(); }
  else renderFilms();
}

// ---- films about a person ("Zmontuj film", like the iPhone's Memories) ----
// The server picks the moments (best faces, spread over the years), cuts them to
// the music's beat, adds dates, a title and the music's credits; each new film
// is a different pick. Here: the options, the music picker, the list of films.
const films = { pid: null, list: [], open: false, changing: null, music: null, mood: 'sentimental',
  pick: 'auto:sentimental', results: null, opts: { length: 'medium', sources: ['dv', 'video', 'photo'], from: '', to: '' } };
let filmPoll = 0, filmAudio = null;
async function loadFilms() {
  const pid = films.pid;
  try { const list = await api(`/api/persons/${pid}/memories`); if (pid !== films.pid) return; films.list = list; }
  catch (_) { return; }
  const busy = films.list.some(f => f.status === 'QUEUED' || f.status === 'RUNNING');
  const box = $('#personFilms'), sig = films.list.map(f => f.id + f.status + (f.updated_at || '')).join();
  if (box && box.dataset.sig === sig && busy) {          // only progress moved: don't reset a playing video
    films.list.forEach(f => { const bar = box.querySelector(`[data-film-bar="${f.id}"]`); if (bar) bar.style.width = `${Math.round((f.progress || 0) * 100)}%`; });
  } else renderFilms();
  clearTimeout(filmPoll);
  if (busy) filmPoll = setTimeout(() => { if (currentRoute().person === pid) loadFilms(); }, 3000);
}
async function loadMusic() {
  if (films.music) return;
  try { films.music = await api('/api/music'); } catch (e) { films.music = { moods: [], tracks: [], error: e.message }; }
  renderFilms();
}
const fmtLen = s => s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}` : '';
function musicRows(tracks) {
  return tracks.map(t => `
    <label class="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1 hover:bg-gray-100 dark:hover:bg-white/[0.06]">
      <input type="radio" name="filmMusic" value="${esc(t.id)}" ${films.pick === t.id ? 'checked' : ''}>
      <button type="button" data-music-play="${esc(t.id)}" class="grid h-6 w-6 shrink-0 place-items-center rounded-full border border-gray-300 text-[10px] text-gray-700 hover:border-brand-500 hover:text-brand-500 dark:border-gray-600 dark:text-gray-200">${filmAudio && filmAudio.dataset.id === t.id && !filmAudio.paused ? '❚❚' : '▶'}</button>
      <span class="min-w-0 flex-1 truncate text-theme-xs text-gray-500 dark:text-gray-400"><b class="text-gray-800 dark:text-white/90">${esc(t.title)}</b>${t.artist ? ' · ' + esc(t.artist) : ''}</span>
      <span class="shrink-0 font-mono text-theme-xs text-gray-400">${fmtLen(t.duration)}</span>
    </label>`).join('');
}
function musicPicker() {
  const m = films.music;
  if (!m) return `<p class="text-theme-xs text-gray-400">${L('loading')}</p>`;
  const moods = [...m.moods, ...(m.tracks.some(t => t.mood === 'own') ? ['own'] : []), 'search'];
  const tracks = films.mood === 'search' ? (films.results || []) : m.tracks.filter(t => t.mood === films.mood);
  return `
    <div class="flex flex-wrap gap-1.5">${moods.map(md => `<button type="button" data-film-mood="${md}" class="${md === films.mood ? BTN_PRIMARY : BTN}">${L('film.mood.' + md)}</button>`).join('')}</div>
    <div class="mt-2 flex flex-wrap gap-3 text-theme-xs text-gray-700 dark:text-gray-300">
      ${films.mood !== 'search' && films.mood !== 'own' ? `<label class="inline-flex items-center gap-1.5"><input type="radio" name="filmMusic" value="auto:${films.mood}" ${films.pick === 'auto:' + films.mood ? 'checked' : ''}> ${L('film.musicRandom')}</label>` : ''}
      <label class="inline-flex items-center gap-1.5"><input type="radio" name="filmMusic" value="" ${films.pick === '' ? 'checked' : ''}> ${L('film.noMusic')}</label>
    </div>
    ${films.mood === 'search' ? `<div class="mt-2 flex gap-2"><input id="filmSearchQ" class="h-8 min-w-0 flex-1 rounded-lg border border-gray-300 bg-white px-2 text-theme-xs text-gray-700 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200" placeholder="${L('film.searchPh')}" value="${esc(films.query || '')}"><button type="button" id="filmSearchGo" class="${BTN}">${L('film.search')}</button></div>
      <p class="mt-1 text-theme-xs text-gray-400">${L('film.searchHint')}</p>` : ''}
    <div class="mt-2 max-h-56 overflow-y-auto">${musicRows(tracks) || (films.mood === 'search' && films.results ? `<p class="text-theme-xs text-gray-400">${L('film.noResults')}</p>` : '')}</div>`;
}
function filmYears() {
  return [...new Set((people.person.scenes || []).map(s => (s.date || '').slice(0, 4)).filter(y => /^\d{4}$/.test(y)))].sort();
}
function renderFilms() {
  const box = $('#personFilms');
  if (!box || !people.person) return;
  const o = films.opts, years = filmYears();
  box.dataset.sig = films.list.map(f => f.id + f.status + (f.updated_at || '')).join();
  const panel = films.open ? `
    <div class="mb-3 rounded-xl border border-brand-500/40 bg-brand-50/40 p-4 text-gray-700 dark:bg-brand-500/[0.06] dark:text-gray-300">
      <div class="grid gap-4 md:grid-cols-2">
        <div class="space-y-3 text-theme-xs">
          <div><div class="mb-1 font-semibold text-gray-700 dark:text-gray-200">${L('film.length')}</div>
            ${['short', 'medium', 'long'].map(l => `<button type="button" data-film-len="${l}" class="${o.length === l ? BTN_PRIMARY : BTN} mr-1">${L('film.len.' + l)}</button>`).join('')}</div>
          <div><div class="mb-1 font-semibold text-gray-700 dark:text-gray-200">${L('film.period')}</div>
            <select id="filmFrom" class="h-8 rounded-lg border border-gray-300 bg-white px-2 text-gray-700 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200"><option value="">${L('film.fromStart')}</option>${years.map(y => `<option ${o.from === y ? 'selected' : ''}>${y}</option>`).join('')}</select>
            –
            <select id="filmTo" class="h-8 rounded-lg border border-gray-300 bg-white px-2 text-gray-700 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200"><option value="">${L('film.toEnd')}</option>${years.map(y => `<option ${o.to === y ? 'selected' : ''}>${y}</option>`).join('')}</select></div>
          <div><div class="mb-1 font-semibold text-gray-700 dark:text-gray-200">${L('film.sources')}</div>
            ${['dv', 'video', 'photo'].map(sx => `<label class="mr-3 inline-flex items-center gap-1.5"><input type="checkbox" data-film-src="${sx}" ${o.sources.includes(sx) ? 'checked' : ''}> ${L('film.src.' + sx)}</label>`).join('')}</div>
          <p class="text-gray-400">${L('film.hint')}</p>
        </div>
        <div><div class="mb-1 text-theme-xs font-semibold text-gray-700 dark:text-gray-200">${L('film.music')}</div>${musicPicker()}</div>
      </div>
      <div class="mt-3 flex gap-2"><button id="filmGo" class="${BTN_PRIMARY}">${L('film.go')}</button><button id="filmCancel" class="${BTN}">${L('film.cancel')}</button></div>
    </div>` : '';
  box.innerHTML = `
    <div class="mb-2 flex items-center gap-3">
      <h4 class="text-theme-sm font-semibold text-gray-700 dark:text-gray-200">${L('film.title', { n: films.list.length })}</h4>
      ${films.open ? '' : `<button id="filmNew2" class="${BTN}">${L('film.make')}</button>`}
    </div>
    ${panel}
    ${films.list.length ? `<div class="grid gap-3 lg:grid-cols-2">${films.list.map(f => filmCard(f)).join('')}</div>`
      : films.open ? '' : `<p class="text-theme-xs text-gray-400">${L('film.empty')}</p>`}`;
}
function filmCard(f) {
  const live = f.status === 'QUEUED' || f.status === 'RUNNING', p = f.params || {};
  const what = [L('film.len.' + (p.length || 'medium')), p.date_from || p.date_to ? `${p.date_from || '…'}–${p.date_to || '…'}` : '',
    f.music ? '♪ ' + esc(f.music.title) : L('film.noMusic')].filter(Boolean).join(' · ');
  return `
    <div class="rounded-xl border border-gray-200 bg-gray-50 p-3 dark:border-gray-800 dark:bg-white/[0.02]">
      ${f.status === 'READY' ? `<video data-film-video="${f.id}" class="mb-2 aspect-video w-full rounded-lg bg-black" controls preload="none" poster="${f.poster}" src="${f.url}"></video>` : ''}
      <div class="flex flex-wrap items-center gap-2 text-theme-xs">
        <b class="text-gray-800 dark:text-white/90">${esc(f.title || '')}</b>
        <span class="text-gray-500">${fmtStamp(f.created_at)}${f.duration ? ' · ' + fmtLen(f.duration) : ''}${f.pieces ? ' · ' + L('film.pieces', { n: f.pieces }) : ''}</span>
      </div>
      <div class="mt-0.5 truncate text-theme-xs text-gray-400">${what}</div>
      ${live ? `<div class="mt-2 text-theme-xs text-gray-500">${L(f.status === 'QUEUED' ? 'film.queued' : 'film.running')}</div>
        <div class="mt-1 h-1.5 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700"><div data-film-bar="${f.id}" class="h-full bg-brand-500 transition-all" style="width:${Math.round((f.progress || 0) * 100)}%"></div></div>` : ''}
      ${f.status === 'ERROR' ? `<div class="mt-2 text-theme-xs text-error-500">${esc(f.error || '')}</div>` : ''}
      <div class="mt-2 flex flex-wrap gap-1.5">
        ${f.status === 'READY' ? `<a class="${BTN}" href="${f.url}&dl=1">${L('film.download')}</a>` : ''}
        ${live ? '' : `<button data-film-music="${f.id}" class="${BTN}">${L('film.changeMusic')}</button>`}
        ${f.status === 'RUNNING' ? '' : `<button data-film-del="${f.id}" class="${BTN_DANGER}">${L('film.delete')}</button>`}
      </div>
      ${films.changing === f.id ? `<div class="mt-3 rounded-lg border border-gray-200 p-3 text-gray-700 dark:border-gray-700 dark:text-gray-300">${musicPicker()}
        <button data-film-music-go="${f.id}" class="${BTN_PRIMARY} mt-2">${L('film.remix')}</button></div>` : ''}
    </div>`;
}
function filmPreview(id) {
  if (filmAudio && filmAudio.dataset.id === id) { filmAudio.paused ? filmAudio.play() : filmAudio.pause(); return; }
  if (filmAudio) filmAudio.pause();
  filmAudio = new Audio(`/api/music/${encodeURIComponent(id)}/audio`);
  filmAudio.dataset.id = id;
  ['play', 'pause', 'ended'].forEach(ev => filmAudio.addEventListener(ev, renderFilms));
  filmAudio.play().catch(e => alert(e.message));
}
$('#peopleView').addEventListener('change', e => {
  if (e.target.name === 'filmMusic') films.pick = e.target.value;
  else if (e.target.id === 'filmFrom') films.opts.from = e.target.value;
  else if (e.target.id === 'filmTo') films.opts.to = e.target.value;
  else if (e.target.dataset.filmSrc) {
    const s = new Set(films.opts.sources); e.target.checked ? s.add(e.target.dataset.filmSrc) : s.delete(e.target.dataset.filmSrc);
    films.opts.sources = [...s];
  }
});
$('#peopleView').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.id === 'filmSearchQ') { e.preventDefault(); $('#filmSearchGo').click(); } });
$('#peopleView').addEventListener('click', e => {
  const t = e.target, pid = films.pid;
  if (t.id === 'filmNew' || t.id === 'filmNew2') { films.open = true; films.changing = null; renderFilms(); loadMusic(); $('#personFilms').scrollIntoView({ behavior: 'smooth', block: 'start' }); return; }
  if (t.id === 'filmCancel') { films.open = false; renderFilms(); return; }
  const len = t.closest('[data-film-len]'); if (len) { films.opts.length = len.dataset.filmLen; renderFilms(); return; }
  const md = t.closest('[data-film-mood]'); if (md) { films.mood = md.dataset.filmMood; renderFilms(); return; }
  const pl = t.closest('[data-music-play]'); if (pl) { e.preventDefault(); filmPreview(pl.dataset.musicPlay); return; }
  if (t.id === 'filmSearchGo') {
    films.query = $('#filmSearchQ').value.trim(); if (!films.query) return;
    t.disabled = true; t.textContent = L('film.searching');
    personAction(async () => {
      try { films.results = (await api(`/api/music/search?q=${encodeURIComponent(films.query)}`)).tracks; }
      finally { renderFilms(); }
    });
    return;
  }
  if (t.id === 'filmGo') {
    const o = films.opts;
    if (!o.sources.length) { alert(L('film.needSource')); return; }
    if (o.from && o.to && o.from > o.to) { alert(L('film.badPeriod')); return; }
    t.disabled = true;
    personAction(async () => {
      await postJSON(`/api/persons/${pid}/memories`, { length: o.length, sources: o.sources, music: films.pick || null,
        date_from: o.from || null, date_to: o.to || null, lang: window.i18n.lang });
      films.open = false; if (filmAudio) filmAudio.pause(); await loadFilms(); refresh();
    });
    return;
  }
  const ch = t.closest('[data-film-music]');
  if (ch) { films.changing = films.changing === +ch.dataset.filmMusic ? null : +ch.dataset.filmMusic; films.open = false; renderFilms(); loadMusic(); return; }
  const go2 = t.closest('[data-film-music-go]');
  if (go2) {
    t.disabled = true;
    personAction(async () => { await postJSON(`/api/memories/${go2.dataset.filmMusicGo}/music`, { music: films.pick || null });
      films.changing = null; if (filmAudio) filmAudio.pause(); await loadFilms(); refresh(); });
    return;
  }
  const del = t.closest('[data-film-del]');
  if (del && confirm(L('film.confirmDelete'))) personAction(async () => { await api(`/api/memories/${del.dataset.filmDel}`, { method: 'DELETE' }); await loadFilms(); });
});

// ---- person timeline: faces on a date axis, scaled from the oldest to the newest ----
// The axis is cut into slots ~half a thumbnail wide; each slot shows its best face
// (p.tracks comes sorted best-first), alternating above / below the line so twice as
// many fit, with a "+n" badge for the faces it stands in for. Clicking a face opens a
// bubble with every face of its slot; clicking a year (or a month) zooms into it.
const DAY = 864e5;
const faceAt = t => t.date ? Date.parse(`${t.date}T${t.time || '12:00:00'}Z`) : null;
const fmtDay = iso => { const [y, m, d] = iso.split('-').map(Number); return `${d} ${PL_MON[m - 1]} ${y}`; };
function personTlTicks(a, b) {
  const span = b - a, out = [], y0 = new Date(a).getUTCFullYear(), y1 = new Date(b).getUTCFullYear();
  if (span > 2.5 * 365 * DAY) {
    const step = span > 30 * 365 * DAY ? 5 : span > 12 * 365 * DAY ? 2 : 1;
    for (let y = y0; y <= y1 + 1; y++) {
      const t = Date.UTC(y, 0, 1);
      if (t >= a && t <= b && y % step === 0) out.push({ t, label: String(y), zoom: [t, Date.UTC(y + 1, 0, 1)] });
    }
  } else if (span > 60 * DAY) {
    const step = span > 400 * DAY ? 3 : 1;
    for (let y = y0; y <= y1; y++) for (let m = 0; m < 12; m++) {
      const t = Date.UTC(y, m, 1);
      if (t >= a && t <= b && m % step === 0)
        out.push({ t, label: m === 0 || step === 3 && m < 3 ? `${PL_MON[m]} ${y}` : PL_MON[m], zoom: [t, Date.UTC(y, m + 1, 1)] });
    }
  } else {
    const step = Math.max(1, Math.ceil(span / DAY / 10));
    for (let t = Date.UTC(new Date(a).getUTCFullYear(), new Date(a).getUTCMonth(), new Date(a).getUTCDate()); t <= b; t += step * DAY)
      if (t >= a) out.push({ t, label: fmtDay(new Date(t).toISOString().slice(0, 10)) });
  }
  return out;
}
function renderPersonTimeline() {
  const host = $('#personTl'), p = people.person;
  if (!host || !p) return;
  const dated = p.tracks.filter(t => t.date).map(t => ({ ...t, at: faceAt(t) }));
  if (!dated.length) { host.innerHTML = ''; return; }
  const ats = dated.map(t => t.at), first = Math.min(...ats), last = Math.max(...ats);
  let [a, b] = people.tlRange || [first, last];
  const shown = dated.filter(t => t.at >= a && t.at <= b);
  if (b - a < 20 * DAY) { const mid = (a + b) / 2; a = mid - 10 * DAY; b = mid + 10 * DAY; }
  const pad = (b - a) * 0.02; a -= pad; b += pad;
  // EDGE keeps the first/last thumbnail (and its "+n" badge) inside the frame
  const W = host.clientWidth || 800, TH = 44, SLOT = 26, EDGE = 10, inner = W - TH - 2 * EDGE, x0 = TH / 2 + EDGE;
  const n = Math.max(1, Math.floor(inner / SLOT)), x = t => x0 + (t - a) / (b - a) * inner;
  const slots = Array.from({ length: n }, () => []);
  shown.forEach(t => slots[Math.min(n - 1, Math.max(0, Math.floor((t.at - a) / (b - a) * n)))].push(t));
  const AXIS = 66;                                   // y of the line; rows above (0) and below (AXIS + 22)
  const canRemove = p.tracks.length > 1;
  const thumbs = slots.map((s, i) => {
    if (!s.length) return '';
    const t = s[0], cx = x0 + (i + 0.5) * inner / n, up = i % 2 === 0, top = up ? 0 : AXIS + 22;
    const more = s.length > 1 ? `<span class="absolute -right-1 ${up ? '-top-1' : '-bottom-1'} rounded-full bg-brand-500 px-1 text-[10px] font-semibold leading-4 text-white">+${s.length - 1}</span>` : '';
    return `<div class="absolute w-px bg-gray-300 dark:bg-gray-600" style="left:${cx}px;top:${up ? TH : AXIS}px;height:${up ? AXIS - TH : 22}px"></div>
      <div class="group absolute hover:z-10" style="left:${cx - TH / 2}px;top:${top}px">
        <button data-slot="${i}"
          title="${fmtDay(t.date)} · ${esc(t.tape_id)}${s.length > 1 ? ' · ' + L('people.tlMore', { n: s.length - 1 }) : ''}"
          class="relative block transition group-hover:scale-125">${avatar(t.id, 'h-11 w-11 ring-2 ring-white dark:ring-gray-900')}${more}</button>
        ${s.length === 1 && canRemove ? `<button data-face-out="${t.id}" title="${L('people.notThis')}"
          class="absolute -left-1.5 ${up ? '-top-1.5' : '-bottom-1.5'} grid h-5 w-5 place-items-center rounded-full bg-gray-800 text-[11px] text-white hover:bg-error-500 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100">✕</button>` : ''}
      </div>`;
  }).join('');
  const tickList = personTlTicks(a, b);
  // label only every k-th tick when they'd collide (narrow screens); the marks all stay
  const every = Math.max(1, Math.ceil(64 / (inner / Math.max(1, tickList.length))));
  const tickLabel = k => {
    const cls = 'absolute -translate-x-1/2 whitespace-nowrap text-[11px] text-gray-500 dark:text-gray-400';
    const pos = `left:${x(k.t)}px;top:${AXIS + 4}px`;
    return k.zoom
      ? `<button data-tl-zoom="${k.zoom.join(',')}" title="${L('people.tlZoom')}" class="${cls} hover:text-brand-500" style="${pos}">${k.label}</button>`
      : `<span class="${cls}" style="${pos}">${k.label}</span>`;
  };
  // the first / last recording's date pinned at the two ends of the axis; tick
  // labels that would run into them are left out (the tick marks stay)
  const sAts = shown.map(t => t.at), sFirst = Math.min(...sAts), sLast = Math.max(...sAts);
  const endW = 78, xFirst = x(sFirst), xLast = x(sLast);
  const ends = shown.length ? `
    <span class="absolute z-10 whitespace-nowrap rounded bg-gray-50 px-1 text-[11px] font-semibold text-gray-700 dark:bg-gray-900 dark:text-gray-200" style="left:${Math.max(0, xFirst - 4)}px;top:${AXIS + 4}px">${fmtDay(new Date(sFirst).toISOString().slice(0, 10))}</span>
    ${sLast - sFirst > DAY ? `<span class="absolute z-10 -translate-x-full whitespace-nowrap rounded bg-gray-50 px-1 text-[11px] font-semibold text-gray-700 dark:bg-gray-900 dark:text-gray-200" style="left:${Math.min(W, xLast + 4)}px;top:${AXIS + 4}px">${fmtDay(new Date(sLast).toISOString().slice(0, 10))}</span>` : ''}` : '';
  const clear = t => x(t) > Math.max(0, xFirst - 4) + endW + 8 && x(t) < Math.min(W, xLast + 4) - endW - 8;
  const ticks = tickList.map((k, i) => `<div class="absolute w-px bg-gray-300 dark:bg-gray-600" style="left:${x(k.t)}px;top:${AXIS - 4}px;height:9px"></div>
    ${i % every || !clear(k.t) ? '' : tickLabel(k)}`).join('') + ends;
  const undated = p.tracks.length - dated.length;
  personTlSlots = slots;
  host.classList.add('relative');
  host.innerHTML = `
    <div class="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
      <h4 class="text-theme-sm font-semibold text-gray-700 dark:text-gray-200">${L('people.tlTitle')}</h4>
      <span class="text-theme-xs text-gray-400">${fmtDay(new Date(first).toISOString().slice(0, 10))} – ${fmtDay(new Date(last).toISOString().slice(0, 10))}
        · ${L('people.tlCount', { n: shown.length })}</span>
      ${undated ? `<button data-tl-undated class="text-theme-xs text-brand-500 hover:underline">${L('people.tlUndated', { n: undated })}</button>` : ''}
      ${people.tlRange ? `<button data-tl-reset class="${BTN}">${L('people.tlAll')}</button>` : ''}
    </div>
    <div class="relative overflow-hidden rounded-xl border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-white/[0.02]" style="height:${AXIS + 22 + TH + 8}px">
      <div class="relative" style="margin:4px 0">
        <div class="absolute h-0.5 rounded-full bg-gray-300 dark:bg-gray-600" style="left:${x0}px;width:${inner}px;top:${AXIS}px"></div>
        ${ticks}${thumbs}
      </div>
    </div>`;
  // a review bubble that was open stays open across re-renders (e.g. after removing a
  // face), unless the width changed and the slots now mean something else
  const open = people.tlOpenSlot;
  if (open && open.width === W && tlBubbleFaces(open.slot).length) openTlSlot(open.slot);
  else people.tlOpenSlot = null;
}

// ---- review bubble: every face of a grouped slot (key = slot index) or the
// undated ones (key = 'undated'); play, remove, or fix the date of each ----
let personTlSlots = [];
function tlBubbleFaces(key) {
  const p = people.person;
  if (key === 'undated') return p ? p.tracks.filter(t => !t.date).map(t => ({ ...t, at: 0 })) : [];
  return personTlSlots[key] || [];
}
function openTlSlot(key) {
  const host = $('#personTl'), faces = tlBubbleFaces(key);
  const btn = host && host.querySelector(key === 'undated' ? '[data-tl-undated]' : `[data-slot="${key}"]`);
  if (!btn || !faces.length) return closeTlSlot();
  people.tlOpenSlot = { slot: key, width: host.clientWidth };
  const list = [...faces].sort((u, v) => u.at - v.at), canRemove = people.person.tracks.length > 1;
  const range = key === 'undated' ? L('people.tlNoDate')
    : list[0].date === list[list.length - 1].date ? fmtDay(list[0].date)
    : `${fmtDay(list[0].date)} – ${fmtDay(list[list.length - 1].date)}`;
  host.querySelector('#personTlPop')?.remove();
  const pop = document.createElement('div');
  pop.id = 'personTlPop';
  pop.className = 'absolute z-30 w-80 max-w-[calc(100%-0.5rem)] rounded-xl border border-gray-200 bg-white p-3 shadow-theme-lg dark:border-gray-700 dark:bg-gray-900';
  pop.innerHTML = `<div class="mb-2 flex flex-wrap items-center gap-2">
      <span class="text-theme-xs font-medium text-gray-700 dark:text-gray-200">${range} · ${L('people.tlCount', { n: list.length })}</span>
      ${key === 'undated' ? '' : `<button data-tl-unknown title="${L('people.tlUnknownHint')}" class="${BTN}">${L('people.tlUnknown')}</button>`}
      <button data-tl-pop-close class="ml-auto rounded-full px-1.5 text-gray-400 hover:text-gray-700 dark:hover:text-white" aria-label="Close">✕</button>
    </div>
    <div class="grid max-h-72 grid-cols-4 gap-2 overflow-y-auto pr-1">${list.map(t => `
      <div class="relative flex flex-col items-center">
        <button data-face-seek="${t.id}" title="${esc(t.tape_id)} · ▶ ${mmss(t.first)}">${avatar(t.id, 'h-14 w-14 hover:ring-2 hover:ring-brand-500')}</button>
        ${canRemove ? `<button data-face-out="${t.id}" title="${L('people.notThis')}" class="absolute right-0 top-0 grid h-5 w-5 place-items-center rounded-full bg-gray-800 text-[11px] text-white hover:bg-error-500">✕</button>` : ''}
        <button data-face-date="${t.id}" title="${L('people.tlSetDate')}" class="absolute left-0 top-0 grid h-5 w-5 place-items-center rounded-full bg-white text-[11px] shadow-theme-xs hover:bg-brand-50 dark:bg-gray-800">📅</button>
        <span class="mt-0.5 text-[10px] text-gray-500 dark:text-gray-400">${t.date ? fmtDay(t.date) : L('people.tlNoDate')}</span>
      </div>`).join('')}</div>`;
  host.appendChild(pop);
  // under whatever was clicked, kept inside the timeline box
  const hr = host.getBoundingClientRect(), br = btn.getBoundingClientRect();
  const left = Math.min(Math.max(0, br.left - hr.left + br.width / 2 - pop.offsetWidth / 2), hr.width - pop.offsetWidth);
  pop.style.left = `${left}px`;
  pop.style.top = `${br.bottom - hr.top + 6}px`;
}
// value: "YYYY-MM-DD", "unknown" (off every timeline) or "" (back to the automatic date)
async function setFaceDates(faces, value) {
  const byTape = {};
  faces.forEach(t => { (byTape[t.tape_id] = byTape[t.tape_id] || {})[t.scene_id] = value; });
  try {
    for (const [tape, dates] of Object.entries(byTape))
      await postJSON(`/api/tapes/${encodeURIComponent(tape)}/scene-dates`, { dates });
  } catch (e) { alert(e.message); }
  closeTlSlot(); people.list = null; await loadPeople(true);
}
function closeTlSlot() {
  people.tlOpenSlot = null;
  $('#personTlPop')?.remove();
}
document.addEventListener('keydown', e => { if (e.key === 'Escape' && $('#personTlPop')) closeTlSlot(); });
let personTlFrame = 0;
new ResizeObserver(() => { cancelAnimationFrame(personTlFrame); personTlFrame = requestAnimationFrame(renderPersonTimeline); })
  .observe($('#peopleView'));

$('#peopleView').addEventListener('change', e => {
  if (e.target.id !== 'personGroup') return;
  const p = people.person;
  let group = e.target.value;
  if (group === '__new') {
    group = (prompt(L('people.grp.prompt')) || '').trim();
    if (!group) { e.target.value = p.group || ''; return; }
  }
  personAction(async () => { await postJSON(`/api/persons/${p.id}`, { group }); people.list = null; await loadPeople(true); });
});

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
  if (e.target.id === 'peopleSugAll') { people.sugAll = !people.sugAll; renderPeople(); return; }
  if (e.target.id === 'peopleSelect') { people.selecting = true; people.sel = new Set(); renderPeople(); return; }
  if (e.target.id === 'peopleSelCancel') { people.selecting = false; people.sel = new Set(); renderPeople(); return; }
  const sp = e.target.closest('[data-sel-person]');
  if (sp) { const id = +sp.dataset.selPerson; people.sel.has(id) ? people.sel.delete(id) : people.sel.add(id); renderPeople(); return; }
  if (e.target.id === 'peopleSelDissolve' || e.target.id === 'peopleSelDelete') {
    const dissolve = e.target.id === 'peopleSelDissolve', ids = [...people.sel];
    const faces = (people.list || []).filter(p => people.sel.has(p.id)).reduce((n, p) => n + p.tracks, 0);
    if (!ids.length || !confirm(L(dissolve ? 'people.confirmDissolve' : 'people.confirmDelete', { n: ids.length, faces }))) return;
    personAction(async () => {
      await postJSON(`/api/persons/${dissolve ? 'dissolve' : 'delete'}`, { ids });
      people.selecting = false; people.sel = new Set(); await loadPeople(true);
    });
    return;
  }
  const gf = e.target.closest('[data-group-filter]');
  if (gf) { people.groupFilter = gf.dataset.groupFilter; renderPeople(); return; }
  const yes = e.target.closest('[data-sugg-yes]'), no = e.target.closest('[data-sugg-no]');
  if (yes || no) {
    const [pid, other] = (yes || no).dataset[yes ? 'suggYes' : 'suggNo'].split(':').map(Number);
    (yes || no).disabled = true;
    personAction(async () => {
      if (yes) await postJSON(`/api/persons/${pid}/merge`, { into: other });
      else await postJSON(`/api/persons/${pid}/reject`, { other });
      people.list = null;
      if (currentRoute().person === pid) go(yes ? '#osoby/' + other : '#osoby');   // the unnamed one is gone / done
      else loadPeople(true);
    });
    return;
  }
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
  if (e.target.id === 'personDissolve' || e.target.id === 'personDelete') {
    const dissolve = e.target.id === 'personDissolve';
    if (!confirm(L(dissolve ? 'people.confirmDissolve' : 'people.confirmDelete', { n: 1, faces: p.tracks.length }))) return;
    personAction(async () => {
      await postJSON(`/api/persons/${dissolve ? 'dissolve' : 'delete'}`, { ids: [p.id] });
      people.list = null; people.person = null; go('#osoby');
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
  const zoom = e.target.closest('[data-tl-zoom]');
  if (zoom) { people.tlRange = zoom.dataset.tlZoom.split(',').map(Number); closeTlSlot(); renderPersonTimeline(); return; }
  if (e.target.closest('[data-tl-reset]')) { people.tlRange = null; closeTlSlot(); renderPersonTimeline(); return; }
  if (e.target.closest('[data-tl-pop-close]')) { closeTlSlot(); return; }
  if (e.target.closest('[data-tl-undated]')) {
    people.tlOpenSlot && people.tlOpenSlot.slot === 'undated' ? closeTlSlot() : openTlSlot('undated');
    return;
  }
  if (e.target.closest('[data-tl-unknown]')) {
    const faces = tlBubbleFaces(people.tlOpenSlot.slot);
    if (confirm(L('people.confirmUnknown', { n: faces.length }))) setFaceDates(faces, 'unknown');
    return;
  }
  const fd = e.target.closest('[data-face-date]');
  if (fd) {
    const t = people.person.tracks.find(x => x.id === +fd.dataset.faceDate);
    const v = prompt(L('people.datePrompt'), t && t.date ? t.date : '');
    if (v === null) return;
    const val = v.trim() === '?' ? 'unknown' : v.trim();
    if (val && val !== 'unknown' && !/^\d{4}-\d{2}-\d{2}$/.test(val)) { alert(L('people.dateBad')); return; }
    setFaceDates([t], val);
    return;
  }
  const slotBtn = e.target.closest('[data-slot]');
  if (slotBtn) {
    const i = +slotBtn.dataset.slot;
    if (people.tlOpenSlot && people.tlOpenSlot.slot === i) closeTlSlot(); else openTlSlot(i);
    return;
  }
  if ($('#personTlPop') && !e.target.closest('#personTlPop')) closeTlSlot();   // click outside closes it
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

// ============ photos & videos: albums = source folders ============
let mediaState = { albums: null, status: null, album: null, items: [], sig: '' };
const plural = (n, one, few, many) => `${n} ${n === 1 ? one
  : window.i18n.lang !== 'pl' || (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20)) ? few : many}`;
const mediaCount = (p, v) => [
  p ? (window.i18n.lang === 'pl' ? plural(p, 'zdjęcie', 'zdjęcia', 'zdjęć') : plural(p, 'photo', 'photos')) : '',
  v ? (window.i18n.lang === 'pl' ? plural(v, 'film', 'filmy', 'filmów') : plural(v, 'video', 'videos')) : '',
].filter(Boolean).join(' · ') || '0';
function renderMediaBar() {
  const st = mediaState.status; if (!st) return;
  if (!st.enabled) { $('#mediaBar').innerHTML = `<p class="text-theme-xs text-gray-400">${L('media.disabled')}</p>`; return; }
  const pending = (st.new || 0) + (st.proxies_pending || 0);
  $('#mediaBar').innerHTML = `<div class="flex flex-wrap items-center gap-3 text-theme-xs text-gray-500 dark:text-gray-400">
    <span>${mediaCount(st.photos || 0, st.videos || 0)}</span>
    ${st.new ? `<span class="text-brand-500">${L('media.pending', { n: st.new })}</span>` : ''}
    ${st.proxies_pending ? `<span>${L('media.proxies', { n: st.proxies_pending })}</span>` : ''}
    ${st.error ? `<span class="text-orange-500">${L('media.errors', { n: st.error })}</span>` : ''}
    ${st.scanned_at ? `<span>${L('media.scannedAt', { t: fmtDT(st.scanned_at) })}</span>` : ''}
    <button id="mediaRescan" class="${BTN}" ${pending ? 'title="' + L('media.busy') + '"' : ''}>${L('media.rescan')}</button>
  </div>`;
}
async function loadMedia(force) {
  try {
    const st = await api('/api/media/status');
    const sig = [st.ready, st.new, st.proxies_pending].join('|');
    if (force || sig !== mediaState.sig || !mediaState.albums) {
      mediaState.sig = sig;
      mediaState.albums = await api(`/api/media/albums${wsQS()}`);
      const r = currentRoute();
      if (r.album != null) mediaState.items = await api(`/api/media/album?name=${encodeURIComponent(r.album)}${wsQS().replace('?', '&')}`);
      mediaState.status = st; renderMediaBar(); renderMedia();
    } else { mediaState.status = st; renderMediaBar(); }
  } catch (e) { $('#mediaView').innerHTML = `<p class="text-error-500">${e.message}</p>`; }
}
let mediaGridItems = [];
function renderMedia() {
  const view = $('#mediaView'), r = currentRoute();
  if (r.album != null) {
    mediaGridItems = mediaState.items.map(mediaItem);
    const undated = mediaState.items.filter(m => !m.date).length;
    view.innerHTML = (undated ? `<div class="mb-4 flex flex-wrap items-center gap-3 text-theme-xs text-gray-500 dark:text-gray-400">
        <span>${L('media.albumUndated', { n: undated, total: mediaState.items.length })}</span>
        <button id="mediaAlbumDate" class="${BTN}">${L('media.albumDate')}</button></div>` : '')
      + (mediaGridItems.length ? sceneGridHTML(mediaGridItems) : `<p class="text-theme-sm text-gray-400">${L('scenes.empty')}</p>`);
    return;
  }
  const albums = mediaState.albums || [];
  if (!albums.length) { view.innerHTML = `<p class="text-theme-sm text-gray-400">${L(mediaState.status && mediaState.status.enabled ? 'media.empty' : 'media.disabled')}</p>`; return; }
  const range = a => !a.date_min ? '' : a.date_min.slice(0, 4) === a.date_max.slice(0, 4) ? a.date_min.slice(0, 4) : `${a.date_min.slice(0, 4)} – ${a.date_max.slice(0, 4)}`;
  view.innerHTML = `<div class="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">${albums.map(a => {
    const name = a.album.split('/').pop(), parent = a.album.split('/').slice(0, -1).join(' / ');
    const cell = id => id ? `<img loading="lazy" class="h-full w-full object-cover" src="${mediaThumb(id)}" alt="">` : '<div class="h-full w-full bg-gray-100 dark:bg-white/[0.04]"></div>';
    const mosaic = a.cover.length < 4 ? cell(a.cover[0])
      : `<div class="grid h-full w-full grid-cols-2 grid-rows-2 gap-0.5">${[0, 1, 2, 3].map(i => cell(a.cover[i])).join('')}</div>`;
    return `<button data-go="#media/${encodeURIComponent(a.album)}" class="${TLTILE}">
      <div class="aspect-square overflow-hidden rounded-xl bg-black">${mosaic}</div>
      <div class="mt-2 px-0.5">
        <div class="truncate font-semibold text-gray-800 group-hover:text-brand-500 dark:text-white/90">${esc(name)}</div>
        <div class="truncate text-theme-xs text-gray-500 dark:text-gray-400">${mediaCount(a.photos, a.videos)}${range(a) ? ' · ' + range(a) : ''}</div>
        ${parent ? `<div class="truncate text-[11px] text-gray-400">${esc(parent)}</div>` : ''}
      </div>
    </button>`;
  }).join('')}</div>`;
}
$('#mediaView').addEventListener('click', async e => {
  const nav = e.target.closest('[data-go]'); if (nav) { go(nav.dataset.go); return; }
  if (e.target.id === 'mediaAlbumDate') {
    const v = (prompt(L('media.albumDatePrompt')) || '').trim();
    if (!v) return;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) { alert(L('people.dateBad')); return; }
    try {
      await postJSON(`/api/media/album/date${wsQS()}`, { album: currentRoute().album, date: v });
      tl.data = null; loadMedia(true);
    } catch (err) { alert(err.message); }
    return;
  }
  const gt = e.target.closest('[data-mkey]'); if (gt) openSceneModal(mediaGridItems, +gt.dataset.mkey);
});
$('#mediaBar').addEventListener('click', async e => {
  if (e.target.id !== 'mediaRescan') return;
  try { await postJSON('/api/media/rescan'); e.target.textContent = L('build.wait.queued'); } catch (err) { alert(err.message); }
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
  if (b.mode === 'media') return L('build.mediaLabel');
  if (t.startsWith('REPROBE') || t === 'METABACKFILL' || t.startsWith('SEL-') || t.startsWith('DEL-') || ['TAPE', 'tape'].includes(t)) return b.tape_id;
  return `${b.tape_id} · ${t.replace(/-RES$/, '')}`;
};
const BUILD_DOWNLOADABLE = new Set(['concat', 'restore', 'share', 'archive', 'montage']);
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
        <span class="font-mono text-[11px] text-gray-400">${fmtTime(j.updated_at)}</span>
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
      live && b.eta_s && L('job.eta', { t: fmtDuration(b.eta_s) })].filter(Boolean).join(' · ')
      : b.mode === 'media' ? [live && b.stage && L('media.stage.' + b.stage), b.detail && esc(b.detail),
        live && b.total ? `${(b.done ?? 0) + 1}/${b.total}` : ''].filter(Boolean).join(' · ') : '';
    return `<div class="rounded-xl border border-gray-200 p-3 dark:border-gray-800">
      <div class="flex flex-wrap items-center gap-2">
        <span class="${BADGE} ${cls}">${buildMode(b.mode)}</span>
        <strong class="text-gray-800 dark:text-white/90">${buildLabel(b)}</strong>
        <span class="font-mono text-theme-xs text-gray-600 dark:text-gray-300">${buildSt(b.status)}${pct != null ? ` · ${pct}%` : ''}${b.size ? ` · ${mb(b.size)}` : ''}</span>
        ${dlUrl ? `<a class="${BTN_PRIMARY}" href="${dlUrl}" download>${L('build.download')}</a>` : ''}
        <span class="ml-auto font-mono text-[11px] text-gray-400">${fmtTime(b.updated_at)}</span>
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
    if (currentRoute().section === 'media') loadMedia(false);   // live while files are being indexed

    // timeline is derived from the tape set — refresh it when that changes and it's on screen
    const tsig = JSON.stringify(t.map(x => [x.tape_id, x.scene_count, x.recording_date, x.undated_date, x.label]));
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
const SECTIONS = ['pulpit', 'zadania', 'kasety', 'media', 'duplikaty', 'timeline', 'osoby', 'logi', 'info'];

function currentRoute() {
  const parts = location.hash.replace(/^#/, '').split('/').map(decodeURIComponent);
  const section = SECTIONS.includes(parts[0]) ? parts[0] : 'pulpit';
  const r = { section, tape: null, scene: null, year: null, month: null, person: null, album: null };
  if (section === 'media' && parts.length > 1 && parts[1]) r.album = parts.slice(1).join('/');
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
  } else if (r.section === 'media' && r.album != null) {
    html = `<button data-go="#media" class="inline-flex items-center gap-1 text-gray-600 hover:text-brand-500 dark:text-gray-300">${L('media.crumbAll')}</button>
      <span class="text-gray-300 dark:text-gray-600">/</span><span class="truncate text-gray-700 dark:text-gray-200">${esc(r.album)}</span>`;
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
    if (r.section === 'media') { mediaState.items = []; $('#mediaView').innerHTML = `<p class="text-theme-sm text-gray-400">${L('loading')}</p>`; loadMedia(true); }
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
