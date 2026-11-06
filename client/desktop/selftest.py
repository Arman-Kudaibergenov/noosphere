"""Headless-проверки десктоп-оболочки: без GUI и без правок системы.

Запуск из корня noosphere:
    C:\\Python314\\python.exe client\\desktop\\selftest.py
Проверяет логику, а не оболочку Windows: mutex между ПРОЦЕССАМИ (второй запуск
видит первого), разбор конфига, диффы наблюдателя и кап тостов, обрезку
полезной нагрузки под лимиты Shell_NotifyIconW, решение «UI или диагностика».
Живая проба сервера — информационная: сеть может быть недоступна, это не провал.

Состояние и журнал изолируются во временный каталог через NOOSPHERE_HOME —
боевой seen.json и app.log самопроверка не трогает.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # client/

from desktop import appconfig, applog, cors_bridge, diagnostics, httpapi, make_icon  # noqa: E402
from desktop import notify, single_instance, watcher  # noqa: E402

CHILD = """
import sys
sys.path.insert(0, r"{client}")
from desktop.single_instance import SingleInstance
si = SingleInstance(r"{mutex}", r"{event}")
print("second" if not si.acquire() else "first")
"""


class Recorder:
    """Регистратор «отправленных» тостов: rc всегда 1, вызовы копятся.

    Приёмник в самопроверке — простая функция, поэтому наблюдатель отдаёт ему
    Notice, а не payload; приводим к payload здесь, заодно проверяя обрезку.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, item) -> int:
        payload = notify.build_payload(item) if isinstance(item, notify.Notice) else dict(item)
        self.calls.append(payload)
        return 1


class FakeFetch:
    """Подмена HTTP: отвечает фикстурами по пути."""

    def __init__(self, alerts: list[dict]) -> None:
        self.alerts = alerts

    def __call__(self, server: str, path: str, token: str) -> httpapi.Reply:
        if path.startswith("/v1/alerts"):
            return httpapi.Reply(True, 200, list(self.alerts))
        return httpapi.Reply(False, 404, None, "нет такого пути")


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"CHECK {name} {detail} ok={ok}")
    return ok


def check_mutex() -> bool:
    mutex = "Local\\NoosphereSelftest.Mutex"
    event = "Local\\NoosphereSelftest.Event"
    si = single_instance.SingleInstance(mutex, event)
    first = si.acquire()
    child = subprocess.run(
        [sys.executable, "-c", CHILD.format(client=str(appconfig.CLIENT_DIR),
                                            mutex=mutex, event=event)],
        capture_output=True, text=True, timeout=30)
    second = child.stdout.strip()

    seen = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(target=si.watch_show, args=(seen.set, stop), daemon=True)
    thread.start()
    time.sleep(0.3)
    si.request_show()
    shown = seen.wait(3.0)
    stop.set()
    thread.join(timeout=2.0)
    si.release()
    return check("mutex", first and second == "second" and shown,
                 f"first={first} child={second or 'no-output'} show_event={shown}")


def check_config() -> bool:
    defaults = appconfig.servers() == appconfig.DEFAULT_SERVERS
    os.environ["NOOSPHERE_SERVERS"] = "http://127.0.0.1:9, http://127.0.0.1:10/"
    try:
        override = appconfig.servers() == ("http://127.0.0.1:9", "http://127.0.0.1:10")
    finally:
        del os.environ["NOOSPHERE_SERVERS"]
    token = appconfig.read_token()
    labels = (httpapi.label_for("http://192.168.0.235:8300") == "LAN"
              and httpapi.label_for("http://100.95.239.47:8300") == "tailnet")
    return check("config", defaults and override and labels,
                 f"servers={len(appconfig.DEFAULT_SERVERS)} override={override} "
                 f"token={'present' if token else 'missing'} len={len(token)} labels={labels}")


def check_watcher(tmp: Path) -> bool:
    alerts = [{"id": 1, "severity": "crit", "kind": "quota", "text": "квота 91%"},
              {"id": 2, "severity": "warning", "kind": "route", "text": "не то"}]
    fetch = FakeFetch(alerts)
    rec = Recorder()
    state = tmp / "seen.json"
    w = watcher.Watcher(lambda: ("http://stub:8300", "tok"), rec, seen_path=state,
                        fetch=fetch)
    first = w.poll_once()
    # первый запуск: история не вываливается, только сводка
    first_ok = first["first_run"] and len(rec.calls) == 1 and rec.calls[0]["kind"] == "summary"

    fetch.alerts = alerts + [{"id": 3, "severity": "crit", "kind": "sla", "text": "crit не доставлен"}]
    second = w.poll_once()
    second_ok = (second["alerts_new"] == 1
                 and [c["kind"] for c in rec.calls[1:]] == ["crit"])

    third = w.poll_once()
    third_ok = third["alerts_new"] == 0 and len(rec.calls) == 2

    # обрыв связи: уведомление на переход, а не на каждый опрос
    broken = _poll_with_error(w)
    recovered = _poll_with_error(w, fail=False)
    transport_ok = broken.count("sent") == 1 and recovered.count("sent") == 1

    ok = first_ok and second_ok and third_ok and transport_ok
    return check("watcher", ok,
                 f"first_run_summary={first_ok} new={second['alerts_new']} "
                 f"repeat={third['alerts_new']} transport={transport_ok}")


