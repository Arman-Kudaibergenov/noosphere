# PROTOCOL.md — контракт клиент↔сервер Noosphere, v1.7

Версионируемый контракт: клиенты (Flutter Windows/Android) и сервер могут
менять язык/стек, протокол остаётся. REST для действий, WS для живого хода.
Авторизация: `Authorization: Bearer <token>` (server.token_file >
server.token > env NOOSPHERE_TOKEN; токен лежит в ~/.secrets/noosphere.token
вне дерева деплоя). Пустой токен + non-loopback host → сервер НЕ стартует
(fail-closed, G1.4). На /v1/* действует per-IP rate-limit (токен-бакет,
server.rate_limit {enabled, rpm, burst}, превышение → 429). CORS сужен до
localhost/LAN/tailnet + null-origin (file:// у pywebview).
Все времена — UTC ISO 8601 (`...Z`); дневные бакеты — локальная дата UTC+4.

Открытый хвост (сознательно, решение владельца 11.10 + аудит r3): TLS на :8300
НЕ поднят — Bearer идёт по домашней LAN открытым текстом; компенсации —
fail-closed auth, rate-limit, джейлы. Если LAN перестанет
считаться доверенной — этот пункт пересматривается первым.

## REST

### GET /
Существующий `client/web/index.html` в браузере (в том числе телефоне), без
отдельного приложения. Адрес API по умолчанию равен origin открытой страницы.
При `server.lan_no_auth: true` LAN/tailnet работают без токена; внешние клиенты
получают 401 без Bearer. Токен в URL не используем, чтобы не оставлять его в
access-log. На экране ≤640px лента и композер занимают всю ширину, список
сессий открывается кнопкой ☰.

### GET /health
→ `{ "status": "ok", "version": "0.1.0", "time": "<utc>" }`

### POST /v1/chat
Единая точка диалога. Создаёт или продолжает серверную сессию.
Память (v1.2): в модель уходит история сессии — done-ходы как сообщения
user/assistant (последние 20 ходов, ≤12000 символов, старые отбрасываются
целиком; провалившийся ход — с пометкой об ошибке), затем текущий prompt.
```json
{
  "session_id": "uuid | null",
  "user": "arman",
  "task_class": "chat | code | extract | ... (см. router.CLASS_TIER)",
  "prompt": "текст",
  "system": "опц.",
  "tier": "опц. форс-тир: local|base|mid|deep|max",
  "max_tokens": 1024,
  "require_json": false,
  "min_chars": 0,
  "must": "опц. обязательная подстрока",
  "steer": false,
  "attachments": ["upload_id"],
  "agent": null,
  "max_tool_iters": 6
}
```
`agent` (v1.2, изменено 2026-10-17): агентский цикл с инструментами
(`shell_exec`, `read_file`, `write_file`, `web_fetch` — см. tools.py; отключается
конфигом `tools.enabled` / `tools.shell`). `null`/ключ опущен → конфиг
`tools.default_agent` (дефолт true — действие по умолчанию, решение владельца
после жалоб на ответы «я не могу»); явный `false` — ход без инструментов
(планировщик, headless). Ответ с tool_calls исполняется на
сервере, результат уходит обратно в модель, до max_tool_iters итераций на ход
(потолок → `ok:false`, err «потолок агентских итераций»). Каждая итерация —
отдельная запись в телеметрии (why: tool_calls). Если system не задан,
подставляется агентский системный промпт по умолчанию.
→
```json
{
  "session_id": "uuid",
  "turn_no": 3,
  "ok": true,
  "text": "ответ",
  "model": "deepseek-v4.1-flash",
  "tier": "base",
  "trace": [{"tier": "base", "model": "...", "ok": true, "why": ""}],
  "escalated": false,
  "usage": {"prompt_tokens": 0, "completion_tokens": 0},
  "ms": 812,
  "err": ""
}
```
Ошибки маршрутизации — HTTP 200 с `ok:false` и `err` (тело — контракт, не код):
`"нет доступного тира"`, `"дневной бюджет канала исчерпан"`, `"treading-water: стоп"`.

### WS /v1/ws?token=...
Один WS = одна сессия. Клиент шлёт те же JSON, что POST /v1/chat.
Сервер стримит события:
```json
{"type": "turn_start", "turn_no": 1, "tier": "base", "model": "..."}
{"type": "delta", "text": "..."}            // если канал умеет стримить
{"type": "turn_end", "ok": true, "text": "...", "usage": {...}, "ms": 0}
{"type": "error", "err": "..."}
```

### GET /v1/sessions  → `[{id, user, channel, status, phase, updated_at}]`
### GET /v1/sessions/{id} → сессия + ходы (turn_no, status, tokens_spent)
### POST /v1/sessions/{id}/resume → реанкоринг abandoned-сессии (новый turn от снапшота)

### GET /v1/quota → последние пробы квот по каналам
```json
[{"channel": "kimi", "ok": true, "remaining": 0.62, "unit": "percent", "window": "day", "ts": "..."}]
```
`remaining: null` = неизвестно (НЕ оценка — дисциплина данных).

### POST /v1/quota/probe → запустить пробы сейчас (read-only)

### GET /v1/burn?days=1 → burn-таблица по каналам
```json
[{"channel": "alibaba", "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
  "reasoning_tokens": 0, "cache_read_tokens": 0, "escalations": 0,
  "failures": 0, "filter_burned_tokens": 0, "total_ms": 0}]
```

### GET /v1/alerts?status=new → алерты контура реакции
### POST /v1/alerts/{id}/ack → подтвердить реакцию (останавливает SLA-эскалацию)

## Контракт сессий (требования из разбора TrueForge, FOUNDING §9)
- Event-sourced: запись `session_turns` durable ДО мутации in-memory.
- Снапшот контекста на уровне turn'а; восстановление = чтение снапшота.
- Форки через `previous_turn_id`; first-terminal-write-wins.
- Перед отправкой истории в LLM — заглушки-ошибки на незавершённые tool_calls
  (OpenToolCallCloser), иначе API отвергнет историю.
- При остановке сервера активные turn'ы → `abandoned`, токены сохранены;
  продолжение = новый turn от снапшота.

---

# v1.1 (2026-10-10) — очередь, статусы, поиск, вложения, планировщик

## Очередь и steer (батч BioClaude-1)
`POST /v1/chat` тело дополнено: `steer: bool`, `attachments: [upload_id]`.
Если по сессии идёт ход — ответ `{"queued": true, "position": N, "session_id": ...}`
(steer-записи идут раньше обычных в очереди сессии). Иначе — как в v1.
- `GET /v1/queue?session_id=` → `[{id, session_id, kind: queue|steer|revive, status, prompt, ts}]`
- `POST /v1/queue/{id}/cancel` → `{ok}`
- Автовозобновление: при старте сервера сессии `abandoned` моложе 24ч с
  неотвеченным последним prompt → встают в очередь `kind=revive` (один раз).

## Статусы сессий (язык точек)
`GET /v1/sessions` строка дополнена: `state: busy|ready|error|idle`, `queue_len: int`.
busy = идёт ход или есть queued; error = последний ход упал («ждёт тебя»);
ready = active, последний ход ok; idle — остальное.

## Поиск
`GET /v1/search?q=текст&limit=20` →
`[{session_id, turn_id, ts, snippet}]` (FTS5 по prompt+text ходов, snippet с `<b>`).

## Вложения (чанки, НЕ base64 в теле сообщения)
- `POST /v1/uploads/init` `{name, size, mime, session_id?}` → `{upload_id}`
- `PUT /v1/uploads/{id}/chunk?offset=N` (сырые байты) → `{received}`
- `POST /v1/uploads/{id}/done` → `{ok, size}`
- `GET /v1/uploads/{id}` → файл (Content-Type из mime, inline)
Привязка к сообщению: `attachments: [upload_id]` в `/v1/chat`; в снапшоте хода —
список `{upload_id, name, mime}`.

## Хранилище доступов (vault, schema v6)

Единый ответ на «где лежат доступы»: ssh, MCP, токены, логины, сервисы, скилы.
Поиск — только метаданные (без secret); секрет — по явному vault_get reveal=true
и только для действия. Канон: значения в ~/.secrets, здесь — указатели (secret_file).
```
GET    /v1/vault?q=...      → поиск/последние (метаданные)
POST   /v1/vault {kind, name, host?, login?, secret?, secret_file?, notes?, tags?} → 201 (upsert kind+name)
DELETE /v1/vault/{id}
```
Инструменты агента: vault_search, vault_get (reveal). vault_save модели НЕ
доступен (с аудита r2) — записи меняет только человек через REST.
Первичное наполнение картой контура: scripts/seed_vault.py (идемпотентно).

## Правила и память рабочего места (батч 4)

Правила — редактируемые блоки, подставляемые в system-промпт каждого хода
(перед пользовательским system; включённых нет — system не меняется):
```
GET    /v1/rules            → список
POST   /v1/rules {name, text, ord?}   → 201 {id}
POST   /v1/rules/{id}/toggle
DELETE /v1/rules/{id}
```
Память — заметки между сессиями (FTS5): агент сохраняет/ищет инструментами
`memory_save` / `memory_search`; `GET /v1/memory?limit=20` — последние записи.

## Пул аккаунтов (батч 4)

`channels.<name>.accounts` — несколько ключей канала (см. config.example.yaml).
Липкость: текущий аккаунт в meta и не меняется без отказа (переезд = потеря
prompt-кэша). При auth/rate-limit: аккаунт на cooldown 10 мин, ретрай того же
вызова со следующим (запись `rotate_account` в trace); ротировать некуда —
честная пометка в trace, дальше обычная логика сбоев.

## Прогоны (runs, батч 3, v1.2)

Фоновый разовый агентский прогон: стартовал и ушёл, статус смотришь потом.
```
POST /v1/runs {prompt, session_id?, task_class?, tier?, agent=true, max_tool_iters?}
  → 201 {run_id, session_id}
GET  /v1/runs          → последние 50 (status, created/finished, result_preview)
GET  /v1/runs/{id}     → полная запись (result_text, err)
POST /v1/runs/{id}/cancel
```
Статусы: queued → running → done | error | canceled. Результат — ход в своей
сессии: виден в клиенте, индексируется поиском. Прогон ждёт, пока сессия
освободится (общий BUSY с диалогом).

## Терминалы (батч 3; PTY и интерактив 16.10.2026)

Инструмент `terminal_exec` — постоянный bash на сервере: cd/export/переменные
сохраняются между вызовами (в отличие от безстейтного shell_exec). На macOS shell
запускается через `/usr/bin/script` с PTY, поэтому поддерживаются интерактивные
программы. Терминал по умолчанию привязан к сессии (`terminal_id` опционален).

На один вызов — одна операция:
- `command` — запустить команду; `timeout` ограничивает только ожидание ответа,
  по его истечении процесс продолжается, а не убивается;
- `input` — stdin активной команды, `enter:true` добавляет Enter; спецклавиши
  передаются JSON control chars (Ctrl-C/Ctrl-D);
- `poll:true` — прочитать новый вывод без ввода;
- `close:true` — завершить терминал и его процессы.

Возвращаемый статус `[процесс продолжается: ...]` означает, что нужны
`input`/`poll`/`close`, а не повторный запуск команды. Процессы умирают вместе с
сервером и при cancel рана. Для `input` в `tool_audit` пишется только длина —
содержимое (в том числе возможный пароль) в журнал не попадает.

## Планировщик (headless-прогоны)
- `GET /v1/schedules` → `[{id, name, prompt, task_class, tier, interval_minutes,
  daily_time, enabled, last_run, next_run, last_status, session_id}]`
- `POST /v1/schedules` `{name, prompt, task_class?, tier?, interval_minutes?,
  daily_time?: "HH:MM", session_id?}` → `{id}` (нужен interval ИЛИ daily_time)
- `POST /v1/schedules/{id}/toggle` → `{enabled}`; `DELETE /v1/schedules/{id}` → `{ok}`
Исполнение: в целевую сессию (или новую на прогон) пишется ход с prompt; результат —
сообщением агента; провал → warning в контур реакции.

---

# v1.3 (батч 6) — единая точка входа: route-классификатор + проекты

Запрос сначала идёт в классификатор; чат/ран клиент стартует сам по `kind`
(`/v1/chat` или `/v1/runs` не менялись). Сессию `/v1/route` НЕ создаёт.

### POST /v1/route
```json
{"prompt": "текст", "session_id": "uuid | null"}
→ {
  "log_id": 12, "kind": "chat | agent | run",
  "project": {"id": 3, "name": "Экспо", "auto": 0} | null,
  "title": "заголовок сессии", "confidence": 0.87, "prefilter": false,
  "threshold": 0.7, "model": "...", "tier": "base", "ms": 812, "err": ""
}
```
Дефолт — run; chat — только по явным маркерам («обсудим», «что думаешь»…).
Порядок: chat-regex → run-regex → ключевые слова проекта (бесплатно) → LLM
(базовый тир, JSON, без эскалации) → при смерти каналов fallback run с `err`.
`confidence < threshold` — клиент показывает кнопки выбора, автовыбора нет.
Каждая классификация — строка `route_log` (модель/тир/токены/латентность).

### POST /v1/route/{log_id}/override  `{kind?, project_id?}` → `{ok}`
Правка пользователя — самый ценный сигнал ночного анализа; уже отправленное
сообщение НЕ переотправляется.

### Проекты (саморастущие)
```
GET  /v1/projects → [{id, name, keywords, auto, sessions_count}]
POST /v1/projects {name, keywords} → 201 (существующее имя — добивает keywords)
POST /v1/projects/{id}/rename {name}  — папки УЖЕ разложенных сессий не трогает
```
Классификатор может создать проект сам (`auto=1`). Сид в коде нейтральный
(Ноосфера, Личное); контуры владельца добавляются конфигом `projects.seed`
(см. config.example.yaml). Папка новой сессии = имя проекта (клиент ставит
`/v1/sessions/{sid}/folder` после первого ответа).

### Ночной анализатор
Раз в сутки (час `route.analysis_hour`, локальное) разбирает `route_log` и
качество ранов (err/cancel/rework% по проектам, медианы стоимости и времени,
доля эскалаций) и шлёт дайджест владельцу (alert kind=route_analysis,
warning). Предложения по экономии — только с предохранителем качества:
нижний тир кандидат, лишь если по факту держит success-rate не хуже.
Авто-применения нет.

---

# v1.4 (аудит r3, G1–G10) — граница агент/контур, честные журналы

## Безопасность исполняющей поверхности (G1)
- **Джейл путей**: write_file — только `~/workspace` и `/tmp`; read_file —
  `~/workspace`, `/tmp` + `tools.read_allow` из конфига; `~/noosphere` (код+БД),
  `~/.ssh`, `~/.secrets`, `~/.kimi-code`, `~/.netrc`, `~/.aws`, `~/.docker`,
  `~/.config`, `~/.git-credentials` — deny. Фолбэка «весь $HOME» больше нет.
- **Shell-guard путей** (эвристика, задокументированная): shell/terminal_exec
  отклоняет команды, упоминающие deny-пути (`~/.secrets`, `~/.ssh`,
  `noosphere.db`, `noosphere/config.yaml`, `/etc/`, `/private/etc/`,
  `bc-personal-data`), и исполнение скриптов из каталогов записи
  (`bash|sh|python /tmp/…`, `./x.sh`).
- **Класс A3 исполняется напрямую**: `ssh`, `git push`, внешние (не LAN/tailnet)
  POST/PUT/DELETE/PATCH через curl/wget/Invoke-* идут на исполнение как обычные
  команды. Механизм заявок approvals (`requires_approval`, `/v1/approvals`,
  `tools.approval_allow`) УДАЛЁН решением владельца 2026-10-17 («механизм
  заявок неудобен»). Deny-list команд и shell-guard путей выше — без изменений.
- **vault.secret зашифрован at-rest** (Fernet; ключ `~/.secrets/noosphere-vault.key`,
  генерируется при первом старте, НЕ в репо). Ленивая миграция: открытый текст
  шифруется при первом чтении. Reveal'нутые значения маскируются `***` в
  снапшоте хода до записи (в БД/FTS секрет не оседает).
  Публичная выдача vault (list/search) — БЕЗ secret, notes и secret_file
  (F11.3); notes/secret_file — только в vault_get по явному запросу.
  REST-мутации vault (POST/DELETE /v1/vault) журналируются в tool_audit
  (tool="vault_rest", args_summary=kind+name без секретов, rc=HTTP-статус,
  отказы валидации/404 — note=refuse) — F6-хвост закрыт.
- **tool_audit** (G9): колонки `turn_no`, `rc`, `args_summary` (команда/путь/URL
  обрезаны до 200 симв, токены/пароли замаскированы; для vault_get — факт
  reveal и id, без значения). Отказы ВСЕХ видов помечены note=refuse.
  Провал записи журнала → crit-алерт (не молчит).
  Ротация: старше 30 дней чистится тикером (вместе с route_log и approvals).

## Контур реакции (G2)
- `check_sla` работает по crit в статусах `new`/`sent`/`escalated`:
  недоставленный crit (new + delivery_error) ретраится с backoff 1→5→15 мин,
  кап 6 попыток → crit «транспорт алертов сломан» альтернативным путём
  (лог-файл `data/alerts-fallback.log` + stderr, не сломанным транспортом).
- `flush_digest`: ошибка доставки НЕ помечает алерты digested — crit +
  счётчик сбоев (`meta` ключ digest_failures).
- `GET /v1/alerts` без параметра — активные (new+sent+escalated);
  `?status=all` — всё; `?status=<s>` — точный фильтр (совместимость).

## Роутер (G3)
- Префильтр: сначала RUN-маркеры, потом CHAT («ран приоритетнее»; чистые
  вопросы run-регекс не матчит — явный chat-маркер по-прежнему побеждает).
- `force_no_escalate` обрывает лестницу и при ПРОПУСКЕ тира (лимит/бюджет/
  выключен) — классификатор не уезжает на mid/deep при исчерпании base.
- `GET /v1/route_log?limit=50&day=YYYY-MM-DD` — журнал классификаций;
  ротация 30 дней. Строка route_log пишется ДО возможного падения (при
  ошибке создания проекта — с err).
- Авто-проекты: `route.auto_projects` (default true) и потолок
  `route.auto_projects_max` (default 50); сверх потолка — проект «Личное» +
  warning-алерт раз в сутки. IntegrityError при параллельном создании —
  повторный поиск, не 500.
- `GET /v1/burn` строки дополнены: `by_class` (разрез task_class внутри
  канала — виден расход классификатора и stt) и `tier_calls` (счётчики tier:*).

## Приёмка (G4)
- «Ход владельца» = turn с `turn_kind IN ('inline','ws')` И `user=<owner_user>`
  (логин владельца из конфига, ключ `owner_user`, дефолт arman)
  (schema v10, колонка turns.turn_kind; runs/scheduler/revive/queue/external
  не засчитываются; строки до v10 без вида не засчитываются).
- Старт — по-прежнему только POST /v1/acceptance/start; история — таблица
  `acceptance_events` (start|reset, actor), повторный старт = reset+start
  (старое meta-значение мигрирует первой строкой). Подтверждение в клиенте —
  на фронте.

## STT (P2-3)
`POST /v1/stt`: размер проверяется по Content-Length ДО буферизации (>25 МБ →
413), обязательна сигнатура RIFF/WAVE или EBML (иначе 400), транскрипт
обрезается до 2000 символов, каждая проба пишется в telemetry
(task_class="stt", channel="stt") — видна в burn.

## Прочее (G10)
- `tokens_spent` хода — сумма usage по ВСЕМ итерациям агентского цикла
  (раньше — только последняя).
- FTS-backfill при старте индексирует и ходы без prompt (по text снапшота).
- Ингест чужих леджеров: dedupe-ключ source+sha256(строки) (таблица
  `ingest_seen`) поверх meta-офсетов — повтор при ротации источника не дублирует.

## Деплой и восстановление (G6)
CI: smoke → deploy с откатом: сохраняется prev SHA → pull → pip install из
запиненного requirements.txt → новый код на временном :8301 (loopback) →
health-опрос до 24с → при ok глушится старый :8300 и стартует новый (host/port
из config.yaml, CI не форсирует --host) → health :8300; при провале любого
шага — `git reset --hard <prev>` + рестарт старого + job fail.
Keepalive (~/bin/noosphere-start.sh, cron) временный :8301 не трогает.

Restore БД из резервной копии (`data/noosphere.db.bak-v<N>` — создаётся через
sqlite backup API перед каждой миграцией схемы, т.е. консистентен даже при
живом WAL):
```
~/bin/noosphere-start.sh НЕ запускать; pkill -f "uvicorn server.main"
cp ~/noosphere/data/noosphere.db.bak-v<N> ~/noosphere/data/noosphere.db
rm -f ~/noosphere/data/noosphere.db-wal ~/noosphere/data/noosphere.db-shm
# старт как обычно (keepalive поднимет сам в течение минуты)
```
Нюанс отката кода ПОСЛЕ миграции схемы: старый код откажется стартовать с
новой БД (schema_version БД > кода) — восстанови .bak-v<N> соответствующей
версии вместе с откатом кода.

---

# v1.5 — читабельные заголовки сессий

`sessions.title` (schema v11, аддитивный ALTER). Приоритет источника:
1. **Явный title от клиента** — опц. поле `title` в `/v1/chat` и `/v1/runs`
   (клиент передаёт `title` из ответа `/v1/route`; доверенный текст ≤120 зн,
   sanitize: управляющие → пробел, пробелы схлопываются). Пишется только в
   пустой title — не перекрывает ручной.
2. **Генерация после первого хода** (turn_no==1, fire-and-forget, ответ хода
   не ждёт): ход успешен → pool.ask(task_class="route", tier=base,
   max_tokens=30, force_no_escalate) «назови диалог 3-6 словами по-русски»;
   ход упал или LLM недоступна → эвристика: первые ~60 символов первого
   промпта с обрезкой по слову. Ошибки генерации — только stderr.
3. **Бэкфилл при старте** (lifespan, без LLM, идемпотентно): сессии без
   title → эвристика от первого user-промпта (snapshot первого хода).

`GET /v1/sessions` и `GET /v1/sessions/{id}` отдают `title`.
`POST /v1/sessions/{sid}/rename {title}` — ручное переименование (≤120 зн,
sanitize; 400 на пустой, 404 на чужой id). Rename перекрывает любой источник.

---

# v1.6 — вложения доходят до модели, ран виден исходному чату

## Вложения в ходе (дефект «скриншот не дошёл»)
`_run_turn` собирает вложения через `uploads.build_attachment_parts`:
- **текстовые** (text/* или расширение txt/md/csv/json/log/yaml/xml/py/js/ts/
  html/css/sh/sql/ini/cfg/toml) — инлайн в промпт блоком
  «Вложение <имя>:\n<содержимое ≤50 КБ, дальше […обрезано]>»;
- **изображения** (png/jpeg/webp/gif) — мультимодальный content-массив
  `[{type:"text"}, {type:"image_url", image_url:{url:"data:<mime>;base64,…"}}]`;
  лимиты: ≤3 картинки на ход, файл ≤4 МБ (ресайза нет — отказ пометкой),
  суммарный base64 ≤10 МБ;
- **остальные бинарные** — как раньше: имя + тип + размер скобкой в промпте.

Ход с картинками идёт НЕ по лестнице тиров, а на первый включённый канал с
`channels.<name>.vision: true` (модель — `vision_model`; бюджет канала
проверяется, вызов пишется в telemetry с tier_used="vision"). В smoke-конфиге
vision выключен. Vision не настроен → честный fallback: ход идёт текстом с
пометкой в промпте, ответ/ошибка хода префиксуется «Изображения не дошли до
модели: vision-канал не настроен» + warning-алерт vision_missing.

## Ран виден исходному чату (дефект «что за ран?»)
`POST /v1/runs` с `session_id` (origin) пишет в origin-сессию служебный
done-ход со snapshot `{kind:"system-note", prompt:"", text:"Запустил фоновый
прогон: «…» (run N, сессия …). Результат появится в нём в списке сессий."}`.
`get_history` включает system-note как assistant-реплику (осознанное расширение
фильтра prompt-only) — модель помнит про запущенное. Завершение рана в
origin НЕ пишется — это работа контура реакции (алерты).

---

# v1.7 — 1С-контур: скилы, MCP, процессы «спека → код → судья»

## Канал zen (opencode Go API)
`channels.<name>.kind: zen` — OpenAI-compatible `/chat/completions` + обязательный
заголовок `x-opencode-session` (стабильный uuid4 на процесс сервера, иначе 400
MissingSessionID; pool.py). Проба квот — `GET {base_url}/models` (живость; usage
неизвестен → remaining=null, честно). Живые модели (проба 2026-10-13):
mimo-v2.6-pro, deepseek-v4.1-flash (обе принимают reasoning_effort).

Канал kimi-cli: base_url обязан включать `/v1` (`https://api.kimi.com/coding/v1`),
модель судьи — `k3`. OAuth-токен живёт ~15 минут (expires_in=900): accounts.py
обновляет его сам через `POST https://auth.kimi.com/api/oauth/token`
(grant_type=refresh_token, публичный client_id kimi CLI) и переписывает
credentials-файл — «гаснущий резерв» больше не гаснет, пока жив refresh_token.

## Скилы (skills.py)
Каталог `skills.dir` (дефолт `~/.agents/skills`): скан `*/SKILL.md`, имя и первая
строка description из YAML-frontmatter, in-memory кэш с mtime-инвалидацией.
Флаги enabled — в SQLite (`skill_state`, дефолт enabled).
```
GET  /v1/skills                 → [{name, desc, enabled}]
POST /v1/skills/{name}/toggle   → {ok, enabled} (404 на чужое имя)
```
Инструменты агента: `skill_list(pattern?)` (каталог, cap 6К) и
`skill_load(name)` (тело SKILL.md, cap ~12К) доступны всегда. В system после rules
каталог по паттернам `skills.auto_include` попадает только при явной 1С-теме
prompt (маркеры 1С/BSL/CFE/EDT/СКД/БСП/ЗУП/ТМС и др.); не-1С ходы не несут
сотню описаний. `auto_block()` без prompt сохранён как legacy-режим.

## MCP (mcp.py)
Минимальный streamable-HTTP клиент: initialize (protocolVersion 2025-03-26) →
Mcp-Session-Id из заголовка → notifications/initialized → tools/list / tools/call.
Ответ парсится и как JSON, и как SSE (`text/event-stream`, склейка data-строк).
Сессия открывается заново на каждый вызов (простота > экономии, LAN ~10 мс).
Конфиг `mcp.servers.<name>: {url, desc, enabled}`. Инструменты агента:
`mcp_tools(server)` (кэш TTL 10 мин, описания ≤300 зн) и
`mcp_call(server, tool, arguments_json)` (текстовые content-блоки склеены,
cap ~8К). Рабочие серверы: atlas_tms `192.168.3.136:8014/mcp`,
testpilot_farm `192.168.0.31:6004/mcp`, rlm `192.168.0.20:8250/mcp`,
run_orchestrator `192.168.0.20:8251/mcp`, bsl_zhetysu `192.168.0.31:8105/mcp`.

Единый маршрут к контурам: `vault_search` → по записи `terminal_exec`
(SSH/CLI/HTTP) или `mcp_tools/mcp_call`. Отдельный инструмент на каждый контур
не требуется; любые SSH и внешние write-запросы исполняются напрямую
(заявки approvals удалены решением владельца 2026-10-17).

## Процессы разработки 1С (processes.py, schema v19)
Фоновый процесс «спека → код → судья» с жёсткой триадой ролей (каждая стадия —
pool.ask с force_channel/force_spec, лестница тиров не применяется; телеметрия
под классами arch/code/review):
- **spec** — zen/mimo-v2.6-pro, max_tokens 8192, инструменты skill_list/
  skill_load/mcp_tools/mcp_call; на выходе спека (метаданные, алгоритм,
  проверки, критерии приёмки);
- **code** — zen/deepseek-v4.1-flash, max_tokens 16384, body
  `{"reasoning_effort":"medium"}`; полный BSL без заглушек;
- **judge** — kimi-cli/k3, max_tokens 8192, строгий JSON
  `{"verdict":"ACCEPT"|"NEEDS_FIXES","issues":[...],"summary":"..."}`;
  разбор — первая JSON-скобка (raw_decode); NEEDS_FIXES и итерация < max →
  code заново с issues в промпте.
Грабли учтены: zen dsflash при малом max_tokens отдаёт пустой content (reasoning
съедает лимит) — zen-стадии ретраятся до 2 раз с удвоенным max_tokens; kimi
ограничен 5-часовым окном — судья один вызов на стадию, без ретраев; невалидный
вердикт → needs_human, не догадка.
```
POST /v1/processes {kind:"dev_1c", task, project?, max_fix_iters?=2, deploy?=false}
  → 201 {process_id}
