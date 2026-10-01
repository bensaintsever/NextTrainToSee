'use strict';
/*
 * NextTrainToSee — logique de la PWA.
 *
 * Aucun serveur : timetable.json, régénéré chaque nuit par `nexttraintosee
 * export` et publié avec l'app, donne tous les passages et tous les libellés
 * (direction_label, look, route_label…). Le téléphone lit lui-même le retard
 * sur le flux temps réel SNCF (realtime.js), et ce fichier se contente
 * d'afficher le résultat et de tenir un compte à rebours à la seconde.
 */

// ---------------------------------------------------------------------------
// Constantes et état global
// ---------------------------------------------------------------------------

const USE_MOCK = new URLSearchParams(location.search).get('mock') === '1';

const TIMETABLE_URL = 'timetable.json';
// Flux SNCF « TripUpdates », relayé sans clé par transport.data.gouv.fr, qui
// autorise l'appel direct depuis un navigateur (CORS ouvert).
const REALTIME_URL = 'https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates';
const REALTIME_POLL_MS = 90_000;      // le flux n'est rafraîchi que toutes les ~2 min
const TIMETABLE_POLL_MS = 60 * 60_000; // l'export change une fois par nuit
const TICK_MS = 1_000;         // horloge locale à la seconde
const IMMINENT_LEAD_MS = 30_000;      // état imminent dès announce_at - 30 s
const HORIZON_MS = 12 * 3600_000;     // fenêtre d'affichage des passages
// Au-delà, l'export n'a pas été republié depuis plusieurs nuits : les
// horaires restent justes jusqu'à épuisement, mais on le signale.
const STALE_TIMETABLE_MS = 2 * 86_400_000;

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

// Passages à venir, recalculés à chaque seconde depuis l'export et le dernier
// relevé temps réel (0, 1 ou 2 : le prochain en grand, le suivant en bas).
let passages = [];
// Contenu de timetable.json, et dernier relevé temps réel décodé (null tant
// qu'aucun n'a abouti, ou s'il est trop ancien pour être cru).
let timetable = null;
let realtime = null;
let realtimeAt = 0;

let histoData = null;
let histoTab = 'weekday';
let histoLastTab = null; // dernier onglet rendu : détermine le sens du glissement

// Décalage appliqué aux horaires de démonstration de assets/mock.json, fixé
// une fois puis reconduit pour que la démo se déroule en temps réel (compte
// à rebours, état imminent, passage) plutôt que de rester figée
// sur « dans 3 min ». Voir getMockData().
let mockShift = null;
let mockRaw = null;

// ---------------------------------------------------------------------------
// Utilitaires de date/heure
// ---------------------------------------------------------------------------

function pad(n, len = 2) { return String(n).padStart(len, '0'); }

/** Formate une Date en ISO 8601 avec le décalage horaire local. */
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
 *  passage suivant) au lieu de rester figés sur les valeurs figées
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

async function loadTimetable() {
  try {
    const data = await fetchJson(TIMETABLE_URL);
    if (data.format !== 1) throw new Error(`format ${data.format} inconnu`);
    timetable = data;
    histoData = data.histogram;
  } catch (err) {
    // On garde l'export déjà chargé, s'il y en a un : il reste juste.
  }
  refresh();
}