def _poll_with_error(w: watcher.Watcher, fail: bool = True) -> list[str]:
    original = w._fetch  # noqa: SLF001 — самопроверка, подмена намеренная

    def failing(server, path, token):
        if fail and path.startswith("/v1/alerts"):
            return httpapi.Reply(False, 0, None, "сеть: отказ")
        return original(server, path, token)

    w._fetch = failing  # noqa: SLF001
    try:
        return [o for o in w.poll_once()["outcomes"] if o]
    finally:
        w._fetch = original  # noqa: SLF001


def check_cap() -> bool:
    clock = {"now": 1000.0}
    rec = Recorder()
    cap = notify.Cap(window=1800.0, limit=5)
    disp = notify.Dispatcher(rec, clock=lambda: clock["now"], cap=cap)
    outcomes = [disp.push(notify.Notice("crit", f"t{i}", f"текст {i}")) for i in range(7)]
    sent = outcomes.count("sent")
    summary = outcomes.count("summary")
    suppressed = outcomes.count("suppressed")
    clock["now"] += 1801.0
    reopen = disp.push(notify.Notice("crit", "после окна", "окно закрылось"))
    ok = sent == 5 and summary == 1 and suppressed == 1 and reopen == "sent"
    return check("cap", ok, f"sent={sent} summary={summary} suppressed={suppressed} "
                            f"reopen={reopen} toasts={len(rec.calls)}")


def check_payload() -> bool:
    long_text = "слово " * 80
    long_title = "заголовок " * 12
    payload = notify.build_payload(notify.Notice("crit", long_title, long_text, "alerts"))
    ok = (len(payload["title"]) <= 63 and len(payload["text"]) <= 255
          and payload["flags"] == notify.NIIF_ERROR and payload["action"] == "alerts"
          and payload["title"].endswith("…"))
    return check("payload", ok, f"title_len={len(payload['title'])} "
                                f"text_len={len(payload['text'])} flags={payload['flags']} "
                                f"action={payload['action']}")


def check_diagnostics() -> bool:
    os.environ["NOOSPHERE_SERVERS"] = "http://127.0.0.1:9"
    try:
        checks, alive = diagnostics.run_checks(timeout=0.5)
        decision, reason = diagnostics.decide(checks, alive)
        dead_ok = decision == "diagnose" and alive is None

        live_checks = [diagnostics.Check("Сервер · LAN", True, "", kind="server"),
                       diagnostics.Check("Токен", True, "", kind="token"),
                       diagnostics.Check("Файлы", True, "", kind="files")]
        probe = httpapi.Probe(url="http://192.168.0.235:8300", label="LAN", ok=True)
        ui_decision, ui_server = diagnostics.decide(live_checks, probe)
        ui_ok = ui_decision == "ui" and ui_server == "http://192.168.0.235:8300"

        no_token = [diagnostics.Check("Сервер · LAN", True, "", kind="server"),
                    diagnostics.Check("Токен", False, "", kind="token"),
                    diagnostics.Check("Файлы", True, "", kind="files")]
        token_decision, token_reason = diagnostics.decide(no_token, probe)
        token_ok = token_decision == "diagnose" and token_reason == "нет токена"
    finally:
        del os.environ["NOOSPHERE_SERVERS"]
    return check("diagnostics", dead_ok and ui_ok and token_ok,
                 f"dead={decision}/{reason} ui={ui_decision} no_token={token_decision}")


def check_icon() -> bool:
    path = appconfig.ICON_PATH
    if not path.exists():
        make_icon.build(path)
    blob = path.read_bytes()
    frames = int.from_bytes(blob[4:6], "little") if blob[:4] == b"\x00\x00\x01\x00" else 0
    return check("icon", frames == 4, f"frames={frames} bytes={len(blob)}")


def check_compile() -> bool:
    root = appconfig.DESKTOP_DIR
    files = sorted(root.glob("*.py")) + [appconfig.ENTRY_PYW]
    bad: list[str] = []
    for item in files:
        try:
            compile(item.read_text(encoding="utf-8"), str(item), "exec")
        except SyntaxError as exc:
            bad.append(f"{item.name}:{exc.lineno}")
    html_ok = appconfig.DIAGNOSE_HTML.exists() and appconfig.INDEX_HTML.exists()
    return check("compile", not bad and html_ok,
                 f"modules={len(files)} bad={','.join(bad) or 'none'} html={html_ok}")


def check_corsbridge() -> bool:
    """Граница моста держит чужой origin; свой исполняется (сеть — инфо-часть)."""
    out = cors_bridge.selfcheck()
    return check("corsbridge", out["foreign_blocked"],
                 f"foreign_blocked={out['foreign_blocked']} own_ok={out['own_ok']} "
                 f"own_status={out['own_status']} shim_bytes={out['shim_bytes']}")


def check_live() -> bool:
    """Информационная проба боевого сервера: сеть может быть недоступна."""
    probe = httpapi.probe(appconfig.DEFAULT_SERVERS[0], timeout=2.0)
    print(f"INFO live server={probe.url} ok={probe.ok} ms={probe.ms} "
          f"version={probe.version} error={probe.error}")
    return True


def main() -> int:
    os.environ["NOOSPHERE_HOME"] = tempfile.mkdtemp(prefix="noosphere-selftest-")
    tmp = Path(os.environ["NOOSPHERE_HOME"])
    applog.info("selftest started", home=str(tmp))

    results = [
        check_mutex(),
        check_config(),
        check_watcher(tmp),
        check_cap(),
        check_payload(),
        check_diagnostics(),
        check_icon(),
        check_compile(),
        check_corsbridge(),
        check_live(),
    ]
    passed = sum(results)
    total = len(results)
    if all(results):
        print(f"SELFTEST PASS ({passed}/{total})")
        return 0
    print(f"SELFTEST FAIL ({passed}/{total})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
