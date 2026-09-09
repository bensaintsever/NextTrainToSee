'use strict';
/*
 * NextTrainToSee — logique de la PWA.
 *
 * Rien n'est inféré côté client : le serveur donne tous les libellés
 * (direction_label, look, route_label…). Ce fichier se contente de les
 * afficher, de tenir un compte à rebours à la seconde, et d'appliquer la
 * procédure d'observation § 6 du document de référence à la lettre.
 */

// ---------------------------------------------------------------------------
// Constantes et état global
// ---------------------------------------------------------------------------

const USE_MOCK = new URLSearchParams(location.search).get('mock') === '1';

const POLL_MS = 30_000;        // /api/next toutes les 30 s
const TICK_MS = 1_000;         // horloge locale à la seconde
const IMMINENT_LEAD_MS = 30_000;      // état imminent dès announce_at - 30 s
const CONFIRM_DELAY_MS = 90_000;      // carte après when + uncertainty + 90 s
const CONFIRM_TTL_MS = 10 * 60_000;   // la carte s'efface après 10 min sans réponse

// Fenêtre nocturne : 18 h – 6 h, heure locale de l'appareil (retour
// utilisateur). Aucune API ne la fournit — c'est un repère visuel, pas une
// donnée du contrat — donc simple à ajuster ici si besoin.
const NIGHT_START_HOUR = 18;
const NIGHT_END_HOUR = 6;
const THEME_COLOR_DAY = '#2f5878';    // bleu du ciel diurne
const THEME_COLOR_NIGHT = '#141a33';  // bleu nuit de la variante nocturne

// Phrases fixes liées à `look`, tel que fourni par le serveur (§ 3 et § 5).
// Raccourcies (retour utilisateur : « trop serré ») — sans « derrière toi ».
const LOOK_PHRASES = {
  tunnel: 'sort du tunnel',
  sud: 'arrive du sud',
};

const CATEGORY_LABELS = {
  ter: 'TER',
  // Le serveur réel envoie « grandes-lignes » (trait d'union, cf.
  // config/toulouse-guilhemery.toml) ; l'alias souligné est toléré au cas où.
  'grandes-lignes': 'Intercités/TGV',
  grandes_lignes: 'Intercités/TGV',
  intercites: 'Intercités',
  tgv: 'TGV',
  tgv_inoui: 'TGV inOui',
  ic: 'IC',
};

// Dernière liste connue de passages (0, 1 ou 2 selon /api/next?limit=2).
let passages = [];
// Le passage actuellement affiché en grand (zone haute), conservé même après
// sa disparition de la liste pour pouvoir déclencher la carte de confirmation.
let trackedTop = null;
// trip_id déjà « réglés » (bouton pressé ou carte répondue) : on ne les
// re-demande jamais.
const acknowledged = new Set();
// trip_id pour lesquels une carte de confirmation a déjà été programmée,
// pour ne jamais programmer deux fois le même minuteur.
const scheduled = new Set();

let offlineTimer = null;
let confirmAutoHideTimer = null;
let currentConfirmPassage = null;
let pendingManualPassage = null;

let histoData = null;
let histoTab = 'weekday';
let histoLastTab = null; // dernier onglet rendu : détermine le sens du glissement

// Décalage appliqué aux horaires de démonstration de assets/mock.json, fixé
// une fois puis reconduit pour que la démo se déroule en temps réel (compte
// à rebours, état imminent, carte de confirmation) plutôt que de rester figée
// sur « dans 3 min ». Voir getMockData().
let mockShift = null;
let mockRaw = null;

// ---------------------------------------------------------------------------
// Utilitaires de date/heure
// ---------------------------------------------------------------------------

function pad(n, len = 2) { return String(n).padStart(len, '0'); }

/** Formate une Date en ISO 8601 avec le décalage horaire local (requis par
 *  le contrat § 4 pour tout ce que le client envoie). */