async function loadRealtime() {
  if (!timetable || document.visibilityState === 'hidden') return;
  try {
    const res = await fetch(REALTIME_URL, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const feed = NextTrainRealtime.decodeFeed(new Uint8Array(await res.arrayBuffer()));
    realtime = NextTrainRealtime.delaysAt(feed, timetable.anchor_stop_ids);
    realtimeAt = Date.now();
  } catch (err) {
    // Panne du flux : repli sur l'horaire théorique, signalé par le bandeau.
  }
  refresh();
}

/** Un relevé temps réel n'est cru que tant qu'il est récent : après une mise
 *  en veille, mieux vaut l'horaire théorique qu'un retard d'il y a une heure. */
function freshRealtime() {
  return realtime && Date.now() - realtimeAt < 3 * REALTIME_POLL_MS ? realtime : null;
}

/** Recalcule la liste affichée : depuis l'export en usage réel, depuis
 *  assets/mock.json en démonstration (?mock=1). */
async function refresh() {
  if (USE_MOCK) {
    try {
      if (!mockRaw) mockRaw = await fetchJson('assets/mock.json');
      setOffline(false);
      handleNextData(getMockData(mockRaw));
    } catch (err) {
      setOffline(true);
    }
    return;
  }
  if (!timetable) { setOffline(true); return; }
  const live = freshRealtime();
  setOffline(false);
  handleNextData({
    realtime: live !== null,
    passages: NextTrainRealtime.upcomingPassages(timetable, live, Date.now(),
                                                 { horizonMs: HORIZON_MS, limit: 2 }),
  });
}

// ---------------------------------------------------------------------------
// Bandeau d'état dégradé (horaires absents / périmés / théoriques)
// ---------------------------------------------------------------------------

const bannerEl = document.getElementById('banner');
let isOffline = false;

function setOffline(v) {
  isOffline = v;
  renderBanner();
  if (v) renderOfflinePlaceholders();
}

function renderBanner() {
  const stale = timetable && Date.now() - Date.parse(timetable.generated_at) > STALE_TIMETABLE_MS;
  if (isOffline) {
    bannerEl.hidden = false;
    bannerEl.classList.remove('theoretical');
    bannerEl.textContent = '📡 Horaires indisponibles';
  } else if (stale) {
    bannerEl.hidden = false;
    bannerEl.classList.add('theoretical');
    bannerEl.textContent = `⏱ Horaires du ${new Date(timetable.generated_at).toLocaleDateString('fr-FR')}`;
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
// Traitement d'une liste de passages
// ---------------------------------------------------------------------------

let lastRoot = null;

function handleNextData(data) {
  lastRoot = data;
  passages = data.passages || [];
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

  trainInfo.textContent = describeTrain(p);
}

/** Identité lisible d'un passage : catégorie en clair, jamais l'identifiant
 *  brut. */
function describeTrain(p) {
  const cat = CATEGORY_LABELS[p.category_id] || (p.category_id || '').toUpperCase();
  const bits = [cat, p.headsign].filter(Boolean).join(' ');
  return [bits, p.route_label || p.branch_label].filter(Boolean).join(' · ');
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
  // Recalcul complet à chaque seconde : quelques centaines de passages à
  // filtrer, rien de coûteux, et la liste avance d'elle-même quand un train
  // est passé, sans attendre le prochain relevé temps réel.
  refresh();
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
function wireDragToClose(sheetEl, backdropEl, handleEl, onDismiss = null) {
  const grabZones = [handleEl, sheetEl.querySelector('.sheet-title')].filter(Boolean);
  const dismiss = () => { closeSheet(sheetEl, backdropEl); onDismiss?.(); };

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
    if (dy > 70) dismiss();
  };

  for (const zone of grabZones) {
    zone.style.touchAction = 'none';
    zone.addEventListener('pointerdown', onDown);
  }
  sheetEl.addEventListener('pointermove', onMove);
  sheetEl.addEventListener('pointerup', onUp);
  sheetEl.addEventListener('pointercancel', onUp);

  // Toucher hors zone : le fond assombri ferme la feuille.
  backdropEl.addEventListener('click', dismiss);
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

function openHistogram() {
  openSheet(histoSheetEl, histoBackdropEl);
  if (histoData) { renderHistoTab(); return; }
  histoBodyEl.textContent = 'Statistiques indisponibles pour le moment.';
  setCompareVisible(false);
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

document.getElementById('btn-histo').addEventListener('click', openHistogram);

wireDragToClose(histoSheetEl, histoBackdropEl, document.getElementById('histo-handle'));

// ---------------------------------------------------------------------------
// Démarrage
// ---------------------------------------------------------------------------

applyDayNight(); // synchrone dès le chargement : pas d'éclair jour avant le premier tick
if (USE_MOCK) {
  refresh();
} else {
  loadTimetable().then(loadRealtime);
  setInterval(loadRealtime, REALTIME_POLL_MS);
  setInterval(loadTimetable, TIMETABLE_POLL_MS);
  // Retour au premier plan : le relevé a pu vieillir pendant la veille.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !freshRealtime()) loadRealtime();
  });
}
setInterval(tick, TICK_MS);

// Service worker : uniquement en contexte sécurisé (jamais sur http local).
if (window.isSecureContext && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js').catch(() => { /* tant pis, l'app fonctionne sans */ });
  });
}
