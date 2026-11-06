/* Проверка логики Notify/Fold без браузера: вырезаем блоки из index.html и гоняем
   на стабах DOM. Запуск: node .check-notify.js */
const fs = require("fs");
const src = fs.readFileSync("client/web/index.html", "utf8");
const start = src.indexOf("const Notify={");
const end = src.indexOf("Fold.init();") + "Fold.init();".length;
const code = src.slice(start, end);

const store = {};
const toastEl = {
  hidden: true,
  _q: {},
  querySelector(sel) {
    if (!this._q[sel]) this._q[sel] = { textContent: "", dataset: {}, hidden: false, addEventListener() {} };
    return this._q[sel];
  },
  addEventListener() {},
};
const blocks = ["teleBlock", "accBlock"].map(id => ({ id, open: true, _toggle: null, addEventListener() {} }));

global.window = {
  matchMedia: () => ({ matches: true, addEventListener() {} }), // телефон
  addEventListener() {},
  AudioContext: null,
};
global.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
let titleNow = "Ноосфера";
global.document = {
  hidden: true,
  visibilityState: "hidden",
  hasFocus: () => false,
  title: "Ноосфера",
  addEventListener() {},
  querySelectorAll: sel => (sel === ".side-block" ? blocks : []),
};
try{ Object.defineProperty(globalThis.navigator,"vibrate",{configurable:true,writable:true,
  value:p=>{ global.__vib = p; return true; }}); }catch(e){ console.log("nav stub:",e.message); }
global.api = async () => [
  { id: "s1", state: "busy", title: "Починка парсера" },
  { id: "s2", state: "ready", title: "Другой чат" },
];
global.Sessions = { load: () => { global.__load = (global.__load || 0) + 1; }, open: sid => { global.__opened = sid; } };
const elById = { ntoast: toastEl };
global.$ = id => elById[id] || null;

new Function(code + "\n;global.Notify=Notify;global.Fold=Fold;")();
const { Notify, Fold } = global;

let fails = 0;
const ok = (cond, msg) => { if (!cond) { fails++; console.log("FAIL:", msg); } else console.log("ok:", msg); };

/* 1. телефон: оба блока свёрнуты по умолчанию */
ok(Fold.mobile() === true, "Fold.mobile() определил телефон");
ok(blocks.every(b => b.open === false), "на телефоне .side-block свёрнуты по умолчанию");

/* 2. липкое состояние: пользователь раскрыл «Квоты и расход» */
blocks[0]._fold = false; blocks[0].open = true;   // программно свёрнуто -> тап раскрыл
Fold.set("teleBlock", true);
ok(store["fold-teleBlock-m"] === "1", "раскрытие сохранено в localStorage (ключ телефона)");
Fold.set("teleBlock", false);
ok(Fold.get("teleBlock") === false, "состояние читается обратно");

/* 3. начальный снимок состояний — без уведомлений */
Notify.observe([{ id: "s1", state: "busy", title: "A" }, { id: "s2", state: "ready", title: "B" }]);
ok(toastEl.hidden === true, "первый снимок не уведомляет");

/* 4. busy→ready по НЕоткрытой сессии при скрытой вкладке */
Notify.observe([{ id: "s1", state: "ready", title: "Починка парсера" }, { id: "s2", state: "ready", title: "B" }]);
ok(toastEl.hidden === false, "тост показан при завершении");
ok(toastEl.querySelector(".ntx").textContent === "Починка парсера · завершено", "текст тоста: " + toastEl.querySelector(".ntx").textContent);
ok(toastEl.querySelector(".ntopen").dataset.sid === "s1", "у тоста есть сессия для «открыть»");
ok(JSON.stringify(global.__vib) === "[80,60,80]", "вибрация 80/60/80");
ok(document.title === "✓ Ноосфера", "заголовок вкладки помечен ✓: " + document.title);

/* 5. возврат фокуса снимает ✓ */
Notify.resetTitle();
ok(document.title === "Ноосфера", "заголовок сброшен при возврате фокуса");

/* 6. ошибка прогона — другой текст (busy→error) */
Notify.observe([{ id: "s1", state: "busy", title: "Починка парсера" }]);
Notify.observe([{ id: "s1", state: "error", title: "Починка парсера" }]);
ok(toastEl.querySelector(".ntx").textContent === "Починка парсера · ошибка", "текст тоста для error");

/* 7. Notification недоступен (http) — код не падает, титул остался */
ok(Notify.supported() === false, "Notification недоступен — supported()=false");
Notify.done("Прогон завершён", "тест", { name: "Сессия", sid: "s9", bad: false });
ok(toastEl.querySelector(".ntx").textContent === "Сессия · завершено", "тост работает без Notification API");

/* 8. бип без AudioContext не бросает */
Notify.arm(); Notify.beep();
ok(true, "arm()/beep() без AudioContext не падают");

/* 9. фон away() при скрытой вкладке и при фокусе */
ok(Notify.away() === true, "away() = true при document.hidden");
document.hidden = false; document.visibilityState = "visible"; document.hasFocus = () => true;
ok(Notify.away() === false, "away() = false когда владелец смотрит на экран");
document.hidden = true; document.visibilityState = "hidden"; document.hasFocus = () => false;

/* 10. poll за 5с-цикл: смена состояния подтягивает Sessions.load() */
(async () => {
  global.api = async () => [{ id: "s1", state: "busy", title: "X" }];
  await Notify.poll();
  const before = global.__load || 0;
  global.api = async () => [{ id: "s1", state: "ready", title: "X" }];
  await Notify.poll();
  ok((global.__load || 0) === before + 1, "poll: смена состояния → Sessions.load() перерисовал список");
  ok(toastEl.querySelector(".ntx").textContent === "X · завершено", "poll: тост о завершении");
  console.log(fails ? "FAILS: " + fails : "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ");
  process.exit(fails ? 1 : 0);
})();