function toIsoLocal(d) {
  const tzMin = -d.getTimezoneOffset();
  const sign = tzMin >= 0 ? '+' : '-';
  const abs = Math.abs(tzMin);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` +
    `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

function fmtHM(iso) {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtHMS(iso) {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Durée en millisecondes → « m:ss » ou « h:mm:ss » si besoin. */
function fmtDur(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${pad(m)}:${pad(s)}`;
  return `${m}:${pad(s)}`;
}

function fmtNumberFr(n, maxDecimals = 1) {
  return n.toLocaleString('fr-FR', { maximumFractionDigits: maxDecimals, minimumFractionDigits: 0 });
}

// ---------------------------------------------------------------------------
// Accès réseau
// ---------------------------------------------------------------------------

async function fetchJson(url, options) {
  const res = await fetch(url, Object.assign({ cache: 'no-store' }, options));
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** Recalcule les horaires de la démo mock pour qu'ils restent proches de
 *  « maintenant » et rejouent tout le cycle (annonce → imminent → passage →
 *  carte de confirmation) au lieu de rester figés sur les valeurs figées
 *  dans assets/mock.json. */
function getMockData(raw) {
  const now = Date.now();
  const rawAnchor = Date.parse(raw.generated_at);

  const shiftIso = (iso, shift) => toIsoLocal(new Date(Date.parse(iso) + shift));
  const applyShift = (shift) => ({
    ...raw,
    generated_at: toIsoLocal(new Date(now)),
    passages: raw.passages.map((p) => ({
      ...p,
      when: shiftIso(p.when, shift),
      announce_at: shiftIso(p.announce_at, shift),
    })),
  });

  if (mockShift === null) mockShift = now - rawAnchor;
  let data = applyShift(mockShift);

  // Une fois le dernier passage bien écoulé, on relance une nouvelle boucle
  // de démonstration à partir de maintenant plutôt que de rester bloqué sur
  // des trains passés.
  const last = data.passages[data.passages.length - 1];
  const lastEnd = Date.parse(last.when) + last.uncertainty_s * 1000;
  if (now > lastEnd + 60_000) {
    mockShift = now - rawAnchor;
    data = applyShift(mockShift);
  }
  return data;
}

async function fetchNext() {
  try {
    const data = await fetchJson('/api/next?limit=2');
    setOffline(false);
    handleNextData(data);
  } catch (err) {
    if (USE_MOCK) {
      try {
        if (!mockRaw) mockRaw = await fetchJson('assets/mock.json');
        setOffline(false);
        handleNextData(getMockData(mockRaw));
        return;
      } catch (err2) {
        // Même le mock est indisponible : on retombe sur l'état hors-ligne.
      }
    }
    setOffline(true);
  }
}

async function postObserve(body) {
  return fetchJson('/api/observe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Bandeau d'état dégradé (API muette / horaire théorique)
// ---------------------------------------------------------------------------

const bannerEl = document.getElementById('banner');
let isOffline = false;

function setOffline(v) {
  isOffline = v;
  renderBanner();
  if (v) renderOfflinePlaceholders();
}

function renderBanner() {
  if (isOffline) {
    bannerEl.hidden = false;
    bannerEl.classList.remove('theoretical');
    bannerEl.textContent = '📡 Connexion au serveur perdue';
  } else if (lastRoot && lastRoot.realtime === false) {
    bannerEl.hidden = false;
    bannerEl.classList.add('theoretical');
    bannerEl.textContent = '⏱ Horaire théorique — temps réel indisponible';
  } else {
    bannerEl.hidden = true;
  }
}

function renderOfflinePlaceholders() {
  document.getElementById('big-time').textContent = '—';
  document.getElementById('uncertainty').innerHTML = '&nbsp;';
  document.getElementById('countdown').innerHTML = '&nbsp;';
  document.getElementById('direction').innerHTML = '&nbsp;';
  document.getElementById('train-info').innerHTML = '&nbsp;';
  document.getElementById('badge-rt').textContent = '—';
  document.getElementById('badge-rt').classList.remove('theoretical');
  document.getElementById('badge-delay').hidden = true;
  document.getElementById('sky-panel').classList.remove('imminent');
  document.getElementById('next-line').textContent = '—';
}

// ---------------------------------------------------------------------------
// Traitement de /api/next
// ---------------------------------------------------------------------------

let lastRoot = null;

function handleNextData(data) {
  lastRoot = data;
  const list = data.passages || [];

  // Le passage affiché en tête change : celui qu'on affichait avant vient de
  // s'écouler (sorti de la fenêtre de 12 h) → on programme, s'il le faut, la
  // carte de confirmation différée pour lui.
  const newTop = list[0] || null;
  if (trackedTop && (!newTop || newTop.trip_id !== trackedTop.trip_id)) {
    scheduleConfirmation(trackedTop);
  }
  if (newTop) trackedTop = newTop;

  passages = list;
  renderBanner();
  renderSky();
  renderTrack();
}

// ---------------------------------------------------------------------------
// Zone haute (le ciel)
// ---------------------------------------------------------------------------

function describePhase(p, now) {
  const annAt = Date.parse(p.announce_at);
  const whenAt = Date.parse(p.when);
  const endAt = whenAt + p.uncertainty_s * 1000;
  const imminent = now >= annAt - IMMINENT_LEAD_MS && now <= endAt;

  let sub;
  // Simplifié (retour utilisateur) : plus de « dès HH:MM:SS », redondant
  // avec l'heure déjà affichée en grand juste au-dessus. Casse cohérente
  // (majuscule initiale) sur les quatre états, comme le reste de l'app.
  if (now < annAt) sub = `Guette dans ${fmtDur(annAt - now)}`;
  else if (now < whenAt) sub = `À l'affût · passage dans ${fmtDur(whenAt - now)}`;
  else if (now <= endAt) sub = '👀 Regarde, il devrait passer !';
  else sub = 'Passage attendu…';

  return { sub, imminent };
}

function renderSky() {
  const badgeRt = document.getElementById('badge-rt');
  const badgeDelay = document.getElementById('badge-delay');
  const bigTime = document.getElementById('big-time');
  const uncertainty = document.getElementById('uncertainty');
  const countdown = document.getElementById('countdown');
  const direction = document.getElementById('direction');
  const trainInfo = document.getElementById('train-info');
  const skyPanel = document.getElementById('sky-panel');

  const p = passages[0];

  if (!p) {
    // Aucun passage dans l'horizon de 12 h : message de nuit, discret.
    badgeRt.textContent = '';
    badgeRt.hidden = true;
    badgeDelay.hidden = true;
    bigTime.textContent = '—';
    uncertainty.innerHTML = '&nbsp;';
    countdown.textContent = '🌙 Aucun train dans les 12 prochaines heures';
    direction.innerHTML = '&nbsp;';
    trainInfo.innerHTML = '&nbsp;';
    skyPanel.classList.remove('imminent');
    return;
  }

  badgeRt.hidden = false;
  if (p.realtime) {
    badgeRt.textContent = 'temps réel';
    badgeRt.classList.remove('theoretical');
  } else {
    badgeRt.textContent = 'horaire théorique';
    badgeRt.classList.add('theoretical');
  }

  if (p.delay_s === null || p.delay_s === undefined) {
    badgeDelay.hidden = true;
  } else if (p.delay_s === 0) {
    badgeDelay.hidden = false;
    badgeDelay.textContent = 'à l\'heure';
    badgeDelay.classList.add('on-time');
  } else {
    badgeDelay.hidden = false;
    badgeDelay.classList.remove('on-time');
    const min = Math.round(p.delay_s / 60);
    badgeDelay.textContent = `${min > 0 ? '+' : ''}${min} min`;
  }

  bigTime.textContent = fmtHMS(p.when);
  uncertainty.textContent = `± ${Math.round(p.uncertainty_s)} s`;

  const now = Date.now();
  const phase = describePhase(p, now);
  countdown.textContent = phase.sub;
  skyPanel.classList.toggle('imminent', phase.imminent);

  const lookPhrase = LOOK_PHRASES[p.look] || '';
  direction.textContent = `${p.direction_label} · ${lookPhrase}`;

  const cat = CATEGORY_LABELS[p.category_id] || (p.category_id || '').toUpperCase();
  const bits = [cat, p.headsign].filter(Boolean).join(' ');
  trainInfo.textContent = [bits, p.route_label || p.branch_label].filter(Boolean).join(' · ');
}

// ---------------------------------------------------------------------------
// Zone basse (la voie)
// ---------------------------------------------------------------------------

function renderTrack() {
  const nextLine = document.getElementById('next-line');
  if (passages.length >= 2) {
    const n = passages[1];
    nextLine.textContent = `puis ${fmtHM(n.when)} · ${n.direction_label}`;
  } else if (passages.length === 1) {
    nextLine.textContent = 'Aucun autre passage prévu pour l\'instant';
  } else {
    nextLine.textContent = '—';
  }
}

// ---------------------------------------------------------------------------
// Bascule jour / nuit — même scène, variante nocturne de l'illustration
// ---------------------------------------------------------------------------

let isNight = null; // état inconnu au démarrage : force la première application

function isNightNow() {
  const hour = new Date().getHours();
  // Fenêtre à cheval sur minuit (18 h → 6 h le lendemain) : une comparaison
  // simple ne suffit pas, l'un des deux bornes doit s'inverser.
  return NIGHT_START_HOUR > NIGHT_END_HOUR
    ? hour >= NIGHT_START_HOUR || hour < NIGHT_END_HOUR
    : hour >= NIGHT_START_HOUR && hour < NIGHT_END_HOUR;
}

/** Applique (ou retire) le mode nuit si l'état a changé depuis le dernier
 *  appel. Rappelée à chaque tick (§ ci-dessous) : peu coûteux quand rien ne
 *  change, et l'app bascule toute seule si elle reste ouverte à travers
 *  18 h ou 6 h sans qu'un rechargement soit nécessaire. */
function applyDayNight() {
  const night = isNightNow();
  if (night === isNight) return;
  isNight = night;
  document.body.classList.toggle('night', night);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', night ? THEME_COLOR_NIGHT : THEME_COLOR_DAY);
}

// ---------------------------------------------------------------------------
// Horloge locale à la seconde : ne rafraîchit que l'affichage, pas les données
// ---------------------------------------------------------------------------

function tick() {
  applyDayNight();
  if (!isOffline) renderSky();
}

// ---------------------------------------------------------------------------
// Observation — § 6, à la lettre
// ---------------------------------------------------------------------------

const toastEl = document.getElementById('toast');
const toastTextEl = document.getElementById('toast-text');
const toastActionEl = document.getElementById('toast-action');
let toastTimer = null;

// Une observation qui porte une action reste affichée plus longtemps : le
// temps de lire, comprendre qu'on peut annuler, et le faire — 3,2 s suffit à
// un message qui ne demande qu'à être lu, pas à celui qui appelle à agir.
const TOAST_DEFAULT_MS = 3200;
const TOAST_ACTION_MS = 6000;

/** @param {string} text
 *  @param {{label: string, onClick: () => void} | null} [action] */
function showToast(text, action = null) {
  clearTimeout(toastTimer);
  toastTextEl.textContent = text;
  toastEl.classList.toggle('with-action', Boolean(action));

  if (action) {
    toastActionEl.hidden = false;
    toastActionEl.disabled = false;
    toastActionEl.textContent = action.label;
    toastActionEl.onclick = () => {
      // Synchrone, avant tout await : un second appui pendant l'annulation en
      // cours ne doit pas déclencher une seconde suppression.
      toastActionEl.disabled = true;
      action.onClick();
    };
  } else {
    toastActionEl.hidden = true;
    toastActionEl.onclick = null;
  }

  toastEl.hidden = false;
  requestAnimationFrame(() => toastEl.classList.add('show'));
  toastTimer = setTimeout(() => {
    toastEl.classList.remove('show');
    setTimeout(() => { toastEl.hidden = true; }, 300);
  }, action ? TOAST_ACTION_MS : TOAST_DEFAULT_MS);
}

/** Annule une observation envoyée par erreur (§ 6) : la seule protection
 *  contre un appui accidentel sur « Il passe ! », qu'aucun serveur ne peut
 *  distinguer d'une vraie observation une fois reçue. */
async function undoObservation(id) {
  try {
    const res = await fetchJson(`/api/observe/${id}`, { method: 'DELETE' });
    showToast(res.deleted ? '↩ Passage annulé' : '↩ Déjà annulé');
  } catch (err) {
    showToast('📡 Hors-ligne : annulation impossible');
  }
}

// 1. Le bouton « Il passe ! » est l'instrument principal : appui → POST
//    immédiat, seen=true, observed_at=maintenant, precision_s=3.
async function onSeenClick() {
  const observedAt = new Date();
  const body = { seen: true, observed_at: toIsoLocal(observedAt), precision_s: 3, source: 'app' };

  // Un appui manuel règle d'office le passage actuellement suivi : plus
  // question de le redemander via la carte différée.
  if (trackedTop) acknowledged.add(trackedTop.trip_id);
  hideConfirmCard();

  try {
    const res = await postObserve(body);
    const undo = res.id != null ? { label: 'Annuler', onClick: () => undoObservation(res.id) } : null;
    if (res.ambiguous) {
      showToast('⚠️ Passage ambigu : non enregistré', undo);
    } else if (res.recorded && res.bound_to) {
      showToast(`✓ Passage de ${fmtHM(res.bound_to.when)} enregistré`, undo);
    } else {
      showToast('✓ Passage enregistré', undo);
    }
  } catch (err) {
    showToast('📡 Hors-ligne : passage non envoyé');
  }
}

// 2. Après when + uncertainty_s + 90 s sans appui : carte discrète.
function scheduleConfirmation(p) {
  if (!p || acknowledged.has(p.trip_id) || scheduled.has(p.trip_id)) return;
  scheduled.add(p.trip_id);

  const whenAt = Date.parse(p.when);
  const endAt = whenAt + p.uncertainty_s * 1000;
  const showAt = endAt + CONFIRM_DELAY_MS;
  const expireAt = showAt + CONFIRM_TTL_MS;

  const now = Date.now();
  // Trop tard : la fenêtre de 10 minutes est déjà entièrement passée.
  // Ignorer la carte = aucune donnée, jamais une invention.
  if (now >= expireAt) return;

  setTimeout(() => showConfirmCard(p, expireAt), Math.max(0, showAt - now));
}

const confirmCardEl = document.getElementById('confirm-card');
const confirmTextEl = document.getElementById('confirm-text');

function showConfirmCard(p, expireAt) {
  if (acknowledged.has(p.trip_id)) return;
  currentConfirmPassage = p;
  confirmTextEl.textContent = `Le train de ${fmtHM(p.when)} est-il passé ?`;
  confirmCardEl.hidden = false;
  requestAnimationFrame(() => confirmCardEl.classList.add('show'));

  clearTimeout(confirmAutoHideTimer);
  const remain = Math.max(0, expireAt - Date.now());
  confirmAutoHideTimer = setTimeout(() => {
    // Ignorée pendant 10 minutes : disparaît sans rien envoyer.
    hideConfirmCard();
  }, remain);
}

function hideConfirmCard() {
  clearTimeout(confirmAutoHideTimer);
  confirmCardEl.classList.remove('show');
  setTimeout(() => { confirmCardEl.hidden = true; }, 250);
}

async function onConfirmYes() {
  const p = currentConfirmPassage;
  if (!p) return;
  acknowledged.add(p.trip_id);
  hideConfirmCard();
  try {
    const res = await postObserve({
      seen: true, observed_at: p.when, precision_s: 60, source: 'app',
    });
    const undo = res.id != null ? { label: 'Annuler', onClick: () => undoObservation(res.id) } : null;
    showToast(res.recorded ? '✓ Merci, c\'est noté' : '✓ Réponse envoyée', undo);
  } catch (err) {
    showToast('📡 Hors-ligne : réponse non envoyée');
  }
}

function onConfirmNo() {
  const p = currentConfirmPassage;
  if (!p) return;
  pendingManualPassage = p;
  hideConfirmCard();
  prefillManualTime();
  openSheet(manualSheetEl, manualBackdropEl);
}

/** Pré-remplit le champ heure avec l'heure courante au moment de l'OUVERTURE
 *  de la sheet (pas au chargement de la page) : c'est la meilleure estimation
 *  par défaut quand on répond « Non », l'utilisateur n'a plus qu'à l'ajuster. */
function prefillManualTime() {
  const now = new Date();
  manualTimeInput.value = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

// ---------------------------------------------------------------------------
// Bottom sheet : précision manuelle (heure réelle ou « non passé »)
// ---------------------------------------------------------------------------

const manualSheetEl = document.getElementById('manual-sheet');
const manualBackdropEl = document.getElementById('manual-backdrop');
const manualTimeInput = document.getElementById('manual-time');

async function onManualSubmit() {
  const p = pendingManualPassage;
  if (!p || !manualTimeInput.value) return;
  const [h, m, s] = manualTimeInput.value.split(':').map(Number);
  const ref = new Date(p.when);
  const observed = new Date(ref.getFullYear(), ref.getMonth(), ref.getDate(), h, m, s || 0, 0);

  acknowledged.add(p.trip_id);
  closeSheet(manualSheetEl, manualBackdropEl);
  try {
    const res = await postObserve({
      seen: true, observed_at: toIsoLocal(observed), precision_s: 30, source: 'app',
    });
    const undo = res.id != null ? { label: 'Annuler', onClick: () => undoObservation(res.id) } : null;
    showToast(res.recorded ? '✓ Heure enregistrée' : '✓ Réponse envoyée', undo);
  } catch (err) {
    showToast('📡 Hors-ligne : réponse non envoyée');
  }
}

async function onManualNotPassed() {
  const p = pendingManualPassage;
  if (!p) return;
  acknowledged.add(p.trip_id);
  closeSheet(manualSheetEl, manualBackdropEl);
  try {
    const res = await postObserve({ seen: false, anchor: p.when, source: 'app' });
    const undo = res.id != null ? { label: 'Annuler', onClick: () => undoObservation(res.id) } : null;
    showToast(res.recorded ? '✓ Noté : non passé' : '✓ Réponse envoyée', undo);
  } catch (err) {
    showToast('📡 Hors-ligne : réponse non envoyée');
  }
}

// ---------------------------------------------------------------------------
// Bottom sheets — mécanique commune (ouverture, glissement, fermeture)
// ---------------------------------------------------------------------------

function openSheet(sheetEl, backdropEl) {
  backdropEl.hidden = false;
  sheetEl.hidden = false;
  // Double rAF : un seul ne garantit pas que l'état de départ
  // (translateY(105%), juste après le passage de `display:none` à visible)
  // ait réellement été peint avant qu'on ne lance la transition vers l'état
  // final — sans quoi le navigateur fusionne les deux et la feuille apparaît
  // d'un coup au lieu de glisser.
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      backdropEl.classList.add('show');
      sheetEl.classList.add('show');
    });
  });
}

