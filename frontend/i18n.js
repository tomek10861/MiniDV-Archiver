// Tiny i18n: EN is the canonical set, PL overrides it. Language lives in
// localStorage; switching reloads the page (simpler than re-rendering every cache).
(function () {
  const EN = {
    'app.subtitle': 'IEEE 1394 · lossless',
    'nav.menu': 'Menu',
    'sec.pulpit': 'Dashboard', 'sec.zadania': 'Tasks', 'sec.kasety': 'Tapes',
    'sec.duplikaty': 'Duplicates', 'sec.timeline': 'Timeline', 'sec.logi': 'Logs', 'sec.info': 'About',
    'lang.label': 'Language',
    'theme.title': 'Theme',

    // dashboard cards
    'card.camera': 'Camera', 'card.free': 'Free space', 'card.jobs': 'Tasks running', 'card.tapes': 'Tapes',
    'card.tapes.sub': 'archived',
    'cam.pill.connecting': 'CONNECTING…', 'cam.pill.online': 'CAMERA ONLINE', 'cam.pill.offline': 'NO CAMERA',
    'cam.offline': 'OFFLINE', 'cam.noDevice': 'no AV/C device', 'cam.guid': 'guid {x}',
    'cam.mode.manual': 'manual', 'cam.mode.auto': 'auto AV/C', 'cam.side.offline': 'camera offline',
    'disk.free': '{x} free', 'disk.hours': '≈ {h} h DV · floor {min}',
    'jobs.sub.processing': 'processing {tape}', 'jobs.sub.background': '{n} in background ({what}…)',
    'jobs.sub.running': 'running', 'jobs.sub.none': 'none',
    'apiError': 'API ERROR',

    // capture panel
    'capture.title': 'New capture',
    'btn.start': 'Start', 'btn.cancel': 'Stop',
    'capture.tape.ph': 'auto: TAPE-0001',
    'capture.manual': 'Manual mode — press PLAY on the camera after starting',
    'capture.manualHint': 'AV/C control disabled — drive transport with the buttons on the camera.',
    'avc.rewind': '⏮ Rewind', 'avc.play': '▶ Play', 'avc.stop': '■ Stop',
    'capture.step1': 'Click “Start”.',
    'capture.step2': 'When the status shows <b>WAITING FOR PLAY</b>, press <b>PLAY</b> on the camera.',
    'capture.step3': 'When the footage ends press <b>STOP</b> on the camera — the capture closes itself.',
    'capture.idle': 'No capture running.',
    'capture.elapsed': 'Elapsed', 'capture.captured': 'Captured',
    'capture.background': 'Background: processing {tape} ({status})',

    // tasks
    'tasks.title': 'Background tasks',
    'jobs.empty': 'No tasks.',
    'job.stop': 'Stop',
    'job.clear': '🗑 Clear',
    'job.badge.capture': 'CAPTURE', 'job.badge.queued': 'QUEUED', 'job.badge.done': 'DONE',
    'job.badge.error': 'ERROR', 'job.badge.cancelled': 'CANCELLED', 'job.badge.processing': 'PROCESSING',
    'job.confirmStop': 'Stop task {tape}?',
    'job.confirmClear': 'Remove the record of {tape} ({status})?\nIt captured no data — nothing archived is affected.',
    'job.drop': '⚠ {n} dropped',
    'job.eta': 'ETA {t}',

    'build.mode.reprobe': 'QUALITY SCAN', 'build.mode.restore': 'RESTORE',
    'build.mode.share': 'SHARE', 'build.mode.concat': 'JOIN',
    'build.st.QUEUED': 'queued', 'build.st.RUNNING': 'processing…', 'build.st.READY': 'done', 'build.st.ERROR': 'error',

    // states
    'state.IDLE': 'IDLE', 'state.CREATED': 'CREATED',
    'state.WAITING_FOR_PLAY': '▶ WAITING FOR PLAY — press PLAY on the camera',
    'state.CAPTURING': 'CAPTURING', 'state.REWINDING': 'REWINDING',
    'state.CHECKING_STORAGE': 'CHECKING DISK', 'state.CHECKING_CAMERA': 'CHECKING CAMERA',
    'state.CAPTURED': 'CAPTURED — WAITING TO PROCESS', 'state.QUEUED': 'QUEUED',
    'state.DETECTING_SCENES': 'DETECTING SCENES', 'state.COMPRESSING': 'COMPRESSING',
    'state.VERIFYING_ARCHIVES': 'VERIFYING ARCHIVE', 'state.ENCODING_MP4': 'ENCODING MP4',
    'state.VERIFYING_MP4': 'VERIFYING MP4', 'state.ANALYZING_DV': 'ANALYSING DV',
    'state.BUILDING_TAPE_PROXY': 'BUILDING TAPE PREVIEW', 'state.PROBING_QUALITY': 'SCANNING QUALITY',
    'state.COMPLETED': 'DONE', 'state.ERROR': 'ERROR', 'state.CANCELLED': 'CANCELLED',

    // tapes
    'tapes.title': 'Tapes',
    'tapes.scent': 'Browse by tape — the way they were captured.',
    'tapes.empty': 'No archived tapes.',
    'tape.scenes': '{n} scenes',
    'loading': 'Loading…',
    'tape.rename': '✏️ Rename', 'tape.label': '🏷️ Label', 'tape.date': '📅 Recording date',
    'tape.playFull': '▶ Play whole tape', 'tape.fbFull': '⬇ FB whole tape',
    'tape.repairFull': '🧹 Restore tape', 'tape.reprobe': '🔍 Scan quality', 'tape.delete': '🗑 Delete tape',
    'tape.fullSoon': 'The whole-tape preview is built on the next archival run.',
    'tape.crumbAll': '← All tapes', 'tape.crumbScene': '· scene',
    'sel.count': 'Selected', 'sel.download': '⬇ Download as one video',
    'sel.fb': '⬇ FB (one video ~90 MB)', 'sel.repair': '🧹 Restore (from masters)',
    'sel.delete': '🗑 Delete selected', 'sel.all': 'select all', 'sel.clear': 'clear',
    'scenes.empty': 'No scenes.',
    'scene.n': 'Scene {n}',
    'scene.frames': '{n} fr.',
    'scene.recorded': 'recorded {dt}',
    'scene.seek': '⏱ in the whole tape', 'scene.preview': '▶ Preview',
    'scene.fb': '⬇ FB (~90 MB)', 'scene.repair': '🧹 Restore',
    'scene.dv': '⬇ DV .dv.zst', 'scene.mp4': '⬇ MP4', 'scene.json': '⬇ JSON',
    'flag.dropped': '⚠ {n} dropped frame(s)', 'flag.disc': '⚠ {n}× discontinuity',
    'flag.badClock': '⚠ camera clock wrong',
    'build.wait.queued': '⏳ queued…', 'build.wait.running': '⏳ processing…', 'build.fail': 'processing error',
    'fromTimeline': '‹ Timeline · {month} {year}',

    'confirm.deleteTape': 'Delete the whole tape {id} ({n} scenes)?\nEvery .dv.zst master is lost PERMANENTLY.',
    'prompt.deleteTapeName': 'To confirm deletion, type the tape name:\n{id}',
    'alert.nameMismatch': 'Cancelled — the name does not match.',
    'prompt.rename': 'New tape name (letters, digits, . _ -):',
    'prompt.label': 'Tape label / description (empty = remove):',
    'prompt.date': 'Recording date YYYY-MM-DD (empty = remove):',
    'confirm.deleteScenes': 'Delete {n} selected scenes from {tape}?\nThe .dv.zst masters of those scenes are lost PERMANENTLY.',
    'confirm.irreversible': 'Are you sure? This CANNOT be undone.',

    // duplicates
    'dup.title': 'Duplicates',
    'dup.scent': 'The same recordings from different captures of one tape — compare and keep the version with fewer errors.',
    'dup.summary': '{g} likely-duplicate groups · {n} scenes fingerprinted{unp}',
    'dup.unprobed': ' · {n} not fingerprinted',
    'dup.scanAll': '🔍 Scan all tapes', 'dup.refresh': '↻ Refresh',
    'dup.groupCount': '{n} versions of the same recording',
    'dup.errors': 'errors: {score}', 'dup.errBreakdown': 'decode {d}, dropped {dr}, disc. {di}',
    'dup.best': ' · best', 'dup.keep': 'Keep this, delete the rest', 'dup.preview': 'preview',
    'dup.none': 'No duplicates found.',
    'dup.noneHint': ' You have scenes without a fingerprint — click “Scan all tapes”, then refresh.',
    'dup.confirmKeep': 'Keep the version from {keep} (errors: {keepScore}) and delete {n}?\n{list}',
    'dup.confirmItem': '• {tape} / scene {idx} — errors: {score}',
    'dup.frames': '{n} fr.',

    // timeline
    'timeline.title': 'Timeline',
    'timeline.scent': 'Browse by recording date — scenes from different tapes together.',
    'timeline.refresh': '↻ Refresh',
    'tl.crumbAll': 'All years',
    'tl.noDated': 'No dated recordings. Fill in tape dates or wait for the index to refresh.',
    'tl.noDate': 'No date', 'tl.noDateSub': '{n} · look in Tapes',
    'tl.noScenes': 'No scenes in this year.',
    'tl.scene': 'scene {n}',
    'tl.undatedNote': '+ {n} without a recognised date (shown under “Tapes”).',
    'tl.openInTapes': 'open in Tapes', 'tl.build': '🎬 Build one video from selected',
    'tl.buildHint': 'Pick scenes from different tapes to join them into a single video, in this order.',

    // logs
    'logs.title': 'Logs', 'logs.follow': 'auto-scroll', 'logs.none': 'no tasks',
    'log.opt.capture': '▶ capture {tape}',

    // about
    'about.title': 'About',
    'about.intro': '<b class="text-gray-800 dark:text-white/90">MiniDV Archiver</b> — lossless capture and archival of MiniDV / DV tapes over FireWire (IEEE&nbsp;1394), with a panel for reviewing what you captured. The master is always the raw DV stream, <span class="font-mono">zstd</span>-compressed and verified byte-for-byte.',
    'about.contact': 'Author · contact',
    'about.license': 'License',
    'about.licenseText': '<b class="text-gray-800 dark:text-white/90">MIT</b> — the code is free: use, modify and redistribute it, commercially too, keeping the copyright notice. Full text in the repository <span class="font-mono">LICENSE</span> file.',
    'about.credits': 'Dashboard based on <a class="text-brand-500 hover:underline" href="https://github.com/TailAdmin/tailadmin-free-tailwind-dashboard-template" target="_blank" rel="noopener">TailAdmin</a> (MIT), Alpine.js (MIT), Tailwind CSS (MIT).',
    'about.tools': 'External runtime tools: dvgrab, ffmpeg, zstd, linux-firewire-utils.',

    'mon.short': ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
    'mon.long': ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'],
    'mon.gen': ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'],
  };

  const PL = {
    'nav.menu': 'Menu',
    'sec.pulpit': 'Pulpit', 'sec.zadania': 'Zadania', 'sec.kasety': 'Kasety',
    'sec.duplikaty': 'Duplikaty', 'sec.timeline': 'Oś czasu', 'sec.logi': 'Logi', 'sec.info': 'Informacje',
    'lang.label': 'Język',
    'theme.title': 'Motyw',

    'card.camera': 'Kamera', 'card.free': 'Wolne miejsce', 'card.jobs': 'Zadania w toku', 'card.tapes': 'Kasety',
    'card.tapes.sub': 'zarchiwizowane',
    'cam.pill.connecting': 'ŁĄCZENIE…', 'cam.pill.online': 'KAMERA ONLINE', 'cam.pill.offline': 'BRAK KAMERY',
    'cam.offline': 'OFFLINE', 'cam.noDevice': 'brak urządzenia AV/C', 'cam.guid': 'guid {x}',
    'cam.mode.manual': 'ręczny', 'cam.mode.auto': 'auto AV/C', 'cam.side.offline': 'kamera offline',
    'disk.free': '{x} wolne', 'disk.hours': '≈ {h} h DV · próg {min}',
    'jobs.sub.processing': 'przetwarzanie {tape}', 'jobs.sub.background': '{n} w tle ({what}…)',
    'jobs.sub.running': 'w toku', 'jobs.sub.none': 'brak',
    'apiError': 'BŁĄD API',

    'capture.title': 'Nowe zgrywanie',
    'btn.start': 'Rozpocznij', 'btn.cancel': 'Przerwij',
    'capture.tape.ph': 'auto: TAPE-0001',
    'capture.manual': 'Tryb ręczny — po starcie naciśnij PLAY na kamerze',
    'capture.manualHint': 'Sterowanie AV/C wyłączone — transport obsługujesz przyciskami na kamerze.',
    'avc.rewind': '⏮ Przewiń', 'avc.play': '▶ Odtwórz', 'avc.stop': '■ Stop',
    'capture.step1': 'Kliknij „Rozpocznij”.',
    'capture.step2': 'Gdy status pokaże <b>CZEKAM NA PLAY</b>, naciśnij <b>PLAY</b> na kamerze.',
    'capture.step3': 'Po materiale naciśnij <b>STOP</b> na kamerze — zapis zamknie się sam.',
    'capture.idle': 'Brak aktywnego zgrywania.',
    'capture.elapsed': 'Nagrane', 'capture.captured': 'Skopiowane',
    'capture.background': 'W tle: przetwarzanie {tape} ({status})',

    'tasks.title': 'Zadania w tle',
    'jobs.empty': 'Brak zadań.',
    'job.stop': 'Przerwij',
    'job.clear': '🗑 Usuń wpis',
    'job.badge.capture': 'ZGRYWANIE', 'job.badge.queued': 'W KOLEJCE', 'job.badge.done': 'GOTOWE',
    'job.badge.error': 'BŁĄD', 'job.badge.cancelled': 'PRZERWANE', 'job.badge.processing': 'PRZETWARZANIE',
    'job.confirmStop': 'Przerwać zadanie {tape}?',
    'job.confirmClear': 'Usunąć wpis zadania {tape} ({status})?\nNie złapało żadnych danych — nic zarchiwizowanego to nie dotyczy.',
    'job.drop': '⚠ {n} zgub.',
    'job.eta': 'zostało {t}',

    'build.mode.reprobe': 'SONDA JAKOŚCI', 'build.mode.restore': 'NAPRAWA',
    'build.mode.share': 'UDOSTĘPNIANIE', 'build.mode.concat': 'SKLEJANIE',
    'build.st.QUEUED': 'w kolejce', 'build.st.RUNNING': 'przetwarzanie…', 'build.st.READY': 'gotowe', 'build.st.ERROR': 'błąd',

    'state.IDLE': 'BEZCZYNNY', 'state.CREATED': 'UTWORZONE',
    'state.WAITING_FOR_PLAY': '▶ CZEKAM NA PLAY — naciśnij PLAY na kamerze',
    'state.CAPTURING': 'NAGRYWANIE', 'state.REWINDING': 'PRZEWIJANIE',
    'state.CHECKING_STORAGE': 'SPRAWDZANIE DYSKU', 'state.CHECKING_CAMERA': 'SPRAWDZANIE KAMERY',
    'state.CAPTURED': 'ZGRANE — CZEKA NA PRZETWARZANIE', 'state.QUEUED': 'W KOLEJCE',
    'state.DETECTING_SCENES': 'WYKRYWANIE SCEN', 'state.COMPRESSING': 'KOMPRESJA',
    'state.VERIFYING_ARCHIVES': 'WERYFIKACJA ARCHIWUM', 'state.ENCODING_MP4': 'KODOWANIE MP4',
    'state.VERIFYING_MP4': 'WERYFIKACJA MP4', 'state.ANALYZING_DV': 'ANALIZA DV',
    'state.BUILDING_TAPE_PROXY': 'SKLEJANIE PODGLĄDU TAŚMY', 'state.PROBING_QUALITY': 'SONDA JAKOŚCI',
    'state.COMPLETED': 'GOTOWE', 'state.ERROR': 'BŁĄD', 'state.CANCELLED': 'PRZERWANE',

    'tapes.title': 'Kasety',
    'tapes.scent': 'Przeglądaj po taśmach — tak, jak zostały zgrane.',
    'tapes.empty': 'Brak zarchiwizowanych kaset.',
    'tape.scenes': '{n} scen',
    'loading': 'Wczytywanie…',
    'tape.rename': '✏️ Zmień nazwę', 'tape.label': '🏷️ Etykieta', 'tape.date': '📅 Data nagrania',
    'tape.playFull': '▶ Odtwórz całą taśmę', 'tape.fbFull': '⬇ FB cała taśma',
    'tape.repairFull': '🧹 Napraw taśmę', 'tape.reprobe': '🔍 Sonduj jakość', 'tape.delete': '🗑 Usuń kasetę',
    'tape.fullSoon': 'Podgląd całej taśmy powstanie przy następnej archiwizacji.',
    'tape.crumbAll': '← Wszystkie kasety', 'tape.crumbScene': '· scena',
    'sel.count': 'Zaznaczono', 'sel.download': '⬇ Pobierz jako jeden film',
    'sel.fb': '⬇ FB (jeden film ~90 MB)', 'sel.repair': '🧹 Napraw (z masterów)',
    'sel.delete': '🗑 Usuń zaznaczone', 'sel.all': 'zaznacz wszystkie', 'sel.clear': 'wyczyść',
    'scenes.empty': 'Brak scen.',
    'scene.n': 'Scena {n}',
    'scene.frames': '{n} kl.',
    'scene.recorded': 'nagrano {dt}',
    'scene.seek': '⏱ w całej taśmie', 'scene.preview': '▶ Podgląd',
    'scene.fb': '⬇ FB (~90 MB)', 'scene.repair': '🧹 Napraw',
    'scene.dv': '⬇ DV .dv.zst', 'scene.mp4': '⬇ MP4', 'scene.json': '⬇ JSON',
    'flag.dropped': '⚠ {n} zgub. klatka', 'flag.disc': '⚠ {n}× nieciągłość',
    'flag.badClock': '⚠ zegar kamery błędny',
    'build.wait.queued': '⏳ w kolejce…', 'build.wait.running': '⏳ przetwarzanie…', 'build.fail': 'błąd przetwarzania',
    'fromTimeline': '‹ Oś czasu · {month} {year}',

    'confirm.deleteTape': 'Usunąć całą kasetę {id} ({n} scen)?\nWszystkie mastery .dv.zst przepadną BEZPOWROTNIE.',
    'prompt.deleteTapeName': 'Aby potwierdzić skasowanie, wpisz nazwę kasety:\n{id}',
    'alert.nameMismatch': 'Anulowano — nazwa nie zgadza się.',
    'prompt.rename': 'Nowa nazwa kasety (litery, cyfry, . _ -):',
    'prompt.label': 'Etykieta / opis kasety (puste = usuń):',
    'prompt.date': 'Data nagrania RRRR-MM-DD (puste = usuń):',
    'confirm.deleteScenes': 'Usunąć {n} zaznaczonych scen z {tape}?\nMastery .dv.zst tych scen przepadną BEZPOWROTNIE.',
    'confirm.irreversible': 'Na pewno? Tej operacji NIE DA SIĘ cofnąć.',

    'dup.title': 'Duplikaty',
    'dup.scent': 'Te same nagrania z różnych zgrań tej samej kasety — porównaj i zostaw wersję z mniejszą liczbą błędów.',
    'dup.summary': '{g} grup możliwych duplikatów · {n} scen z odciskiem{unp}',
    'dup.unprobed': ' · {n} bez odcisku',
    'dup.scanAll': '🔍 Sonduj wszystkie taśmy', 'dup.refresh': '↻ Odśwież',
    'dup.groupCount': '{n} wersje tego samego nagrania',
    'dup.errors': 'błędy: {score}', 'dup.errBreakdown': 'dekod. {d}, zgub. {dr}, nieciąg. {di}',
    'dup.best': ' · najlepsza', 'dup.keep': 'Zostaw tę, usuń resztę', 'dup.preview': 'podgląd',
    'dup.none': 'Nie znaleziono duplikatów.',
    'dup.noneHint': ' Masz sceny bez odcisku — kliknij „Sonduj wszystkie taśmy", potem odśwież.',
    'dup.confirmKeep': 'Zostawić wersję z {keep} (błędy: {keepScore}) i usunąć {n}?\n{list}',
    'dup.confirmItem': '• {tape} / scena {idx} — błędy: {score}',
    'dup.frames': '{n} kl.',

    'timeline.title': 'Oś czasu',
    'timeline.scent': 'Przeglądaj po dacie nagrania — sceny z różnych taśm razem.',
    'timeline.refresh': '↻ Odśwież',
    'tl.crumbAll': 'Wszystkie lata',
    'tl.noDated': 'Brak datowanych nagrań. Uzupełnij daty kaset albo poczekaj na odświeżenie indeksu.',
    'tl.noDate': 'Bez daty', 'tl.noDateSub': '{n} · szukaj w Kasetach',
    'tl.noScenes': 'Brak scen w tym roku.',
    'tl.scene': 'scena {n}',
    'tl.undatedNote': '+ {n} bez rozpoznanej daty (widoczne w „Kasety”).',
    'tl.openInTapes': 'otwórz w Kasetach', 'tl.build': '🎬 Zbuduj jedno nagranie z zaznaczonych',
    'tl.buildHint': 'Zaznacz sceny z różnych kaset, żeby złączyć je w jedno nagranie, w tej kolejności.',

    'logs.title': 'Logi', 'logs.follow': 'auto-scroll', 'logs.none': 'brak zadań',
    'log.opt.capture': '▶ zgrywanie {tape}',

    'about.title': 'Informacje',
    'about.intro': '<b class="text-gray-800 dark:text-white/90">MiniDV Archiver</b> — bezstratne zgrywanie i archiwizacja kaset MiniDV / DV przez FireWire (IEEE&nbsp;1394), z panelem do przeglądania nagrań. Masterem jest zawsze surowy strumień DV skompresowany <span class="font-mono">zstd</span> i zweryfikowany bajt‑po‑bajcie.',
    'about.contact': 'Autor · kontakt',
    'about.license': 'Licencja',
    'about.licenseText': '<b class="text-gray-800 dark:text-white/90">MIT</b> — kod jest wolny: można go używać, modyfikować i rozpowszechniać, także komercyjnie, zachowując notę o prawach autorskich. Pełny tekst w pliku <span class="font-mono">LICENSE</span> repozytorium.',
    'about.credits': 'Dashboard oparty na <a class="text-brand-500 hover:underline" href="https://github.com/TailAdmin/tailadmin-free-tailwind-dashboard-template" target="_blank" rel="noopener">TailAdmin</a> (MIT), Alpine.js (MIT), Tailwind CSS (MIT).',
    'about.tools': 'Narzędzia zewnętrzne w runtime: dvgrab, ffmpeg, zstd, linux‑firewire‑utils.',

    'mon.short': ['sty', 'lut', 'mar', 'kwi', 'maj', 'cze', 'lip', 'sie', 'wrz', 'paź', 'lis', 'gru'],
    'mon.long': ['Styczeń', 'Luty', 'Marzec', 'Kwiecień', 'Maj', 'Czerwiec', 'Lipiec', 'Sierpień', 'Wrzesień', 'Październik', 'Listopad', 'Grudzień'],
    'mon.gen': ['stycznia', 'lutego', 'marca', 'kwietnia', 'maja', 'czerwca', 'lipca', 'sierpnia', 'września', 'października', 'listopada', 'grudnia'],
  };

  const SUPPORTED = { en: 'English', pl: 'Polski' };
  let lang = localStorage.getItem('lang');
  if (!SUPPORTED[lang]) lang = (navigator.language || 'en').toLowerCase().startsWith('pl') ? 'pl' : 'en';

  const DICT = lang === 'pl' ? PL : EN;
  function t(key, vars) {
    let s = DICT[key];
    if (s === undefined) s = EN[key];
    if (s === undefined) return key;
    if (Array.isArray(s)) return s;
    if (vars) for (const k in vars) s = s.split('{' + k + '}').join(vars[k]);
    return s;
  }

  function applyI18n(root) {
    (root || document).querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
    (root || document).querySelectorAll('[data-i18n-html]').forEach(el => { el.innerHTML = t(el.dataset.i18nHtml); });
    (root || document).querySelectorAll('[data-i18n-ph]').forEach(el => { el.placeholder = t(el.dataset.i18nPh); });
    (root || document).querySelectorAll('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
    document.documentElement.lang = lang;
  }

  function setLang(next) {
    if (!SUPPORTED[next] || next === lang) return;
    localStorage.setItem('lang', next);
    location.reload();
  }

  window.i18n = { t, lang, applyI18n, setLang, supported: SUPPORTED };
  window.L = t;                      // short alias for use in app.js (avoids the `t` param collisions)
  window.MON_SHORT = t('mon.short');
  window.MON_LONG = t('mon.long');
  window.MON_GEN = t('mon.gen');
  document.addEventListener('DOMContentLoaded', () => applyI18n());
})();