GET  /v1/processes            → список, новые сверху (task усечён до 200 зн)
GET  /v1/processes/{id}       → процесс + стадии с полными артефактами
POST /v1/processes/{id}/cancel → мягкая отмена (стоп между стадиями)
```
Статусы: queued → running → done (ACCEPT) | needs_human (исчерпаны итерации или
судья невалиден) | error | canceled. `deploy:true` после ACCEPT добавляет стадию
deploy_plan (роль code; план по скилу dev-loop) — исполнение плана только
человеком/отдельным раном.

## Приёмка владельцем (owner_marks, 2026-11-02)

Золотая метка владельца на чат целиком — единственный признак приёмки, который не
выводится из транскрипта. До этого `owner_marks` не писал никто кроме selftest
(0 строк), и судья ставил partial за неподтверждённую приёмку.

```
POST /v1/sessions/{id}/mark {mark:"ok"|"notit"|"none", note?≤200}
  → {session_id, mark, effective, stale, status, rejudge:"scheduled"}
  400 на mark вне перечисления, 404 на несуществующую сессию
GET  /v1/sessions            → строки дополнены owner_mark: "" | ok | notit
GET  /v1/sessions/{id}       → owner_mark + owner_mark_ts
GET  /v1/audit               → chats[] дополнены owner_mark (действующая метка)
```

- `none` — снять метку: статус чата снова считают правила и судья.
- Действующая метка = последняя строка журнала (журнал не перезаписывается).
- Метка перевешивает вердикт детерминированно: `ok` → success, `notit` → fail —
  и в proxy-вердикте правил (`verdicts.chat_proxy_verdict`), и в выводе судьи
  (`chat_judge._clamp_by_mark`, исходный вердикт судьи остаётся в reason).
- `stale=true`, если после метки были ходы: тогда метка — улика о части чата, а
  статус выводят правила и судья.
- Запись метки пересуживает чат в фоне (один вызов судьи, `role=judge`); судья
  недоступен → пишется proxy-вердикт, который метку тоже учитывает.
- Ночной судья берёт чат заново, если метка новее вердикта (`_needs_judge`).
- Клиент: 👍/👎 в шапке открытого чата (повторный клик снимает метку, «не то»
  спрашивает причину), чип «принято»/«не то» в строке списка и в карточке
  вердикта на вкладке «Аудит».
