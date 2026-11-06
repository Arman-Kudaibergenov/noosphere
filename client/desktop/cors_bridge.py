"""CORS-мост внутри WebView2: страница ходит на сервер через Python.

Факт, снятый пробами 2026-10-12: десктопный Edge той же версии пропускает
cors-fetch из `file://` на `http://192.168.0.235:8300` (и с чистым профилем, и с
включённым PNA), а WebView2 — отклоняет (`TypeError: Failed to fetch`), хотя
no-cors проходит и сервер отдаёт корректный префлайт. Консоль в установленном SDK
недоступна, флаги среды pywebview перетирает. Чиним на месте, без правок сервера
и без локальных сокетов: шим подменяет `window.fetch` и отдаёт запрос в Python
(urllib), которому сеть доступна; ответ собирается в `Response` уже внутри
страницы, где CORS-проверок нет.

Шим внедряется `AddScriptToExecuteOnDocumentCreatedAsync` — на document-start
каждой следующей навигации, то есть раньше любых скриптов страницы: гонки с
первыми запросами boot() не возникает. Префлайтов нет вовсе: запрос не покидает
процесс как cors-запрос браузера.

Граница: Python исполняет только запросы к адресам из `appconfig.servers()`;
всё остальное и любые сбои моста падают в нативный fetch (страница честно
покажет «нет связи», а не молча соврёт). Ответы буферизуются — web-UI не стримит
(проверено: нет getReader/TextDecoder), загрузки и STT ходят кусками.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from urllib.parse import urlparse
from . import appconfig, applog

TIMEOUT = 120.0
DROP_HEADERS = {"host", "content-length", "connection", "origin", "referer",
                "accept-encoding", "keep-alive"}

SHIM_JS = r"""
(function(){
  window.__noosphereShim = 1;
  if (window.__noosphereCorsShim) { return; }
  window.__noosphereCorsShim = true;
  window.__noosphereShimHits = 0;
  window.__noospherePageErr = "";
  /* ловим тихие провалы страницы: page_check оболочки читает это из app.log-пробы */
  window.addEventListener("error", function(e){
    window.__noospherePageErr = (e.message || "error") + " @" + (e.lineno || 0);
  });
  window.addEventListener("unhandledrejection", function(e){
    window.__noospherePageErr = "unhandled: " + ((e.reason && e.reason.message) || e.reason || "?");
  });
  var orig = window.fetch ? window.fetch.bind(window) : null;
  if (!orig) { return; }
  var hits = 0;
  /* маячок для внешней диагностики: счётчик перехватов в заголовке окна */
  function beacon(){ hits += 1; window.__noosphereShimHits = hits;
    try { document.title = "Ноосфера · shim=" + hits; } catch (e) {} }
  /* стадийные маячки (заказ владельца 2026-10-13): каждая стадия fetch уходит
     в app.log через js_api beacon — видно, где именно вызов умирает в живом окне */
  function B(m){ try { if (window.pywebview && window.pywebview.api &&
      window.pywebview.api.beacon) { window.pywebview.api.beacon(m); } } catch (e) {} }
  /* async-callback мост (2026-10-13): cors_fetch ничего не возвращает; результат
     пушится из Python в __noosphereCorsDone единственным каналом evaluate_js
     под замком оболочки — без внутренних evaluate_js pywebview на возврат js_api */
  var pending = {}, nextId = 1;
  window.__noosphereCorsDone = function(id, payloadJson){
    var p = pending[id]; if (!p) { return; } delete pending[id];
    if (p.tm) { clearTimeout(p.tm); }
    var r = null; try { r = JSON.parse(payloadJson); } catch (e) { r = null; }
    p.res(r);
  };
  function toB64(buf){
    var bytes = new Uint8Array(buf), bin = "", CH = 0x8000, i;
    for (i = 0; i < bytes.length; i += CH) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    }
    return btoa(bin);
  }
  function encText(s){ return toB64(new TextEncoder().encode(s).buffer); }
  window.fetch = function(input, init){
    init = init || {};
    var url = typeof input === "string" ? input : ((input && input.url) || "");
    if (!/^https?:\/\//i.test(url)) { return orig(input, init); }
    var bridge = window.pywebview && window.pywebview.api;
    if (!bridge || !bridge.cors_fetch) { return orig(input, init); }
    var method = (init.method || (input && input.method) || "GET").toUpperCase();
    B("shim in " + method + " " + url);
    var headers = {}, src = init.headers || (input && input.headers) || {};
    if (typeof src.forEach === "function") { src.forEach(function(v, k){ headers[k] = v; }); }
    else { Object.keys(src).forEach(function(k){ headers[k] = src[k]; }); }
    var body = (init.body !== undefined) ? init.body : (input && input.body);
    var payload;
    if (body == null) { payload = Promise.resolve(null); }
    else if (typeof body === "string") { payload = Promise.resolve(encText(body)); }
    else if (body instanceof ArrayBuffer) { payload = Promise.resolve(toB64(body)); }
    else if (ArrayBuffer.isView(body)) {
      payload = Promise.resolve(toB64(body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength)));
    }
    else if (typeof Blob !== "undefined" && body instanceof Blob) {
      payload = body.arrayBuffer().then(toB64);
    }
    else if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) {
      payload = Promise.resolve(encText(body.toString()));
    }
    else { return orig(input, init); }   /* FormData и прочее — нативно */
    return payload.then(function(b64){
      beacon();
      /* висящий вызов моста не должен оставлять панель пустой навсегда:
         таймаут превращает hang в обычную ошибку fetch с сообщением */
      B("shim call " + url);
      var id = nextId++;
      var call = new Promise(function(res){ pending[id] = {res: res}; });
      bridge.cors_fetch(id, url, method, headers, b64);
      /* поллы — дело секунд; ход и классификатор — десятков секунд: гард не должен
         выдавать медленный сервер за мёртвый мост (сбой 2026-10-13: /v1/route ответил
         за 45 с, /v1/chat за 54 с со статусом 200, а гард 25 с уже отверг промис,
         вернул текст в поле и нарисовал баннер) */
      /* ход идёт ДО РЕЗУЛЬТАТА: серверный бюджет хода — 30 минут, поэтому гард
         клиента заведомо выше, иначе длинный ход обрывается ложным «Сбой связи»
         (урок 2026-10-13: гард 25 с уже отвергал живые медленные ответы) */
      var guardMs = (method === "GET" || method === "HEAD") ? 25000 : 2400000;
      var guard = new Promise(function(_, rej){
        pending[id].tm = setTimeout(function(){ B("shim guard " + url); delete pending[id];
          rej(new Error("cors-мост не ответил за " + (guardMs / 1000) + " с")); }, guardMs);
      });
      return Promise.race([call, guard]);
    }).then(function(r){
      B("shim race " + url + " " + (r && r.status));
      if (!r || !r.ok_bridge) { return orig(input, init); }
      var bin = r.body_b64
        ? Uint8Array.from(atob(r.body_b64), function(c){ return c.charCodeAt(0); })
        : new Uint8Array(0);
      var noBody = r.status === 204 || r.status === 205 || r.status === 304;
      var resp = new Response(noBody ? null : bin,
        { status: r.status, statusText: r.status_text || "", headers: r.headers || {} });
      B("shim resp " + url);
      return resp;
    }, function(e){ B("shim race-err " + url + " " + (e && e.message)); throw e; });
  };
})();
"""


def allowed(url: str) -> bool:
    """Только известные серверы владельца: страница своя, но граница дешёвая."""
    try:
        target = urlparse(url)
    except ValueError:
        return False
    for server in appconfig.servers():
        known = urlparse(server)
        if (target.scheme, target.hostname, target.port) == (known.scheme, known.hostname,
                                                             known.port):
            return True
    return False


def fetch(url: str, method: str = "GET", headers: dict | None = None,
          body_b64: str | None = None) -> dict:
    """Исполняет запрос страницы и возвращает всё для сборки Response в JS."""
    if not allowed(url):
        return {"ok_bridge": False, "error": "origin not allowed"}
    data = base64.b64decode(body_b64) if body_b64 else None
    clean = {k: v for k, v in (headers or {}).items() if k.lower() not in DROP_HEADERS}
    request = urllib.request.Request(url, data=data, headers=clean, method=method)
    applog.info("cors fetch in", url=url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            raw = resp.read()
            applog.info("cors fetch http", url=url, status=resp.status,
                        bytes=len(raw))
            status, status_text = resp.status, getattr(resp, "reason", "") or ""
            out_headers = {k: v for k, v in resp.headers.items()
                           if k.lower() not in ("content-encoding", "transfer-encoding",
                                                "content-length", "set-cookie")}
    except urllib.error.HTTPError as exc:
        # статусы ошибок нужны странице как есть: 401/403/404 она разбирает сама
        raw = exc.read()
        status, status_text = exc.code, exc.reason or ""
        out_headers = {k: v for k, v in (exc.headers or {}).items()
                       if k.lower() not in ("content-encoding", "transfer-encoding",
                                            "content-length", "set-cookie")}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        applog.warn("cors bridge network fail", url=url, method=method,
                    error=f"{type(exc).__name__}: {exc}")
        return {"ok_bridge": False, "error": f"{type(exc).__name__}: {exc}"}
    out = {"ok_bridge": True, "status": status, "status_text": status_text,
           "headers": out_headers,
           "body_b64": base64.b64encode(raw).decode("ascii") if raw else None}
    applog.info("cors fetch out", url=url, status=status)
    return out


def attach(window, server: str = "", token: str = "") -> bool:
    """Внедряет шим и конфиг на document-start всех следующих навигаций окна.

    Конфиг (`window.__noosphere_cfg`) попадает в страницу раньше ЛЮБЫХ её скриптов,
    поэтому boot() не падает в prompt() из-за гонки с внедрением моста pywebview
    (провалы 2026-10-12: prompt на холодном старте и после reload). Токен живёт
    только в памяти рендерера: в URL, историю и журналы не пишется. Повторный
    вызов (retry) добавляет свежий конфиг — он перетирает прежний.
    """
    try:
        from System import Func, Type  # pythonnet, доступен только под Windows-GUI
        from webview.platforms.winforms import BrowserView
    except Exception as exc:
        applog.error("cors bridge import failed", error=f"{type(exc).__name__}: {exc}")
        return False
    inst = BrowserView.instances.get(getattr(window, "uid", None))
    if inst is None and BrowserView.instances:
        inst = list(BrowserView.instances.values())[0]
    if inst is None:
        applog.error("cors bridge: no winforms instance")
        return False
    cfg_js = ("window.__noosphereShell=1;window.__noosphere_cfg={server:"
              + json.dumps(server or "") + ",token:" + json.dumps(token or "") + "};")

    def _run():
        # возвращаем None: pywebview маршалит Invoke через Func[Type], и любое
        # не-None значение ломает преобразование ("value cannot be converted to Type")
        core = inst.webview.CoreWebView2
        core.AddScriptToExecuteOnDocumentCreatedAsync(SHIM_JS + cfg_js)

    try:
        # синхронно: шим обязан встать ДО навигации на index.html, иначе первые
        # fetch страницы уйдут нативно и окно покажет «нет связи» (провал 10:44).
        # Invoke из таймера навигации безопасен: UI-поток в этот момент простаивает
        # (deadlock 10:42 был на старой версии без логов, воспроизвести не удалось).
        inst.Invoke(Func[Type](_run))
    except Exception as exc:
        applog.error("cors bridge attach failed", error=f"{type(exc).__name__}: {exc}")
        return False
    applog.info("cors bridge attached", servers=len(appconfig.servers()), cfg=bool(server))
    return True


def invoke_on_ui(window, fn) -> bool:
    """Выполнить fn на UI-потоке winforms: навигация load_url из фоновых потоков
    убивает мост рендерера (инцидент 2026-10-12 15:03)."""
    try:
        from System import Func, Type  # pythonnet, доступен только под Windows-GUI
        from webview.platforms.winforms import BrowserView
    except Exception as exc:
        applog.error("invoke_on_ui import failed", error=f"{type(exc).__name__}: {exc}")
        return False
    inst = BrowserView.instances.get(getattr(window, "uid", None))
    if inst is None and BrowserView.instances:
        inst = list(BrowserView.instances.values())[0]
    if inst is None:
        applog.error("invoke_on_ui: no winforms instance")
        return False
    try:
        inst.Invoke(Func[Type](fn))
        return True
    except Exception as exc:
        applog.error("invoke_on_ui failed", error=f"{type(exc).__name__}: {exc}")
        return False


def selfcheck() -> dict:
    """Для selftest: граница держит чужой origin, свой — исполняется."""
    foreign = fetch("http://example.com/", "GET")
    own = fetch(appconfig.DEFAULT_SERVERS[0] + "/health", "GET",
                {"Content-Type": "application/json"})
    return {"foreign_blocked": foreign.get("error") == "origin not allowed",
            "own_ok": bool(own.get("ok_bridge")) and own.get("status") == 200,
            "own_status": own.get("status"), "shim_bytes": len(SHIM_JS)}
