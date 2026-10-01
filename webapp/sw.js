'use strict';
/*
 * Service worker minimal : réseau d'abord, cache en secours, pour les seuls
 * fichiers du site — timetable.json compris, qui permet d'afficher l'horaire
 * théorique sans réseau. Le flux temps réel SNCF, d'une autre origine, n'est
 * jamais mis en cache : un retard d'il y a une heure tromperait l'usager.
 * Enregistré uniquement en contexte sécurisé (voir app.js).
 */

const CACHE_NAME = 'nexttraintosee-static-v4';
const STATIC_FILES = [
  './',
  'index.html',
  'app.css',
  'app.js',
  'realtime.js',
  'timetable.json',
  'manifest.webmanifest',
  'assets/toulouse.jpg',
  'assets/icon-180.png',
  'assets/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_FILES)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) => Promise.all(
      names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  if (url.origin !== location.origin) return;
  if (event.request.method !== 'GET') return;

  // Réseau d'abord, cache en secours : l'app et ses horaires se mettent à
  // jour dès qu'on les republie, au lieu de servir à vie la première version
  // mise en cache.
  event.respondWith(
    fetch(event.request).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
      }
      return response;
    }).catch(() => caches.match(event.request))
  );
});
