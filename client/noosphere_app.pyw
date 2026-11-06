"""Ноосфера — десктоп-приложение Windows: окно pywebview + трей + уведомления.

Запуск: `pythonw client\\noosphere_app.pyw [--minimized] [--notify-test] [--diagnose]`
Установка ярлыков и автозапуска: `python client\\desktop\\install_windows.py`
Самопроверка логики: `python client\\desktop\\selftest.py`

Граница ответственности (решение владельца 2026-10-11): сервер, инструменты и
секреты живут на Mac mini :8300, ноутбук — только окно поверх них. Оболочка
добавляет то, чего странице не хватает: один экземпляр, трей, тосты о crit,
автозапуск и экран диагностики вместо белого окна.

Сворачивание вместо закрытия: крестик прячет окно в трей, реально гасит только
«Выйти» в меню трея — иначе «закрыл окно» терял бы наблюдателя и тосты.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # client/

from desktop import appconfig, applog, autostart, cors_bridge, diagnostics  # noqa: E402
from desktop import devrun, httpapi, notify, single_instance, tray, updater, watcher  # noqa: E402

try:
    import webview
except Exception as exc:  # pythonnet/WebView2 могут отсутствовать — журнал скажет чем
    applog.error("webview import failed", error=f"{type(exc).__name__}: {exc}")
    raise


class Runtime:
    """Живое состояние процесса: сервер, токен, окно, трей, стоп-флаг."""

    def __init__(self) -> None:
        self.server = ""
        self.token = ""
        self.reason = ""
        self.checks: list[diagnostics.Check] = []
        self.window = None
        self.tray: tray.Tray | None = None
        self.dispatcher: notify.Dispatcher | None = None
        self.stop = threading.Event()
        self.force_quit = False

    def endpoint(self) -> tuple[str, str]:
        return self.server, self.token


# Splash первой страницей: холодный старт WebView2 занимает секунды, а мост
# pywebview внедряется только на NavigationCompleted. Если сразу грузить index.html,
# его страховочный таймаут 2 с успевает дёрнуть prompt() до моста (поймано
# скриншотом 2026-10-11). Splash прогревает среду, и индекс стартует с живым мостом.
# Query-параметры с сервером/токеном не вариант: WebView2 не грузит file:// с query
# (ERR_FILE_NOT_FOUND, проверено пробой 2026-10-11).
SPLASH_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<style>html,body{margin:0;height:100%;background:#EDF1EA;display:flex;align-items:center;
justify-content:center;font:15px/1.5 "Golos Text","Segoe UI",system-ui,sans-serif;color:#5A675F}
.b{display:flex;flex-direction:column;align-items:center;gap:14px}
.d{width:14px;height:14px;border-radius:50%;background:#155D4B;animation:p 1s infinite ease-in-out}
@keyframes p{0%,100%{transform:scale(.7);opacity:.5}50%{transform:scale(1);opacity:1}}
@media (prefers-reduced-motion:reduce){.d{animation:none}}</style></head>
<body><div class="b"><div class="d"></div><div>Ноосфера запускается…</div></div>
<script>window.__go=function(u){location.replace(u);};</script></body></html>"""


def _ask_and_save_server() -> bool:
    """Диалог первого запуска: адрес сервера Ноосферы. Пробуем введённый адрес,
    при живом /health сохраняем в client.json. True = адрес принят и жив."""
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    try:
        url = simpledialog.askstring(
            "Ноосфера — первый запуск",
            "Сервер не найден по адресам по умолчанию.\n"
            "Введи адрес сервера (например, http://192.168.1.10:8300):",
            parent=root)
    finally:
        root.destroy()
    if not url or not url.strip():
        return False
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "http://" + url
    from desktop import httpapi
    if not httpapi.probe(url, timeout=4.0).ok:
        applog.info("first-run server rejected (probe failed)", server=url)
        return False
    appconfig.save_servers([url])
    applog.info("first-run server saved", server=url)
    return True


