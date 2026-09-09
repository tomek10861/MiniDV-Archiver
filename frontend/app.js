const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const gib = n => `${(n / 1024 ** 3).toFixed(1)} GiB`;
const mb = n => n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB` : `${(n / 1024 ** 2).toFixed(0)} MB`;
const TERMINAL = new Set(['COMPLETED', 'ERROR', 'CANCELLED']);
const STATE_PL = {
  IDLE: 'BEZCZYNNY', CREATED: 'UTWORZONE', WAITING_FOR_PLAY: '▶ CZEKAM NA PLAY — naciśnij PLAY na kamerze',
  CAPTURING: 'NAGRYWANIE', REWINDING: 'PRZEWIJANIE', CHECKING_STORAGE: 'SPRAWDZANIE DYSKU',
  CHECKING_CAMERA: 'SPRAWDZANIE KAMERY', CAPTURED: 'ZGRANE — CZEKA NA PRZETWARZANIE', QUEUED: 'W KOLEJCE',
  DETECTING_SCENES: 'WYKRYWANIE SCEN', COMPRESSING: 'KOMPRESJA', VERIFYING_ARCHIVES: 'WERYFIKACJA ARCHIWUM',
  ENCODING_MP4: 'KODOWANIE MP4', VERIFYING_MP4: 'WERYFIKACJA MP4', ANALYZING_DV: 'ANALIZA DV',
  BUILDING_TAPE_PROXY: 'SKLEJANIE PODGLĄDU TAŚMY', COMPLETED: 'GOTOWE', ERROR: 'BŁĄD', CANCELLED: 'PRZERWANE',
};
const stPL = s => STATE_PL[s] || s || '—';
const durTxt = (frames, std) => {
  const t = Math.round(frames / (std === 'NTSC' ? 30000 / 1001 : 25));
  return `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`;
};
const badYear = d => d && !/^(19[89]\d|20[0-2]\d)-/.test(d);
const PL_MON = ['sty', 'lut', 'mar', 'kwi', 'maj', 'cze', 'lip', 'sie', 'wrz', 'paź', 'lis', 'gru'];
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
          <div class="truncate text-theme-xs text-gray-500 dark:text-gray-400">${t.label ? t.tape_id + ' · ' : ''}${t.scene_count} scen${date ? ` · ${date}` : ''}</div>
        </div>
      </button>`;
    }).join('') || '<p class="col-span-full text-theme-sm text-gray-400">Brak zarchiwizowanych kaset.</p>';
  }
  // keep an open detail view fresh
  if (openTapeId) {
    const t = list.find(x => x.tape_id === openTapeId);
    if (t) renderTapeDetail(t);
  }
}

