// Service worker minimo: la pagina funziona anche offline con gli ultimi dati scaricati.
const CACHE = 'bisenzio-v2';
const FILES = ['./', 'index.html', 'manifest.json', 'icon.svg', 'icon-192.png', 'icon-512.png'];
self.addEventListener('install', (e) => e.waitUntil(caches.open(CACHE).then((c) => c.addAll(FILES))));
self.addEventListener('activate', (e) => e.waitUntil(caches.keys().then((k) => Promise.all(k.filter((x) => x !== CACHE).map((x) => caches.delete(x))))));
self.addEventListener('fetch', (e) => {
  // Prima la rete (dati freschi), poi la copia salvata
  e.respondWith(fetch(e.request).then((r) => {
    const copia = r.clone(); caches.open(CACHE).then((c) => c.put(e.request.url.split('?')[0], copia)); return r;
  }).catch(() => caches.match(e.request.url.split('?')[0])));
});