def refresh_runtime(rt: Runtime, prefix: str = "boot") -> dict:
    """Пробы серверов + токен → решение «UI или диагностика»."""
    checks, alive = diagnostics.run_checks()
    if alive is None and not os.environ.get("NOOSPHERE_SERVERS") \
            and not appconfig._saved_servers():
        # первый запуск на чистой машине: дефолт (loopback) мёртв, списка нет —
        # спрашиваем адрес сервера один раз и запоминаем
        if _ask_and_save_server():
            checks, alive = diagnostics.run_checks()
    rt.checks = checks
    decision, second = diagnostics.decide(checks, alive)
    if decision == "ui":
        rt.server, rt.token, rt.reason = second or "", appconfig.read_token(), ""
        applog.info(f"{prefix} decision=ui", server=rt.server)
    else:
        rt.server, rt.token, rt.reason = "", "", second or ""
        applog.info(f"{prefix} decision=diagnose", reason=rt.reason)
    return {"decision": decision, "server": rt.server, "reason": rt.reason}


def show_window(rt: Runtime, source: str = "") -> None:
    window = rt.window
    if not window:
        return
    applog.info("window show", source=source)
    window.show()
    window.restore()


_JS_LOCK = threading.Lock()


def _eval_js(window, js: str):
    """evaluate_js под замком: параллельные вызовы из разных потоков вешают
    старт окна (инцидент 2026-10-12 18:39: measure_bridge + page_check)."""
    with _JS_LOCK:
        return window.evaluate_js(js)


def page_check(window, via: str) -> None:
    """Что реально в DOM окна: шторка и её вкладка «Доступы».

    Жалоба 2026-10-12 «вкладки доступа нет» не имела ответа в журнале: теперь
    после каждой навигации состав вкладок шторки виден в app.log.
    """
    probe = ("JSON.stringify({vault:!!document.getElementById('tabVault'),"
             "tabs:[].slice.call(document.querySelectorAll('.dtab'))"
             ".map(function(t){return t.textContent.trim();}),"
             "err:window.__noospherePageErr||'',"
             "shim:window.__noosphereShimHits||0,"
             "apis:Object.keys((window.pywebview&&window.pywebview.api)||{}).join(','),"
             "vaultKids:(document.getElementById('paneVault')"
             "||{children:[]}).children.length})")
    try:
        raw = _eval_js(window, probe)
        state = json.loads(raw) if raw else {}
    except Exception as exc:
        applog.warn("page check failed", via=via, error=f"{type(exc).__name__}: {exc}")
        return
    applog.info("page check", via=via, vault_tab=bool(state.get("vault")),
                drawer_tabs="|".join(state.get("tabs") or []),
                shim_hits=state.get("shim"), vault_pane_kids=state.get("vaultKids"),
                page_err=state.get("err") or "-")


def open_drawer(rt: Runtime, tab: str) -> None:
    """Показать окно и раскрыть нужную вкладку шторки в уже загруженном UI."""
    window = rt.window
    if not window or not rt.server:
        return
    show_window(rt, source="drawer:" + tab)
    js = (f"if(window.Drawer){{ if(!Drawer.isOpen()) Drawer.toggle(); "
          f"setTimeout(function(){{ Drawer.showTab({tab!r}); }}, 0); }}")
    try:
        _eval_js(window, js)
    except Exception as exc:
        applog.warn("evaluate_js failed", tab=tab, error=f"{type(exc).__name__}: {exc}")
    # проба после открытия вкладки: page_err клика по «Доступам» виден в журнале
    # сразу, а не на следующей навигации
    threading.Timer(2.5, page_check, args=(window, "drawer:" + tab)).start()