function sceneCard(id, s, hasFull) {
  const enc = encodeURIComponent(id), sid = s.scene_id, f = s.files || {}, tc = s.timecode || {},
    rec = s.recording || {}, v = s.video || {}, arch = f.archive || {}, prox = f.proxy || {};
  const at = ((s.source || {}).start_frame || 0) / (v.standard === 'NTSC' ? 30000 / 1001 : 25);
  const drops = (s.capture || {}).dropped_frames || 0, disc = ((s.capture || {}).source_discontinuities || []).length;
  const flags = [drops && `⚠ ${drops} zgub. klatka`, disc && `⚠ ${disc}× nieciągłość`,
    badYear(rec.datetime) && '⚠ zegar kamery błędny'].filter(Boolean);
  const file = n => `/api/tapes/${enc}/files/${encodeURIComponent(sid)}.${n}`;
  const on = selected.has(sid);
  return `<div class="flex gap-4 rounded-xl border ${on ? 'border-brand-500 bg-brand-50/60 dark:bg-brand-500/[0.08]' : 'border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-white/[0.02]'} p-3">
    <label class="relative shrink-0 cursor-pointer">
      <img loading="lazy" src="/api/tapes/${enc}/files/thumbnails/${encodeURIComponent(sid)}.jpg" alt=""
        class="h-24 w-32 rounded-lg bg-black object-cover ${hasFull ? 'group-[.playable]:cursor-pointer' : ''}">
      <input type="checkbox" class="scenesel absolute left-1.5 top-1.5 h-4 w-4 accent-brand-500" data-scene="${sid}" ${on ? 'checked' : ''}>
    </label>
    <div class="min-w-0 flex-1">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
        <strong class="text-gray-800 dark:text-white/90">Scena ${s.scene_index}</strong>
        <span class="font-mono text-theme-xs text-gray-500 dark:text-gray-400">${tc.start || '?'} – ${tc.end || '?'} · ${durTxt(s.frame_count, v.standard)} · ${s.frame_count} kl.</span>
      </div>
      <div class="mt-1 text-theme-xs text-gray-500 dark:text-gray-400">${v.standard || ''} ${v.resolution || ''} ${v.frame_rate || ''} ·
        nagrano ${rec.datetime ? rec.datetime.replace('T', ' ') : '—'} ·
        DV ${mb(arch.size_uncompressed || 0)} → zst ${mb(arch.size_compressed || 0)}${arch.verified_byte_for_byte ? ' ✓' : ''}</div>
      ${flags.length ? `<div class="mt-1 text-theme-xs text-orange-500">${flags.join(' · ')}</div>` : ''}
      <div class="mt-2 flex flex-wrap items-center gap-2 text-theme-xs">
        ${hasFull ? `<button class="seek ${BTN}" data-seek="${at.toFixed(2)}">⏱ w całej taśmie</button>` : ''}
        <button class="preview ${BTN}" data-src="${file('mp4')}">▶ Podgląd</button>
        <button class="fb ${BTN_FB}" data-tape="${id}" data-scene="${sid}">⬇ FB (~90 MB)</button>
        <a class="${LINK}" href="${file('dv.zst')}?dl=1">⬇ DV .dv.zst${arch.size_compressed ? ` (${mb(arch.size_compressed)})` : ''}</a>
        <a class="${LINK}" href="${file('mp4')}?dl=1">⬇ MP4${prox.size ? ` (${mb(prox.size)})` : ''}</a>
        <a class="${LINK}" href="${file('json')}?dl=1">⬇ JSON</a>
      </div>
    </div></div>`;
}

async function openTape(id) {
  openTapeId = id; selected.clear();
  $('#tapeGrid').classList.add('hidden');
  $('#tapeDetail').classList.remove('hidden');
  $('#tapeBack').classList.remove('hidden');
  $('#tapeDetail').innerHTML = '<p class="text-theme-sm text-gray-400">Wczytywanie…</p>';
  try {
    scenesCache[id] = await api(`/api/tapes/${encodeURIComponent(id)}/scenes`);
  } catch (e) { $('#tapeDetail').innerHTML = `<p class="text-error-500">${e.message}</p>`; return; }
  const t = (window.__tapes || []).find(x => x.tape_id === id) || { tape_id: id };
  renderTapeDetail(t, true);
}
function closeTape() {
  openTapeId = null; selected.clear(); detailSig = '';
  $('#tapeDetail').dataset.tape = '';
  $('#tapeGrid').classList.remove('hidden');
  $('#tapeDetail').classList.add('hidden');
  $('#tapeBack').classList.add('hidden');
}

