'use strict';
/*
 * Service worker minimal : ne met en cache QUE le statique de la PWA.
 * Toute requête vers /api/* traverse toujours le réseau — les passages et
 * l'histogramme sont dynamiques, les mettre en cache tromperait l'usager.
 * Enregistré uniquement en contexte sécurisé (voir app.js) ; absent sur
 * http local, où l'app fonctionne à l'identique sans lui (§ 1 du doc de
 * référence).
 */

const CACHE_NAME = 'nexttraintosee-static-v3';
const STATIC_FILES = [
  './',
  'index.html',
  'app.css',
  'app.js',
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

  // Jamais de cache pour l'API : toujours frais, toujours réseau.
  if (url.pathname.startsWith('/api/')) return;
  if (event.request.method !== 'GET') return;

  // Réseau d'abord, cache en secours : le serveur est sur le réseau local,
  // donc quasi toujours joignable — et l'app se met à jour dès qu'on la
  // redéploie, au lieu de servir à vie la première version mise en cache.
  event.respondWith(
    fetch(event.request).then((response) => {
      if (response.ok && url.origin === location.origin) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
      }
      return response;
    }).catch(() => caches.match(event.request))
  );
});