function closeSheet(sheetEl, backdropEl) {
  backdropEl.classList.remove('show');
  sheetEl.classList.remove('show');
  setTimeout(() => {
    backdropEl.hidden = true;
    sheetEl.hidden = true;
  }, 280);
}

/** Ferme la feuille par glissement vers le bas (tactile ou souris).
 *  Le geste ne démarre que sur la poignée ou le titre : lier tout le corps de
 *  la feuille ferait concurrence au défilement de son contenu (l'histogramme
 *  compte jusqu'à dix-huit lignes), l'un des deux perdant systématiquement en
 *  fluidité. */
function wireDragToClose(sheetEl, backdropEl, handleEl) {
  const grabZones = [handleEl, sheetEl.querySelector('.sheet-title')].filter(Boolean);

  let startY = null;
  let dragging = false;
  let pendingDy = null;
  let rafId = null;

  // Un `pointermove` peut se déclencher bien plus souvent que le taux de
  // rafraîchissement de l'écran ; n'appliquer le déplacement qu'une fois par
  // image évite de solliciter la mise en page à chaque micro-mouvement.
  const flush = () => {
    rafId = null;
    if (pendingDy !== null) sheetEl.style.transform = `translateY(${pendingDy}px)`;
  };

  const onDown = (ev) => {
    startY = ev.clientY;
    dragging = true;
    sheetEl.style.transition = 'none';
    // Peut lever si le pointeur n'est plus actif à l'instant de l'appel
    // (quelques WebView Android) : la capture n'est qu'un confort, jamais
    // requise pour que le glissement fonctionne.
    try { sheetEl.setPointerCapture?.(ev.pointerId); } catch { /* ignoré */ }
  };
  const onMove = (ev) => {
    if (!dragging || startY === null) return;
    pendingDy = Math.max(0, ev.clientY - startY);
    if (rafId === null) rafId = requestAnimationFrame(flush);
  };
  const onUp = (ev) => {
    if (!dragging) return;
    dragging = false;
    if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
    sheetEl.style.transition = '';
    const dy = startY !== null ? Math.max(0, ev.clientY - startY) : 0;
    sheetEl.style.transform = '';
    startY = null;
    pendingDy = null;
    if (dy > 70) closeSheet(sheetEl, backdropEl);
  };

  for (const zone of grabZones) {
    zone.style.touchAction = 'none';
    zone.addEventListener('pointerdown', onDown);
  }
  sheetEl.addEventListener('pointermove', onMove);
  sheetEl.addEventListener('pointerup', onUp);
  sheetEl.addEventListener('pointercancel', onUp);

  // Toucher hors zone : le fond assombri ferme la feuille.
  backdropEl.addEventListener('click', () => closeSheet(sheetEl, backdropEl));
}

