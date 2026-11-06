"""config.py — загрузка конфигурации Noosphere.

Контракт: load_config() -> Config. Все модули берут настройки только отсюда.
Секреты читаются из файлов (api_key_file / credentials_file) или env — никогда
не из тела конфига. Пути с ~ раскрываются.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 8300
    token: str = ""                 # резолвнутое значение (token_file > token > env)
    token_file: str = ""            # путь к файлу с Bearer вне дерева деплоя (~/.secrets)
    token_source: str = ""          # file | inline | env | "" — для startup-варнинга
    lan_no_auth: bool = False       # без Bearer только для LAN/tailnet; внешние — по-прежнему 401
    tz_offset_hours: int = 4
    vault_key_file: str = "~/.secrets/noosphere-vault.key"  # ключ Fernet vault (G1.5)
    rate_limit: dict = field(default_factory=lambda: {
        "enabled": True, "rpm": 120, "burst": 40})  # per-IP токен-бакет на /v1/* (G1.4)


@dataclass
class ChannelCfg:
    name: str
    kind: str                       # alibaba | ollama | kimi | zen
    base_url: str = ""
    api_key_file: str = ""
    api_key_env: str = ""
    credentials_file: str = ""
    quota_unit: str = "tokens"      # calls | tokens
    daily_budget_tokens: int = 0    # 0 = без бюджета
    enabled: bool = True
    vision: bool = False            # v1.6: канал принимает изображения
    vision_model: str = ""          # модель для хода с картинками (напр. qwen3.7-plus)
    context_window: int = 0         # окно модели в токенах (0 — неизвестно; UI покажет только число)
    proxy: str = ""                 # egress-прокси канала (http://host:port); пусто = напрямую.
                                    # 30.10.2026: фильтр на маршруте дропает TLS OpenSSL-клиентов
                                    # к *.kimi.com — kimi-cli ходит через privoxy контура (batch43/45)
    accounts: list = field(default_factory=list)  # пул аккаунтов (accounts.py)

    def api_key(self) -> str:
        if self.api_key_env and os.environ.get(self.api_key_env):
            return os.environ[self.api_key_env]
        if self.api_key_file:
            p = Path(self.api_key_file).expanduser()
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
        return ""


@dataclass
class RouterCfg:
    max_tokens: int = 1024
    continue_max: int = 2               # автопродолжения обрыва length после потолка роста (порт канона 29.10)
    # E6 (аудит 2026-10-29): запасной тир, когда лестница кончилась отказом
    # (429 квоты подписки, 401 ключ, 5xx, «нет доступного тира»). Ход доигрывается
    # на бесплатном канале вместо пустой ошибки владельцу. Пусто — запасного нет.
    fallback_tier: str = ""
    limits: dict = field(default_factory=lambda: {"local": 0, "base": 0, "mid": 500, "deep": 60, "max": 20})
    tier_channels: dict = field(default_factory=dict)  # тир → {channel, model?, body?} или имя канала строкой
    # Роль → модель (замер 02.11): route/title/summary/chat/worker/judge, каждая
    # {channel, model, body?, max_tokens?, failover: [{channel, model, body?}]}.
    # Пусто — работает лестница тиров. Причина: base и mid смотрели в один
    # station/local/deepseek-flash (95% объёма), поэтому эскалация не меняла
    # модель, а следующий шаг deep уводил чат и исполнителя на kimi-cli/k3.1-pro —
    # модель ночного судьи с окном 100 вызовов на 5 часов и 44% отказов.
    roles: dict = field(default_factory=dict)   # роль → {channel, model, body?, max_tokens?, failover?}


@dataclass
class AlertsCfg:
    transport: str = "log"          # ssh-notify | bot | log
    ssh_target: str = ""
    notify_command: str = "~/bin/notify.py"
    bot_token_file: str = ""
    bot_chat_id: str = ""
    sla_minutes: int = 30
    digest_minutes: int = 60


@dataclass
class AntiloopCfg:
    budget_warn_pct: int = 70
    budget_crit_pct: int = 90
    max_repeat_fingerprint: int = 3
    max_water_cycles: int = 4
    # Решение владельца 2026-11-04 («без ограничений», план единого контура):
    # детекторы остаются и шлют алерт, но ход НЕ останавливают — защита
    # наблюдаемостью, а не запретом. True возвращает старое поведение (стоп).
    stop_enabled: bool = False


@dataclass
class ToolsCfg:
    enabled: bool = True            # мастер-выключатель инструментов агента
    shell: bool = True              # shell_exec — самый мощный и самый рискованный
    # Ход идёт ДО РЕЗУЛЬТАТА. Ограничивают его ресурсы (время и токены) и детекторы
    # отсутствия прогресса (antiloop: повтор входа, treading-water), а не счётчик
    # раундов: 6/12 раундов резали задачу на середине (бой 20.10 — 12 раундов ушли
    # на разведку, ноль действий). max_iters — только страховка от безумия.
    max_iters: int = 100
    turn_max_minutes: int = 30      # бюджет времени одного хода
    turn_max_tokens: int = 400_000  # бюджет токенов хода (prompt+completion, все раунды)
    # Бюджет под-агента delegate (28.10.2026): у ребёнка свой ход и свой потолок,
    # чтобы разведка не ела бюджет координатора
    sub_turn_budget: int = 100_000
    sub_tier: str = "mid"           # тир модели под-агента delegate (28.10 — лёгкий текст
                                      # уезжал на base → deepseek-flash окно 131k)
    sub_output_cap: int = 2000       # кап результата инструмента для под-агента
    shell_timeout: int = 60         # сек, жёсткий потолок одной команды
    output_cap: int = 8000          # символов на результат инструмента
    # Компакция истории хода при таком размере контекста (токены), 0 — выключено.
    # Замер 03.11: единственный триггер «70% окна» на станции (131 072) = 91 750
    # токенов, а максимум контекста чат-хода за 7 дней — 77 134, то есть компакция
    # по окну не срабатывала ни разу, а расход хода квадратичен по числу раундов
    # (10 ходов с ≥40 раундами = 44,5% расхода чата).
    compact_above_tokens: int = 24_000
    stt_url: str = "http://192.168.0.22:8899/transcribe"  # gigastt файл-путь (CT106)
    # Голос Ноосферы (режим «Разговор», 01.11.2026): ESpeech — клон голоса Армана,
    # Silero v4_ru — быстрый фолбэк чужим голосом (159 мс против 2.2-3.4 с).
    tts_url: str = "http://192.168.0.22:8897/tts"
    tts_fallback_url: str = "http://192.168.0.22:8898/tts"
    read_allow: list = field(default_factory=list)   # доп. каталоги чтения (к ~/workspace, /tmp)
    default_agent: bool = True        # решение владельца 2026-10-17: ход без явного
                                      # agent=false идёт с инструментами (действие по умолчанию)


@dataclass
class RouteCfg:
    enabled: bool = True              # мастер-выключатель единой точки входа
    tier: str = "base"                # тир классификатора (эскалация выключена)
    max_tokens: int = 300
    confidence_threshold: float = 0.7  # ниже — клиент показывает кнопки выбора
    prefilter: bool = True            # regex-маркеры до LLM (бесплатно)
    analysis_hour: int = 4            # час ночного анализатора (локальное UTC+4)
    auto_projects: bool = True        # классификатор может создавать проекты сам
    auto_projects_max: int = 50       # потолок auto-проектов; сверх — в «Личное» + warning


@dataclass
class SkillsCfg:
    dir: str = "~/.agents/skills"   # каталог скилов (*/SKILL.md с YAML-frontmatter)
    auto_include: list = field(default_factory=lambda: [
        "1c-*", "cfe-*", "dev-loop", "ibcmd-farm", "form-*", "meta-*", "role-*",
        "scaffold-object", "subsystem-*", "extensions-*"])  # паттерны для инъекции в system


@dataclass
class McpCfg:
    servers: dict = field(default_factory=dict)  # name -> {url, desc, enabled, headers?}


@dataclass
class MobileCfg:
    """Мобильный доступ (образец Hermes Mobile).

    port для SSH-туннеля берётся из server.port — здесь только то, чего в
    server нет: адрес loopback для телефона и сроки жизни сессий/сертификатов.
    """
    enabled: bool = True                 # раздел «Мобильный доступ» в окне
    loopback_host: str = "127.0.0.1"     # на телефоне после туннеля — этот адрес
    session_ttl_hours: int = 12          # 0 = сессия без срока (до «завершить все»)
    default_expires_days: int = 30       # срок действия нового профиля (0 = бессрочно)
    cert_days: int = 30                  # срок клиентского mTLS-сертификата


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    mobile: MobileCfg = field(default_factory=MobileCfg)
    data_dir: Path = Path("./data")
    channels: dict = field(default_factory=dict)        # name -> ChannelCfg
    router: RouterCfg = field(default_factory=RouterCfg)
    alerts: AlertsCfg = field(default_factory=AlertsCfg)
    antiloop: AntiloopCfg = field(default_factory=AntiloopCfg)
    tools: ToolsCfg = field(default_factory=ToolsCfg)
    route: RouteCfg = field(default_factory=RouteCfg)
    skills: SkillsCfg = field(default_factory=SkillsCfg)
    mcp: McpCfg = field(default_factory=McpCfg)
    owner_user: str = "arman"           # логин владельца (приёмка считает его ходы)
    projects_seed: list = field(default_factory=list)  # projects.seed: [{name, keywords}]
    panel_nodes: list = field(default_factory=list)  # panel.nodes: узлы приборной панели

    def channel(self, name: str) -> ChannelCfg | None:
        return self.channels.get(name)


_CONFIG: Config | None = None


@dataclass
class DevRunCfg:
    """Самодействие прогона доработки (E0, аудит 2026-10-29).

    Одобрений человеком нет и не будет (решения владельца 17.10 и 29.10) — вместо
    гейта одобрения патч проверяется автоматическим smoke ДО commit/push/рестарта.
    """
    auto_apply: bool = True       # патч успешного прогона применяется сам
    auto_restart: bool = True     # сервис перезапускается на новом коде
    smoke_gate: bool = True       # scripts/smoke.py обязан пройти до commit/push
    smoke_timeout: int = 300      # сек на прогон smoke
    allow_dirty: bool = False     # в грязном дереве патч отклоняется (A3/F5)
    # решение владельца 2026-10-31 «всё на Маке»: прогон доработки исполняет
    # сам сервер (server/devexec.py) в worktree СВОЕГО репозитория, а не
    # оболочка ноутбука (там base был из форка GitLab — патч не ложился).
    execute: bool = True          # включать серверный цикл исполнителя
    poll_seconds: int = 5         # как часто смотреть очередь queued-прогонов
    worktree_root: str = "~/workspace/noosphere-dev"
    exec_class: str = "code"      # класс задачи хода исполнителя → тир модели
    exec_max_iters: int = 60      # потолок раундов инструментов
    exec_budget: int = 0          # 0 → tools.turn_max_tokens
    max_wall_minutes: int = 20    # стена на прогон
    patch_max_chars: int = 400_000  # потолок размера патча
    # параллельная доработка (слово владельца 2026-11-01): интеграция патча
    # всегда на свежий origin/main в одноразовом worktree (autopatch)
    rebase_retries: int = 2       # push-гонка: столько раз переложить патч на новую базу
    stale_retries: int = 1        # устаревшая база: столько раз переисполнить прогон


def load_config(path: str | os.PathLike | None = None, *, reload: bool = False) -> Config:
    """Читает YAML-конфиг один раз; reload=True перечитывает (тесты)."""
    global _CONFIG
    if _CONFIG is not None and not reload:
        return _CONFIG
    p = Path(path) if path else Path(os.environ.get("NOOSPHERE_CONFIG", DEFAULT_CONFIG_PATH))
    raw: dict = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    cfg = Config()
    srv = raw.get("server") or {}
    inline_token = srv.get("token", "") or ""
    token_file = srv.get("token_file", "") or ""
    token, token_source = "", ""
    if token_file:  # G1.4: приоритет token_file > inline token > env
        tf = Path(token_file).expanduser()
        if tf.exists():
            token = tf.read_text(encoding="utf-8").strip()
            token_source = "file"
        else:
            print(f"[config] token_file {tf} не существует — токен не загружен из файла")
    if not token and inline_token:
        token, token_source = inline_token, "inline"
    if not token and os.environ.get("NOOSPHERE_TOKEN"):
        token, token_source = os.environ["NOOSPHERE_TOKEN"], "env"
    cfg.server = ServerCfg(
        host=srv.get("host", "127.0.0.1"),
        port=int(srv.get("port", 8300)),
        token=token,
        token_file=token_file,
        token_source=token_source,
        lan_no_auth=bool(srv.get("lan_no_auth", False)),
        tz_offset_hours=int(srv.get("tz_offset_hours", 4)),
        vault_key_file=srv.get("vault_key_file", "~/.secrets/noosphere-vault.key"),
        rate_limit={**ServerCfg().rate_limit, **(srv.get("rate_limit") or {})},
    )
    mb = raw.get("mobile") or {}
    cfg.mobile = MobileCfg(**{k: mb[k] for k in
                              ("enabled", "loopback_host", "session_ttl_hours",
                               "default_expires_days", "cert_days") if k in mb})
    cfg.data_dir = Path(raw.get("data_dir", "./data")).expanduser()
    dr = raw.get("devrun") or {}
    cfg.devrun = DevRunCfg(**{k: dr[k] for k in
                              ("auto_apply", "auto_restart", "smoke_gate",
                               "smoke_timeout", "allow_dirty", "execute",
                               "poll_seconds", "worktree_root", "exec_class",
                               "exec_max_iters", "exec_budget",
                               "max_wall_minutes", "patch_max_chars",
                               "rebase_retries", "stale_retries") if k in dr})

    for name, ch in (raw.get("channels") or {}).items():
        cfg.channels[name] = ChannelCfg(
            name=name,
            kind=ch.get("kind", ""),
            base_url=ch.get("base_url", ""),
            api_key_file=ch.get("api_key_file", ""),
            api_key_env=ch.get("api_key_env", ""),
            credentials_file=ch.get("credentials_file", ""),
            quota_unit=ch.get("quota_unit", "tokens"),
            daily_budget_tokens=int(ch.get("daily_budget_tokens", 0)),
            enabled=bool(ch.get("enabled", True)),
            vision=bool(ch.get("vision", False)),
            vision_model=ch.get("vision_model", ""),
            context_window=int(ch.get("context_window", 0)),
            proxy=str(ch.get("proxy", "") or ""),
            accounts=list(ch.get("accounts") or []),
        )

    rt = raw.get("router") or {}
    cfg.router = RouterCfg(max_tokens=int(rt.get("max_tokens", 1024)),
                           continue_max=int(rt.get("continue_max", 2)),
                           limits={k: int(v) for k, v in (rt.get("limits") or {}).items()} or RouterCfg().limits,
                           tier_channels=dict(rt.get("tier_channels") or {}),
                           roles=dict(rt.get("roles") or {}),
                           # E6: без этой строки router.fallback_tier из config.yaml
                           # не попадал в конфиг и запасной тир никогда не срабатывал
                           fallback_tier=str(rt.get("fallback_tier", "") or ""))

    al = raw.get("alerts") or {}
    cfg.alerts = AlertsCfg(**{k: al[k] for k in ("transport", "ssh_target", "notify_command", "bot_token_file",
                                                 "bot_chat_id", "sla_minutes", "digest_minutes") if k in al})

    an = raw.get("antiloop") or {}
    cfg.antiloop = AntiloopCfg(**{k: an[k] for k in ("budget_warn_pct", "budget_crit_pct",
                                                     "max_repeat_fingerprint", "max_water_cycles",
                                                     "stop_enabled") if k in an})

    tl = raw.get("tools") or {}
    cfg.tools = ToolsCfg(**{k: tl[k] for k in ("enabled", "shell", "max_iters",
                                               "turn_max_minutes", "turn_max_tokens",
                                               "sub_turn_budget", "sub_tier", "sub_output_cap",
                                               "shell_timeout", "output_cap",
                                               "compact_above_tokens", "stt_url",
                                               "tts_url", "tts_fallback_url",
                                               "read_allow", "default_agent") if k in tl})

    ro = raw.get("route") or {}
    cfg.route = RouteCfg(**{k: ro[k] for k in ("enabled", "tier", "max_tokens",
                                               "confidence_threshold", "prefilter",
                                               "analysis_hour", "auto_projects",
                                               "auto_projects_max") if k in ro})

    sk = raw.get("skills") or {}
    cfg.skills = SkillsCfg(
        dir=sk.get("dir", SkillsCfg().dir),
        auto_include=list(sk.get("auto_include") or SkillsCfg().auto_include))

    mc = raw.get("mcp") or {}
    # headers — только если он реально задан: значение — строка или {from_file: путь}
    # (нужно MCP с авторизацией, напр. farm-router: x-api-key).
    cfg.mcp = McpCfg(servers={
        name: {**{"url": str(s.get("url", "")), "desc": str(s.get("desc", "")),
                  "enabled": bool(s.get("enabled", True))},
               **({"headers": s["headers"]} if isinstance(s.get("headers"), dict) else {})}
        for name, s in (mc.get("servers") or {}).items()})

    cfg.owner_user = str(raw.get("owner_user", "arman")).strip() or "arman"
    pj = raw.get("projects") or {}
    cfg.projects_seed = list(pj.get("seed") or [])
    cfg.panel_nodes = list((raw.get("panel") or {}).get("nodes") or [])

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    _CONFIG = cfg
    return cfg
