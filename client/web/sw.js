/* Ноосфера: service worker ради уведомлений и установки PWA.
   Android Chrome запрещает new Notification() из страницы — показать уведомление
   можно лишь через registration.showNotification(). Кэша и офлайна здесь нет:
   клиент всегда живёт с сервером, устаревшая копия интерфейса вреднее пустого
   экрана. Никакой Cache Storage не заводим и в fetch ничего не подменяем, так
   что старый index.html из воркера приехать не может.

   sw-версия: 2026-11-02 (жалоба 01.11 — в десктопном окне жил прежний UI;
   корень был в липком localStorage клиента, ключи свёрнутости теперь
   версионированы, см. client/web/index.html, блок Fold). Строка версии нужна,
   чтобы браузер увидел изменённый текст воркера и переустановил его без ручной
   чистки кэша: сервер отдаёт sw.js с Cache-Control: no-cache, а страница
   регистрирует его с updateViaCache:"none". Меняется вместе с правкой клиента. */
const SW_VERSION = "2026-11-02-fold-keys-v2";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

/* Пуш от сервера (server/push.py). Если прямо сейчас на экране открыто окно
   Ноосферы — не мигаем уведомлением: человек и так видит ответ, ему хватит
   тоста внутри приложения. */
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = {body: e.data ? e.data.text() : ""}; }
  e.waitUntil((async () => {
    let visible = false;
    try {
      const all = await self.clients.matchAll({type: "window", includeUncontrolled: true});
      visible = all.some(c => c.visibilityState === "visible" || c.focused);
    } catch (_) {}
    if (visible) return;
    await self.registration.showNotification(d.title || "Ноосфера", {
      body: (d.body || "").slice(0, 200),
      tag: d.tag || "noosphere-done",
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      renotify: true,
      data: {url: d.url || "/", sid: d.sid || ""}
    });
  })());
});

/* fetch — только пропуск в сеть, ничего не подменяем. Chrome на Android
   считает приложение устанавливаемым лишь когда есть обработчик fetch, а кэша
   здесь нет намеренно (см. выше). Пустой waitUntil не нужен: браузер грузит сам */
self.addEventListener("fetch", () => {});

/* Подписка умерла (сброс Chrome, переустановка приложения): переоформляем на
   том же ключе сервера и отдаём её открытому окну — токен есть только в
   странице, сам SW на /v1/push/subscribe не авторизуется */
self.addEventListener("pushsubscriptionchange", e => {
  e.waitUntil((async () => {
    try {
      const old = e.oldSubscription;
      if (!old) return;
      const opts = {userVisibleOnly: true};
      if (old.options && old.options.applicationServerKey) {
        opts.applicationServerKey = old.options.applicationServerKey;
      }
      const s = await self.registration.pushManager.subscribe(opts);
      const all = await self.clients.matchAll({type: "window", includeUncontrolled: true});
      for (const c of all) c.postMessage({type: "push-resub", sub: s.toJSON()});
    } catch (_) {}
  })());
});

/* Список окон нужен трижды: сразу после пробуждения SW браузер отдаёт пустой
   массив, хотя окно на экране есть (Windows Chrome после «сна» вкладки, PWA на
   Android после выгрузки из памяти). 2-3 повтора с паузой 150-300 мс дают
   браузеру время заполнить clients — без них обработчик считал, что окон нет,
   и открывал новое. */
const CLICK_DELAYS = [150, 300];

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function findWindows(tries) {
  for (let i = 0; i < tries; i++) {
    try {
      const all = await self.clients.matchAll({type: "window", includeUncontrolled: true});
      if (all && all.length) return all;
    } catch (_) {}
    if (i + 1 < tries) await sleep(CLICK_DELAYS[i] || 200);
  }
  return [];
}

/* Просим открытое окно показать сессию и ждём подтверждения. Подтверждение
   нужно, чтобы отличить живой клиент (в нём есть и токен, и состояние) от
   старой вкладки без обработчика: только во втором случае оправдана навигация. */
function askSession(win, sid) {
  return new Promise(resolve => {
    let settled = false;
    const done = v => { if (!settled) { settled = true; resolve(v); } };
    try {
      const ch = new MessageChannel();
      ch.port1.onmessage = ev => {
        if (ev.data && ev.data.type === "open-session-ack") done(true);
      };
      win.postMessage({type: "open-session", sid: sid}, [ch.port2]);
    } catch (_) { return done(false); }
    setTimeout(() => done(false), 600);
  });
}

/* Тап по уведомлению (Windows Chrome и PWA на Android, 2026-11-03).
   Раньше обработчик на пустом результате clients.matchAll({type:"window"})
   вызывал clients.openWindow(url) — Chrome открывал ВТОРОЕ окно поверх
   существующего, и человек терял полноэкранный вид (фокус уходил в новое
   окно, а старое оставалось в обычном режиме). Порядок теперь такой:
     1) matchAll({type:"window", includeUncontrolled:true}) с 2-3 повторами —
        клиент, открытый до активации SW, тоже попадает в список;
     2) окно есть → focus() (возвращает полноэкранный вид) и переход к нужной
        сессии: сначала сообщением (страница с обработчиком сама откроет чат,
        ничего не перезагружая), затем navigate(url) для клиента без
        обработчика, а если navigate недоступен — снова postMessage;
     3) окна действительно нет (приложение закрыто) — только тогда openWindow.
   Уведомление закрываем в обоих путях: и при фокусе, и при открытии окна. */
self.addEventListener("notificationclick", e => {
  const note = e.notification;
  const data = (note && note.data) || {};
  const sid = data.sid || "";
  const url = sid ? "/?sid=" + encodeURIComponent(sid) : (data.url || "/");
  e.waitUntil((async () => {
    const closeNote = () => { try { note.close(); } catch (_) {} };
    try {
      const all = await findWindows(3);
      const win = all.filter(c => "focus" in c)[0] || null;
      if (!win) {
        /* клиентов реально нет: окно приложения закрыто или выгружено */
        try { return await self.clients.openWindow(url); } catch (_) { return null; }
      }
      try { await win.focus(); } catch (_) {}
      if (await askSession(win, sid)) return win;
      if ("navigate" in win) {
        try { await win.navigate(url); } catch (_) {}
      } else {
        try { win.postMessage({type: "open-url", url: url, sid: sid}); } catch (_) {}
      }
      try { await win.focus(); } catch (_) {}
      return win;
    } finally {
      closeNote();
    }
  })());
});