// ---------------------------------------------------------------------------
// Bottom sheet : histogramme des passages (§ 7)
// ---------------------------------------------------------------------------

const histoSheetEl = document.getElementById('histo-sheet');
const histoBackdropEl = document.getElementById('histo-backdrop');
const histoBodyEl = document.getElementById('histo-body');
const histoCompareEl = document.getElementById('histo-compare');

/** Bascule la phrase de comparaison sans jamais la retirer du flux — sa
 *  hauteur reste réservée en permanence (voir app.css) : c'est ce qui évite
 *  à la feuille de changer de taille en changeant d'onglet. */
function setCompareVisible(visible) {
  histoCompareEl.classList.toggle('is-visible', visible);
}

async function openHistogram() {
  openSheet(histoSheetEl, histoBackdropEl);
  if (histoData) { renderHistoTab(); return; }
  histoBodyEl.textContent = 'Chargement…';
  setCompareVisible(false);
  try {
    histoData = await fetchJson('/api/histogram');
    renderHistoTab();
  } catch (err) {
    histoBodyEl.textContent = 'Statistiques indisponibles pour le moment.';
    setCompareVisible(false);
  }
}

function formatRatioFr(r) {
  const rounded = Math.round(r * 10) / 10;
  const isNearInt = Math.abs(rounded - Math.round(rounded)) < 0.05;
  const val = isNearInt ? Math.round(rounded) : rounded;
  return `${fmtNumberFr(val, 1)}×`;
}