let detailSig = '';
function renderTapeDetail(t, force) {
  const id = t.tape_id, e = encodeURIComponent(id), full = t.proxy_full;
  const scenes = scenesCache[id] || [];
  // Only rebuild when something actually changed — otherwise the 3 s refresh would
  // wipe any open <video> the user just started ("preview closes after a second").
  const sig = JSON.stringify([id, t.scene_count, t.label, t.recording_date, !!full,
    (full || {}).size, scenes.length, [...selected].sort()]);
  if (!force && sig === detailSig && $('#tapeDetail').dataset.tape === id) return;
  detailSig = sig;
  $('#tapeDetail').dataset.tape = id;
  $('#tapeDetail').innerHTML = `
    <div class="flex flex-wrap items-center gap-3">
      <h3 class="text-lg font-semibold text-brand-500">${id}</h3>
      ${t.label ? `<span class="rounded bg-gray-100 px-2 py-0.5 text-theme-xs text-gray-700 dark:bg-gray-800 dark:text-gray-200">🏷️ ${t.label}</span>` : ''}
      <span class="font-mono text-theme-xs text-gray-500 dark:text-gray-400">${t.scene_count ?? scenes.length} scen · ${t.recording_date ? '📅 ' + t.recording_date : (t.capture_completed_at || '').slice(0, 19).replace('T', ' ')}</span>
    </div>
    <div class="mt-2 flex flex-wrap items-center gap-2 text-theme-xs">
      <button id="tapeRename" class="${BTN}">✏️ Zmień nazwę</button>
      <button id="tapeLabel" class="${BTN}">🏷️ Etykieta</button>
      <button id="tapeDate" class="${BTN}">📅 Data nagrania</button>
    </div>
    <div class="tape-tools mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-theme-xs">
      ${full ? `<button class="fulltape ${BTN_PRIMARY}">▶ Odtwórz całą taśmę${full.size ? ` (${mb(full.size)})` : ''}</button>` : '<span class="text-gray-400">Podgląd całej taśmy powstanie przy następnej archiwizacji.</span>'}
      <a class="${LINK}" href="/api/tapes/${e}/files/tape.json?dl=1">tape.json</a>
      <a class="${LINK}" href="/api/tapes/${e}/files/tape.sha256?dl=1">tape.sha256</a>
      <a class="${LINK}" href="/api/tapes/${e}/files/capture.log?dl=1">capture.log</a>
      ${full ? `<a class="${LINK}" href="/api/tapes/${e}/files/tape.mp4?dl=1">⬇ tape.mp4</a>` : ''}
      ${full ? `<button class="fb ${BTN_FB}" data-tape="${id}">⬇ FB cała taśma</button>` : ''}
      <button id="tapeDelete" class="${BTN_DANGER}">🗑 Usuń kasetę</button>
    </div>
    <div id="selBar" class="mt-3 hidden flex-wrap items-center gap-3 rounded-xl border border-brand-500/40 bg-brand-50/60 p-3 dark:bg-brand-500/[0.08]">
      <span class="text-sm">Zaznaczono <b id="selCount">0</b></span>
      <button id="selDownload" class="${BTN_PRIMARY}">⬇ Pobierz jako jeden film</button>
      <button id="selFb" class="${BTN_FB}">⬇ FB (jeden film ~90 MB)</button>
      <button id="selDelete" class="${BTN_DANGER}">🗑 Usuń zaznaczone</button>
      <button id="selAll" class="${BTN}">zaznacz wszystkie</button>
      <button id="selClear" class="${BTN}">wyczyść</button>
    </div>
    <div id="detailScenes" class="playable mt-4 grid gap-3">${scenes.map(s => sceneCard(id, s, !!full)).join('') || '<p class="text-theme-sm text-gray-400">Brak scen.</p>'}</div>`;
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
      btn.textContent = j.status === 'QUEUED' ? '⏳ w kolejce…' : '⏳ przetwarzanie…';
      await new Promise(r => setTimeout(r, 2500));
      j = await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    }
    if (j.status !== 'READY') throw Error(j.error || 'błąd przetwarzania');
    btn.textContent = `${label}${j.size ? ` (${mb(j.size)})` : ''}`;
    const a = document.createElement('a'); a.href = downloadUrl(j); document.body.appendChild(a); a.click(); a.remove();
  } catch (e) { alert(e.message); btn.textContent = label; }
  finally { btn.disabled = false; }
}
function fbCompress(btn) {
  const tape = btn.dataset.tape, scene = btn.dataset.scene || null, label = btn.textContent;
  const base = scene ? `/api/tapes/${encodeURIComponent(tape)}/scenes/${encodeURIComponent(scene)}` : `/api/tapes/${encodeURIComponent(tape)}`;
  const name = scene ? encodeURIComponent(scene) + '.mp4' : 'TAPE.mp4';
  pollBuild(base + '/compress', null, btn, label, () => `/api/tapes/${encodeURIComponent(tape)}/compressed/${name}`);
}
function downloadSelection(btn, share) {
  if (!openTapeId || selected.size === 0) return;
  const tape = openTapeId, scenes = [...selected], label = btn.textContent;
  pollBuild(`/api/tapes/${encodeURIComponent(tape)}/compress`, { scenes, share: !!share }, btn, label,
    j => `/api/tapes/${encodeURIComponent(tape)}/compressed/${j.token}.mp4`);
}