class Bridge:
    """js_api окна: контракт web-UI (get_config) + управление из страницы диагностики."""

    def __init__(self, rt: Runtime) -> None:
        # _rt: pywebview util.get_functions пропускает атрибуты с подчёркиванием;
        # без этого интроспекция js-моста рекурсивно лезет в Runtime.window и
        # намертво блокируется на свойствах Window до GUI-ready — цикл deadlock'а
        # с UI-потоком (лотерея виснутых стартов 15:07–18:1x)
        self._rt = rt

    def get_config(self) -> dict:
        return {"server": self._rt.server, "token": self._rt.token}

    def cors_fetch(self, cid: int, url: str, method: str = "GET",
                   headers: dict | None = None, body_b64: str | None = None) -> None:
        """Сеть для шима, fire-and-forget: немедленно возвращает None.

        Результат пушится в страницу `window.__noosphereCorsDone` через `_eval_js`
        под `_JS_LOCK` — единый канал Python→JS без внутренних evaluate_js
        pywebview на возврат js_api (гонка этого канала глушила живое окно,
        раунд 6: page_check немел, возвраты fetch терялись, панель молчала).
        """
        threading.Thread(target=self._cors_worker,
                         args=(cid, url, method, headers or {}, body_b64),
                         name=f"cors-{cid}", daemon=True).start()

    def _cors_worker(self, cid: int, url: str, method: str, headers: dict,
                     body_b64: str | None) -> None:
        try:
            out = cors_bridge.fetch(url, method, headers, body_b64)
        except Exception as exc:
            out = {"ok_bridge": False, "error": f"{type(exc).__name__}: {exc}"}
        payload = json.dumps(out, ensure_ascii=False)
        js = (f"window.__noosphereCorsDone && "
              f"window.__noosphereCorsDone({cid}, {json.dumps(payload)});")
        try:
            _eval_js(self._rt.window, js)
        except Exception as exc:
            applog.warn("cors push failed", cid=cid, url=url,
                        error=f"{type(exc).__name__}: {exc}")

    def get_diag(self) -> dict:
        return diagnostics.payload(self._rt.checks,
                                   "ui" if self._rt.server else "diagnose",
                                   self._rt.server, self._rt.reason)

    def retry(self) -> dict:
        out = refresh_runtime(self._rt, prefix="retry")
        if out["decision"] == "ui" and self._rt.window:
            cors_bridge.attach(self._rt.window, self._rt.server, self._rt.token)
            cors_bridge.invoke_on_ui(self._rt.window,
                                     lambda: self._rt.window.load_url(
                                         appconfig.INDEX_HTML.as_uri()))
            applog.info("ui shown", server=self._rt.server, via="retry")
        return out

    def beacon(self, msg: str) -> None:
        """Стадийный маячок шима/страницы (заказ 2026-10-13): стадии fetch в app.log.

        Kwarg НЕ msg: первый позиционный параметр applog.info сам называется msg —
        иначе TypeError на каждом маячке (page_err 2026-10-13 04:25).
        """
        applog.info("shim beacon", text=str(msg)[:240])

    def devrun_start(self, task: str) -> dict:
        """Кнопка «В прогон» вкладки «Прогоны»: headless qwen в изолированном worktree."""
        if not self._rt.server:
            return {"ok": False, "error": "нет живого сервера"}
        return devrun.start(self._rt.server, self._rt.token, str(task or ""))

    def devrun_log(self, rid: str) -> str:
        return devrun.read_log(str(rid))

    def open_log(self) -> str:
        path = str(appconfig.log_path())
        try:
            os.startfile(path)  # noqa: S606 — открываем собственный журнал
            return path
        except OSError as exc:
            applog.warn("open_log failed", error=str(exc))
            return path

    def notify_test(self) -> int:
        """Явный тестовый тост (ключ --notify-test и пункт меню)."""
        if not self._rt.tray:
            return 0
        payload = notify.build_payload(notify.Notice(
            "info", "Ноосфера · проверка тоста",
            "Уведомления работают. Клик по тосту открывает шторку.", "alerts"))
        return int(self._rt.tray.send(payload))

    def shell_update(self) -> dict:
        """Кнопка ⟳ / пункт трея: git pull --ff-only и перезагрузка страницы окна."""
        out = updater.pull()
        if not out["ok"]:
            return out
        if self._rt.window:
            target = (appconfig.INDEX_HTML if self._rt.server
                      else appconfig.DIAGNOSE_HTML)
            cors_bridge.invoke_on_ui(self._rt.window,
                                     lambda: self._rt.window.load_url(target.as_uri()))
            applog.info("ui reloaded", via="update", page=target.name)
            threading.Timer(2.5, page_check, args=(self._rt.window, "update")).start()
        if self._rt.dispatcher:
            text = (f"Обновлено файлов: {len(out['files'])} "
                    f"({out['before']}→{out['after']})"
                    + (" — оболочке нужен перезапуск: кнопка ⟳ стала ↻"
                       if out["shell_changed"] else "")) if out["files"] \
                else f"Уже свежая версия ({out['after']})"
            self._rt.dispatcher.push(notify.Notice("info", "Ноосфера · обновление", text, ""))
        return out

    def shell_restart(self) -> dict:
        """↻ после обновления оболочки: новая копия ждёт mutex, старая гаснет сама."""
        argv = [a for a in sys.argv[1:] if a != "--notify-test"]
        pid = updater.restart(argv)
        threading.Timer(1.2, self.quit_app).start()
        return {"ok": True, "pid": pid}

    def set_autostart(self, on: bool) -> dict:
        if on:
            autostart.install()
        else:
            autostart.remove()
        state = autostart.read()
        applog.info("autostart changed", enabled=state["enabled"])
        return state

    def quit_app(self) -> None:
        self._rt.force_quit = True
        if self._rt.window:
            self._rt.window.destroy()