// Ordre visuel des onglets (Semaine à gauche, Week-end à droite) : détermine
// le sens du glissement, pour que l'animation suive le doigt plutôt que de
// sembler arbitraire.
const HISTO_TAB_ORDER = ['weekday', 'weekend'];

function renderHistoTab() {
  if (!histoData) return;
  const series = histoData[histoTab];
  const peak = histoData.peak || 1;

  // Rejoué seulement lors d'un vrai changement d'onglet — jamais au premier
  // rendu, qui n'a rien à quitter.
  const animate = histoLastTab !== null && histoLastTab !== histoTab;
  const forward = HISTO_TAB_ORDER.indexOf(histoTab) > HISTO_TAB_ORDER.indexOf(histoLastTab);
  histoLastTab = histoTab;

  histoBodyEl.innerHTML = '';
  series.hours.forEach((h) => {
    const row = document.createElement('div');
    row.className = 'histo-row';

    const hourEl = document.createElement('span');
    hourEl.className = 'histo-hour';
    hourEl.textContent = `${pad(h.hour)}h`;

    const track = document.createElement('span');
    track.className = 'histo-bar-track';
    const fill = document.createElement('span');
    fill.className = 'histo-bar-fill';
    const pct = peak > 0 ? Math.max(0, Math.min(100, (h.total / peak) * 100)) : 0;
    fill.style.width = `${pct}%`;
    track.appendChild(fill);

    const valEl = document.createElement('span');
    valEl.className = 'histo-value';
    valEl.textContent = fmtNumberFr(h.total, 1);

    row.append(hourEl, track, valEl);
    histoBodyEl.appendChild(row);
  });

  // Phrase de comparaison, calculée depuis les données — jamais codée en dur
  // — affichée uniquement sous l'onglet Week-end (§ 7).
  if (histoTab === 'weekend') {
    const sumWeekday = histoData.weekday.hours.reduce((a, h) => a + h.total, 0);
    const sumWeekend = histoData.weekend.hours.reduce((a, h) => a + h.total, 0);
    if (sumWeekend > 0 && sumWeekday > 0) {
      const ratio = sumWeekday / sumWeekend;
      histoCompareEl.textContent = `Le week-end, ~${formatRatioFr(ratio)} moins de trains qu'en semaine.`;
      setCompareVisible(true);
    } else {
      setCompareVisible(false);
    }
  } else {
    setCompareVisible(false);
  }

  if (animate) playHistoTransition(forward);
}