async function reloadOpenTape() {
  if (!openTapeId) return;
  try { scenesCache[openTapeId] = await api(`/api/tapes/${encodeURIComponent(openTapeId)}/scenes`); } catch (_) {}
  await refresh();
  if (openTapeId) renderTapeDetail(currentTape());
}
async function deleteWholeTape(id, count) {
  if (!confirm(`Usunąć całą kasetę ${id} (${count} scen)?\nWszystkie mastery .dv.zst przepadną BEZPOWROTNIE.`)) return;
  if (prompt(`Aby potwierdzić skasowanie, wpisz nazwę kasety:\n${id}`) !== id) { alert('Anulowano — nazwa nie zgadza się.'); return; }
  try {
    await api(`/api/tapes/${encodeURIComponent(id)}`, { method: 'DELETE' });
    closeTape(); await refresh();
  } catch (e) { alert(e.message); }
}
async function renameTape(id) {
  const nn = (prompt('Nowa nazwa kasety (litery, cyfry, . _ -):', id) || '').trim();
  if (!nn || nn === id) return;
  try {
    const r = await api(`/api/tapes/${encodeURIComponent(id)}/rename`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ new_id: nn }) });
    openTapeId = r.tape_id; await refresh(); openTape(r.tape_id);
  } catch (e) { alert(e.message); }
}
async function editTapeMeta(id, field) {
  const cur = currentTape()[field] || '';
  const q = field === 'label' ? 'Etykieta / opis kasety (puste = usuń):' : 'Data nagrania RRRR-MM-DD (puste = usuń):';
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
  if (!confirm(`Usunąć ${n} zaznaczonych scen z ${openTapeId}?\nMastery .dv.zst tych scen przepadną BEZPOWROTNIE.`)) return;
  if (!confirm(`Na pewno? Tej operacji NIE DA SIĘ cofnąć.`)) return;
  try {
    await api(`/api/tapes/${encodeURIComponent(openTapeId)}/scenes`,
      { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scenes: [...selected] }) });
    selected.clear();
    await reloadOpenTape();
  } catch (e) { alert(e.message); }
}

