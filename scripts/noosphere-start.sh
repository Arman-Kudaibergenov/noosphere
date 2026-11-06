#!/bin/bash
# Старт сервера Noosphere. Канон живёт в репозитории; ~/bin/noosphere-start.sh — симлинк сюда.
# Идемпотентно: порт занят — выходим 0 (cron-keepalive дёргает каждую минуту и после ребута).
# host/port берутся из config.yaml — один источник правды с CI-деплоем.
# Журнал: ~/noosphere/logs/server-YYYYMMDD.log с хранением 14 суток (аудит 2026-10-29, A5:
# раньше stdout уходил в /private/tmp/noosphere-start.log или в общий cron-лог — терялся).
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
export PYTHONUTF8=1

ROOT="$HOME/noosphere"
LOGDIR="$ROOT/logs"
cd "$ROOT" || exit 1

HOST=$(./.venv/bin/python -c "import yaml;print((yaml.safe_load(open('config.yaml')) or {}).get('server',{}).get('host','0.0.0.0'))" 2>/dev/null || echo 0.0.0.0)
PORT=$(./.venv/bin/python -c "import yaml;print((yaml.safe_load(open('config.yaml')) or {}).get('server',{}).get('port',8300))" 2>/dev/null || echo 8300)

if /usr/bin/nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
  echo "noosphere: уже слушает :$PORT — пропускаю"
  exit 0
fi

mkdir -p "$LOGDIR"
find "$LOGDIR" -name 'server-*.log' -mtime +14 -delete 2>/dev/null
LOG="$LOGDIR/server-$(date +%Y%m%d).log"
{
  echo "=== start $(date '+%F %T') host=$HOST port=$PORT head=$(git rev-parse --short HEAD 2>/dev/null) ==="
} >> "$LOG" 2>&1
exec ./.venv/bin/python -m uvicorn server.main:app --host "$HOST" --port "$PORT" >> "$LOG" 2>&1