/** Rejoue l'animation d'entrée sur le corps du tableau et la phrase de
 *  comparaison — deux éléments distincts dans le DOM (§ voir index.html),
 *  animés ensemble pour rester perçus comme un seul bloc qui glisse. Retirer
 *  puis réappliquer la classe (avec un reflow forcé entre les deux) est le
 *  moyen standard de rejouer une animation CSS sur le même élément d'un appui
 *  à l'autre — sans cela, la deuxième bascule vers un onglet déjà visité ne
 *  se rejoue pas, la classe étant déjà présente. */
function playHistoTransition(forward) {
  const cls = forward ? 'histo-anim-right' : 'histo-anim-left';
  for (const el of [histoBodyEl, histoCompareEl]) {
    el.classList.remove('histo-anim-right', 'histo-anim-left');
    void el.offsetWidth; // force le reflow : sans lui, remove+add se fondent en un no-op
    el.classList.add(cls);
  }
}

document.querySelectorAll('.tab').forEach((btn) => {
  btn.addEventListener('click', () => {
    histoTab = btn.dataset.tab;
    document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b === btn));
    renderHistoTab();
  });
});

// ---------------------------------------------------------------------------
// Câblage des boutons
// ---------------------------------------------------------------------------

document.getElementById('btn-seen').addEventListener('click', onSeenClick);
document.getElementById('btn-histo').addEventListener('click', openHistogram);
document.getElementById('confirm-yes').addEventListener('click', onConfirmYes);
document.getElementById('confirm-no').addEventListener('click', onConfirmNo);
document.getElementById('manual-submit').addEventListener('click', onManualSubmit);
document.getElementById('manual-not-passed').addEventListener('click', onManualNotPassed);

wireDragToClose(manualSheetEl, manualBackdropEl, document.getElementById('manual-handle'));
wireDragToClose(histoSheetEl, histoBackdropEl, document.getElementById('histo-handle'));

// ---------------------------------------------------------------------------
// Démarrage
// ---------------------------------------------------------------------------

applyDayNight(); // synchrone dès le chargement : pas d'éclair jour avant le premier tick
fetchNext();
setInterval(fetchNext, POLL_MS);
setInterval(tick, TICK_MS);

// Service worker : uniquement en contexte sécurisé (jamais sur http local),
// et il ne met en cache que le statique — jamais /api/*.
if (window.isSecureContext && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js').catch(() => { /* tant pis, l'app fonctionne sans */ });
  });
}
