# Noosphere

Личное рабочее место ИИ-агентов владельца. Учредительный документ: `../docs/FOUNDING.md`
(пакет проекта — `workspace/new`). Стройка по директиве владельца 2026-10-10:
одним махом, без MVP-гейта; неделя-0 (контур реакции, пробы квот, burn-таблица)
встроена в первый код сервера.

## Что внутри (фаза 1)

- `server/telemetry.py` — каждый ход → SQLite (модель, токены, кэш, исход).
- `server/router.py` + `server/pool.py` — лестница тиров (local/base/mid/deep/max),
  эскалация при провале; логика портирована из `workspace/kimi/llm-router/llm.py`.
- `server/antiloop.py` — бюджеты 70/90%, fingerprint-повторы, treading-water (урок Paperclip).
- `server/alerts.py` — контур реакции: алерт → TG → SLA → эскалация («реакция первична»).
- `server/quotas.py` — read-only пробы квот (Kimi usages, ollama, Alibaba = по телеметрии).
- `server/burn.py` — burn-таблица по каналам.
- `server/sessions.py` — серверные сессии: event-sourced turn'ы, снапшоты, форки,
  OpenToolCallCloser, маркер фазы.
- `PROTOCOL.md` — версионируемый контракт клиент↔сервер v1.

Каналы: `alibaba` (главный, Token Plan), `ollama` (бесплатный локальный тир),
`kimi-cli` (подписка Kimi, OAuth с авто-refresh), `zen` (opencode Go API:
mimo/deepseek и др., обязателен заголовок x-opencode-session).

## 1С-контур (v1.7)

Процессы разработки 1С «спека → код → судья» (`server/processes.py`,
`POST /v1/processes`): спека пишет **mimo-v2.6-pro** (zen) с доступом к скилам
и MCP, код — **deepseek-v4.1-flash** (zen), ревью — **Kimi K3** (kimi-cli);
NEEDS_FIXES → повторный круг (до max_fix_iters), дальше `needs_human`.
Каталог скилов с диска (`server/skills.py`, REST /v1/skills, инструменты
skill_list/skill_load, инъекция в system агентских ходов) и минимальный
MCP-клиент (`server/mcp.py`, инструменты mcp_tools/mcp_call; atlas ТМС и
testpilot-ферма). Deploy не исполняется автоматически — только план, исполняет
человек. Подробности — PROTOCOL.md v1.7.

## Запуск

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # заполнить пути к ключам
python scripts/smoke.py              # дым, без сети
uvicorn server.main:app --host 0.0.0.0 --port 8300
```

CLI: `python scripts/quota_probe.py`, `python scripts/burn_report.py [дней]`.

## Прогоны доработки (на сервере)

Прогон доработки исполняет сам сервер модулем `server/devexec.py` в изолированном
worktree `~/workspace/noosphere-dev/<rid>` от `origin/main`; ход идёт собственным
агентским циклом сервиса (`pool.ask` + инструменты, те же гварды: джейл путей,
deny-list команд, бюджеты хода), а оболочка на ноутбуке для этого больше не нужна.
Снятый патч применяет автопатч (`server/autopatch.py`) — smoke-гейт до commit/push,
затем перезапуск сервиса с проверкой `/health` и откатом, если новый код не поднялся.

## Клиент Windows (десктоп-оболочка, 2026-10-11)

Ноутбук — только окно: сервер, инструменты и секреты остаются на Mac mini :8300.

```bash
python client\desktop\install_windows.py    # ярлыки Desktop/Пуск + автозапуск HKCU Run
pythonw client\noosphere_app.pyw            # запуск (--minimized / --notify-test / --diagnose)
python client\desktop\selftest.py           # headless-проверки логики, без GUI
python client\desktop\install_windows.py --uninstall   # откат установки
```

Оболочка (`client\desktop\`): один экземпляр (mutex+событие), трей с меню,
тосты о crit-алертах (кап 5/30 мин), наблюдатель `/v1/alerts`,
экран диагностики `client\web\diagnose.html` вместо белого окна,
журнал `%LOCALAPPDATA%\Noosphere\app.log`. Крестик прячет окно в трей.

Дыра платформы, закрыта 2026-10-12: WebView2 отклоняет cors-fetch из `file://` на
http-сервер (десктопный Edge той же версии пропускает). Данные окна идут через
`desktop/cors_bridge.py`: шим `fetch`, внедрённый на document-start, исполняет запрос
urllib'ом и собирает `Response` внутри страницы; Python трогает только адреса из
`appconfig.servers()`, остальное падает в нативный fetch. Диагноз и доказательства —
в отчёте `workspace\qwen\noosphere-desktop-2026-10-11\report.md`.

## Аудит успешности чатов (schema v12, 2026-10-12)

Финальный статус каждого закрытого чата ставит процесс-судья `server/chat_judge.py`
(ночной scheduled run, класс `audit` → deep, `force_no_escalate`, без инструментов;
цена видна в burn строкой `turn_kind=judge`, кап ≤15 чатов за ночь). Детерминированный
слой признаков — `server/verdicts.py` (TECH/TOOL/COST, переделки, протухшие заявки),
он же proxy-вердикт, когда судья недоступен. Транскрипт для судьи сжимает и маскирует
`server/transcript.py`.

Приёмка владельцем (02.11.2026): золотая метка на чат целиком — `owner_marks`
(`POST /v1/sessions/{id}/mark`, кнопки 👍/👎 в шапке открытого чата, чип «принято»
или «не то» в списке). Метка — факт, а не оценка: она уходит судье уликой
(`[метки владельца]` в транскрипте + `OWNER_MARK_ADDENDUM`) и перевешивает его
вывод — `ok` → success, `notit` → fail; метка, после которой были новые ходы,
золото теряет и остаётся уликой. Запись метки пересчитывает признаки правил и
сразу пересуживает чат в фоне (один вызов судьи), а `_needs_judge` возвращает чат
ночному судье, если метка новее вердикта. До этого `owner_marks` не писал никто
кроме selftest: 0 меток за всё время и 21 partial против 29 success за 30 дней —
судья снижал статус за неподтверждённую приёмку там, где владелец результат взял.

```bash
python scripts/audit_selftest.py        # правила/маска/разбор судьи, 10 проверок
python scripts/audit_judge_dryrun.py    # ночной прогон на фикстурах, pool.ask подменён
python -m scripts.audit_backfill --days 30   # пересчёт признаков на хосте, 0 токенов
```

Дизайн и решения: `workspace\qwen\noosphere-chat-audit-2026-10-12\design.md`
(ночной цикл аудит→оптимизация→утверждение одной строкой — батчи C3–C5).

## Дальше (траектория FOUNDING §14)

Фаза 2 — sessions dogfood + 14 дней владельцем; фаза 3 — Flutter-прототип
(стриминг/офлайн/пуши); фаза 4 — автономность A1/A2 и ночной цикл после
30 ночей с ненулевым bench + рабочий анти-цикл + контур реакции.
<!-- e2e-integration-pipeline 2026-11-01 -->
<!-- e2e-pipeline-full 2026-11-01 -->

## Журнал проверок

- 2026-11-02 сквозная проверка: прогон закрывается только по факту интеграции в origin/main