$('#tapeBack').onclick = closeTape;
$('#tapeGrid').addEventListener('click', e => {
  const tile = e.target.closest('.tile'); if (tile) openTape(tile.dataset.id);
});
$('#tapeDetail').addEventListener('click', e => {
  const cb = e.target.closest('.scenesel');
  if (cb) {
    cb.checked ? selected.add(cb.dataset.scene) : selected.delete(cb.dataset.scene);
    e.target.closest('.flex')?.classList.toggle('border-brand-500', cb.checked);
    syncSelBar(); return;
  }
  if (e.target.id === 'selClear') { selected.clear(); renderTapeDetail(currentTape()); return; }
  if (e.target.id === 'selAll') { (scenesCache[openTapeId] || []).forEach(s => selected.add(s.scene_id)); renderTapeDetail(currentTape()); return; }
  if (e.target.id === 'selDownload') { downloadSelection(e.target, false); return; }
  if (e.target.id === 'selFb') { downloadSelection(e.target, true); return; }
  if (e.target.id === 'selDelete') { deleteSelectedScenes(); return; }
  if (e.target.id === 'tapeDelete') { deleteWholeTape(openTapeId, (scenesCache[openTapeId] || []).length); return; }
  if (e.target.id === 'tapeRename') { renameTape(openTapeId); return; }
  if (e.target.id === 'tapeLabel') { editTapeMeta(openTapeId, 'label'); return; }
  if (e.target.id === 'tapeDate') { editTapeMeta(openTapeId, 'recording_date'); return; }
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

// ============ jobs ============
function jobBadge(j, queue) {
  if (j.stage === 'capture') return ['ZGRYWANIE', 'bg-brand-500 text-white'];
  if (j.status === 'QUEUED' || queue.includes(j.tape_id)) return ['W KOLEJCE', 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200'];
  if (j.status === 'COMPLETED') return ['GOTOWE', 'bg-success-50 text-success-700 dark:bg-success-500/15 dark:text-success-400'];
  if (j.status === 'ERROR') return ['BŁĄD', 'bg-error-50 text-error-700 dark:bg-error-500/15 dark:text-error-400'];
  if (j.status === 'CANCELLED') return ['PRZERWANE', 'bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300'];
  return ['PRZETWARZANIE', 'bg-blue-light-50 text-blue-light-700 dark:bg-blue-light-500/15 dark:text-blue-light-400'];
}
let jobsSig = '';
function renderJobs(list, queue) {
  const sig = JSON.stringify(list.map(j => [j.tape_id, j.status, j.current_scene]));
  if (sig === jobsSig) return; jobsSig = sig;
  $('#jobs').innerHTML = list.map(j => {
    const [txt, cls] = jobBadge(j, queue), run = !TERMINAL.has(j.status);
    return `<div class="rounded-xl border border-gray-200 p-3 dark:border-gray-800">
      <div class="flex flex-wrap items-center gap-2">
        <span class="${BADGE} ${cls}">${txt}</span>
        <strong class="text-gray-800 dark:text-white/90">${j.tape_id}</strong>
        <span class="font-mono text-theme-xs text-gray-600 dark:text-gray-300">${stPL(j.status)}${j.current_scene ? ` · ${j.current_scene}` : ''}${j.dropped_frames ? ` · ⚠ ${j.dropped_frames} zgub.` : ''}</span>
        <span class="ml-auto font-mono text-[11px] text-gray-400">${(j.updated_at || '').slice(11, 19)}</span>
        ${run ? `<button class="jstop ${BTN}" data-tape="${j.tape_id}">Przerwij</button>` : ''}
      </div>
      ${j.error ? `<div class="mt-1.5 text-theme-xs text-error-500">${j.error}</div>` : ''}
    </div>`;
  }).join('') || '<p class="text-theme-sm text-gray-400">Brak zadań.</p>';
}
$('#jobs').addEventListener('click', async e => {
  const b = e.target.closest('.jstop'); if (!b) return;
  if (!confirm(`Przerwać zadanie ${b.dataset.tape}?`)) return;
  try {
    await api('/api/capture/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tape_id: b.dataset.tape }) });
    refresh();
  } catch (err) { alert(err.message); }
});

// ============ logs ============
let logPickSig = '';
function renderLogs(s) {
  const jobs = s.jobs || [];
  const opts = [];
  if (s.capture) opts.push(['__capture__', `▶ zgrywanie ${s.capture.tape_id}`]);
  jobs.forEach(j => opts.push([j.tape_id, `${j.tape_id} — ${stPL(j.status)}`]));
  const sig = JSON.stringify(opts.map(o => o[0]));
  const pick = $('#logPick');
  if (sig !== logPickSig) {
    logPickSig = sig;
    const cur = pick.value;
    pick.innerHTML = opts.map(([v, t]) => `<option value="${v}">${t}</option>`).join('') || '<option>brak zadań</option>';
    if ([...pick.options].some(o => o.value === cur)) pick.value = cur;
  }
  const key = pick.value;
  const job = key === '__capture__' ? s.capture : jobs.find(j => j.tape_id === key);
  const txt = (job && job.logs) || (s.capture ? s.capture.logs : '') || '—';
  const view = $('#logView');
  if (view.textContent !== txt) {
    const atBottom = $('#logFollow').checked;
    view.textContent = txt;
    if (atBottom) view.scrollTop = view.scrollHeight;
  }
}
$('#logPick').addEventListener('change', () => { logPickSig = ''; refresh(); });

// ============ main refresh loop ============
async function refresh() {
  try {
    const [s, d, t] = await Promise.all([api('/api/status'), api('/api/storage'), api('/api/tapes')]);
    window.__tapes = t;
    const c = s.camera, on = c.connected;
    $('#camPill').textContent = on ? 'KAMERA ONLINE' : 'BRAK KAMERY';
    $('#camPill').className = `inline-flex items-center rounded-full border px-3 py-1.5 text-theme-xs font-medium ${on ? 'border-success-500 text-success-600 dark:text-success-400' : 'border-error-500 text-error-600 dark:text-error-400'}`;
    $('#stCam').textContent = on ? (c.model_name || 'KAMERA') : 'OFFLINE';
    $('#stCamSub').textContent = on ? `${c.transport} · ${c.mode === 'manual' ? 'ręczny' : 'auto AV/C'}`
      : (c.guid ? `guid ${c.guid}` : 'brak urządzenia AV/C');
    $('#sideCam').textContent = on ? `${c.model_name || 'camera'}\n${c.device || ''}` : 'kamera offline';

    $('#diskPill').textContent = `${gib(d.free)} wolne`;
    $('#stFree').textContent = gib(d.free);
    $('#stBar').style.width = `${100 * d.used / d.total}%`;
    $('#stHours').textContent = `≈ ${d.estimated_dv_hours} h DV · próg ${gib(d.min_free)}`;

    const active = (s.jobs || []).filter(j => !TERMINAL.has(j.status));
    $('#stJobs').textContent = active.length;
    $('#stJobsSub').textContent = s.processing ? `przetwarzanie ${s.processing.tape_id}` : (active.length ? 'w toku' : 'brak');
    $('#navJobs').textContent = active.length || '';
    $('#stTapes').textContent = t.length;
    $('#navTapes').textContent = t.length || '';

    const avc = !!c.avc_enabled, m = $('#manual');
    $('#avc').hidden = !avc; $('#manualHint').hidden = avc; $('#manualSteps').hidden = avc;
    if (!avc) { m.checked = true; m.disabled = true; } else m.disabled = false;

    const cap = s.capture;
    $('#capState').textContent = cap ? stPL(cap.status) : 'BEZCZYNNY';
    $('#capLog').textContent = cap ? (cap.logs || '—')
      : (s.processing ? `W tle: przetwarzanie ${s.processing.tape_id} (${stPL(s.processing.status)})` : 'Brak aktywnego zgrywania.');
    $('#start').disabled = !!cap;

    renderJobs(s.jobs || [], s.queue || []);
    renderTapeGrid(t);
    renderLogs(s);
  } catch (e) {
    $('#camPill').textContent = 'BŁĄD API'; $('#capLog').textContent = e.message;
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

// active-nav highlight (TailAdmin menu-item state classes)
const SECTIONS = ['pulpit', 'zadania', 'kasety', 'logi', 'info'];
const scroller = document.querySelector('.overflow-y-auto');
function syncNav() {
  const y = (scroller?.scrollTop || window.scrollY || 0) + 140;
  let cur = SECTIONS[0];
  for (const id of SECTIONS) { const el = document.getElementById(id); if (el && el.offsetTop <= y) cur = id; }
  $$('.navlink').forEach(a => {
    const on = a.getAttribute('href') === '#' + cur;
    a.classList.toggle('menu-item-active', on);
    a.classList.toggle('menu-item-inactive', !on);
    const svg = a.querySelector('svg');
    svg?.classList.toggle('menu-item-icon-active', on);
    svg?.classList.toggle('menu-item-icon-inactive', !on);
  });
}
scroller?.addEventListener('scroll', syncNav);
addEventListener('scroll', syncNav);

refresh();
setInterval(refresh, 3000);