def make_menu(rt: Runtime) -> list[tray.MenuItem]:
    item = tray.MenuItem
    return [
        item(appconfig.CMD_SHOW, "Показать Ноосферу"),
        item(appconfig.CMD_DRAWER_ALERTS, "Алерты контура"),
        item(appconfig.CMD_DRAWER_VAULT, "Доступы и секреты"),
        item(0, separator=True),
        item(appconfig.CMD_CHECK, "Проверить связь"),
        item(appconfig.CMD_UPDATE, "Получить обновление"),
        item(appconfig.CMD_AUTOSTART, "Автозапуск при входе", checked=autostart.enabled()),
        item(appconfig.CMD_NOTIFY_TEST, "Тестовое уведомление"),
        item(0, separator=True),
        item(appconfig.CMD_QUIT, "Выйти"),
    ]


def on_command(rt: Runtime, command: int, payload: str = "") -> None:
    if command == appconfig.CMD_SHOW:
        show_window(rt, source="tray")
    elif command == appconfig.CMD_DRAWER_ALERTS:
        open_drawer(rt, "alerts")
    elif command == appconfig.CMD_DRAWER_VAULT:
        open_drawer(rt, "vault")
    elif command == appconfig.CMD_BALLOON:
        open_drawer(rt, payload or "alerts")
    elif command == appconfig.CMD_CHECK:
        out = refresh_runtime(rt, prefix="manual-check")
        if rt.dispatcher:
            rt.dispatcher.push(notify.Notice(
                "info", "Ноосфера · проверка связи",
                (f"Сервер жив: {out['server']}" if out["decision"] == "ui"
                 else f"Сервер недоступен: {out['reason']}"),
                "alerts" if out["decision"] == "ui" else ""))
        if out["decision"] == "ui" and rt.window:
            current = rt.window.get_current_url() or ""
            if "diagnose" in current:
                cors_bridge.invoke_on_ui(rt.window,
                                         lambda: rt.window.load_url(
                                             appconfig.INDEX_HTML.as_uri()))
    elif command == appconfig.CMD_AUTOSTART:
        on = autostart.toggle()
        if rt.dispatcher:
            rt.dispatcher.push(notify.Notice(
                "info", "Ноосфера · автозапуск",
                "Включён: приложение поднимется в трей при входе в Windows."
                if on else "Выключен: запуск только вручную, с ярлыка.", ""))
    elif command == appconfig.CMD_NOTIFY_TEST:
        Bridge(rt).notify_test()
    elif command == appconfig.CMD_UPDATE:
        threading.Thread(target=Bridge(rt).shell_update, name="noosphere-update",
                         daemon=True).start()
    elif command == appconfig.CMD_QUIT:
        applog.info("quit requested", source="tray")
        rt.force_quit = True
        if rt.window:
            rt.window.destroy()


def background(rt: Runtime, si: single_instance.SingleInstance, args) -> None:
    """Поток pywebview: поднимает трей, наблюдателя и слушателя «покажи окно»."""
    rt.tray = tray.Tray(lambda cmd, payload: on_command(rt, cmd, payload),
                        lambda: make_menu(rt),
                        icon_path=appconfig.ICON_PATH if appconfig.ICON_PATH.exists() else None)
    tray_ok = rt.tray.start()
    if tray_ok:
        rt.tray.set_tooltip(f"Ноосфера · {rt.server or 'нет связи'}")
    rt.dispatcher = notify.Dispatcher(rt.tray.send if rt.tray else lambda _p: 0)

    def _devrun_toast(rid: str, status: str, summary: str) -> None:
        """Финиш прогона доработки — тост: владелец ждал результат из чата."""
        if not rt.tray:
            return
        payload = notify.build_payload(notify.Notice(
            "info", f"Ноосфера · прогон {rid[:8]}",
            f"{status}: {summary[:120]}", "alerts"))
        rt.tray.send(payload)

    devrun.FINISH_HOOK = _devrun_toast

    threading.Thread(target=si.watch_show,
                     args=(lambda: show_window(rt, source="second-instance"), rt.stop),
                     name="noosphere-show", daemon=True).start()

    obs = watcher.Watcher(rt.endpoint, rt.dispatcher)
    threading.Thread(target=obs.run, args=(rt.stop,), name="noosphere-watcher",
                     daemon=True).start()
    threading.Thread(target=devrun.apply_watcher, args=(rt.endpoint, rt.stop),
                     name="noosphere-devrun-apply", daemon=True).start()

    if args.notify_test:
        Bridge(rt).notify_test()
    applog.info("background ready", tray=tray_ok, server=rt.server or "-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ноосфера — окно на ноутбуке")
    parser.add_argument("--minimized", action="store_true",
                        help="стартовать свёрнутым в трей (автозапуск)")
    parser.add_argument("--notify-test", action="store_true",
                        help="сразу отправить тестовый тост")
    parser.add_argument("--diagnose", action="store_true",
                        help="показать экран диагностики даже при живом сервере")
    args = parser.parse_args(argv)

    applog.info("app start", argv=" ".join(argv if argv is not None else sys.argv[1:]) or "-",
                pid=os.getpid(), python=sys.version.split()[0])
    tray.set_app_user_model_id()

    si = single_instance.SingleInstance()
    if not si.acquire(wait_s=8.0):
        # либо уже работает, либо старая копия ещё гаснет после «перезапустить»
        applog.info("second-instance", action="signal-first-and-exit")
        si.request_show()
        si.release()
        return 0

    rt = Runtime()
    refresh_runtime(rt)
    if args.diagnose:
        rt.server, rt.token = "", ""
        rt.reason = rt.reason or "принудительная диагностика (--diagnose)"

    hidden = args.minimized and bool(rt.server)
    if rt.server:
        target, page_name = appconfig.INDEX_HTML.as_uri(), "index.html"
        applog.info("ui shown", server=rt.server, hidden=hidden)
    else:
        # при поломке окно обязано быть видимым: молча сидящая в трее диагностика
        # хуже белого окна не будет, а причину владелец увидит сразу
        hidden = False
        target, page_name = appconfig.DIAGNOSE_HTML.as_uri(), "diagnose.html"
        applog.info("diagnose shown", reason=rt.reason)

    window = webview.create_window(
        appconfig.APP_TITLE, html=SPLASH_HTML, js_api=Bridge(rt),
        width=1280, height=860, min_size=(980, 640), hidden=hidden,
        text_select=True, easy_drag=False, background_color="#EDF1EA")
    rt.window = window
    applog.info("window=created", hidden=hidden, page=page_name)

    nav = {"done": False}

    def navigate() -> None:
        """Вторая навигация — уже в прогретой среде WebView2, мост приходит вовремя."""
        if nav["done"]:
            return
        nav["done"] = True
        applog.info("navigate entered", page=page_name)
        attached = cors_bridge.attach(window, rt.server, rt.token)
        applog.info("navigate attach", ok=attached)
        applog.info("splash loaded, navigating", page=page_name)
        window.load_url(target)
        threading.Thread(target=measure_bridge, name="noosphere-bridge", daemon=True).start()
        threading.Timer(2.5, page_check, args=(window, "boot")).start()

    def measure_bridge() -> None:
        """Что реально есть в странице: мост pywebview, шим fetch, внедрённый конфиг.

        Заодно лечит гонку инъекции: как только мост на месте, шим+конфиг
        доустанавливаются evaluate_js прямо в живой документ (идемпотентно).
        """
        started = time.perf_counter()
        probe = ("JSON.stringify({py:!!(window.pywebview&&window.pywebview.api),"
                 "shim:!!window.__noosphereShim,cfg:!!window.__noosphere_cfg})")
        heal = cors_bridge.SHIM_JS + (
            "window.__noosphereShell=1;window.__noosphere_cfg={server:"
            + json.dumps(rt.server or "") + ",token:" + json.dumps(rt.token or "") + "};")
        healed = False
        state: dict = {}
        for _ in range(120):
            try:
                raw = _eval_js(window, probe)
                state = json.loads(raw) if raw else {}
            except Exception:
                state = {}
            if state.get("py") and not healed:
                try:
                    _eval_js(window, heal)
                    healed = True
                    applog.info("shim healed via evaluate_js", page=page_name)
                except Exception as exc:
                    applog.warn("shim heal failed", error=f"{type(exc).__name__}: {exc}")
            if state.get("py") or state.get("shim"):
                applog.info("bridge ready", ms=int((time.perf_counter() - started) * 1000),
                            page=page_name, py=state.get("py"), shim=state.get("shim"),
                            cfg=state.get("cfg"), healed=healed)
                return
            time.sleep(0.1)
        applog.warn("bridge not ready in 12s", page=page_name,
                    py=state.get("py"), shim=state.get("shim"), cfg=state.get("cfg"))

    window.events.loaded += navigate
    threading.Timer(4.0, navigate).start()  # loaded splash не пришёл — всё равно идём дальше

    def closing() -> bool:
        if rt.force_quit:
            return True
        applog.info("window hidden to tray")
        window.hide()
        return False

    window.events.closing += closing
    window.events.shown += lambda: applog.info("window shown")

    icon = str(appconfig.ICON_PATH) if appconfig.ICON_PATH.exists() else None
    # стабильный профиль WebView2: temp-каталог на каждый старт оставлял в %TEMP%
    # десятки EBWebView-папок и давал повод Defender'у тормозить создание
    # окружения на первой записи (виснутые старты 15:50/15:52/15:55 —
    # CreateAsync не завершался, UI-поток в winforms:808)
    webview.start(func=lambda: background(rt, si, args), icon=icon,
                  storage_path=str(appconfig.home_dir() / "webview2"))

    rt.stop.set()
    if rt.tray:
        rt.tray.stop()
    si.release()
    applog.info("app exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
