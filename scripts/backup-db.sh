#!/bin/bash
# Бэкап БД «Ноосферы» ВНЕ деплой-дерева + копия на второй хост (аудит 2026-10-29, A6).
# .backup, а не cp: при WAL копия файла может оказаться несогласованной.
# Значения секретов не читаются и не печатаются.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ROOT="$HOME/noosphere"
DB="$ROOT/data/noosphere.db"
DEST="$HOME/backups/noosphere"
LOGDIR="$ROOT/logs"
REMOTE="hermes@100.65.118.22"
RDIR="/home/hermes/backups/noosphere"
KEEP_LOCAL=14
KEEP_REMOTE=7
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/noosphere-$STAMP.db"

mkdir -p "$DEST" "$LOGDIR"
LOG="$LOGDIR/backup.log"
say() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

if [ ! -f "$DB" ]; then say "FATAL: нет $DB"; exit 1; fi

sqlite3 "$DB" ".backup '$OUT'" || { say "FATAL: .backup не прошёл"; exit 1; }
INT=$(sqlite3 "file:$OUT?immutable=1" "PRAGMA integrity_check;" 2>&1 | head -1)
SV=$(sqlite3 "file:$OUT?immutable=1" "SELECT value FROM meta WHERE key='schema_version';" 2>&1 | head -1)
TN=$(sqlite3 "file:$OUT?immutable=1" "SELECT count(*) FROM turns;" 2>&1 | head -1)
SZ=$(stat -f %z "$OUT" 2>/dev/null)
say "local: noosphere-$STAMP.db size=$SZ integrity=$INT schema=$SV turns=$TN"
if [ "$INT" != "ok" ]; then say "FATAL: integrity_check=$INT"; exit 1; fi

if ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE" "mkdir -p '$RDIR'" >>"$LOG" 2>&1; then
  if scp -q -o BatchMode=yes -o ConnectTimeout=20 "$OUT" "$REMOTE:$RDIR/" >>"$LOG" 2>&1; then
    say "remote: копия на $REMOTE:$RDIR"
    RN=$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE" "ls -1t '$RDIR'/noosphere-*.db 2>/dev/null | tail -n +$((KEEP_REMOTE+1)) | while read -r f; do rm -f \"\$f\"; done; ls -1 '$RDIR'/noosphere-*.db 2>/dev/null | wc -l" 2>>"$LOG")
    say "remote: осталось копий ${RN:-?}"
  else
    say "WARN: scp не прошёл — локальная копия есть"
  fi
else
  say "WARN: $REMOTE недоступен — локальная копия есть"
fi

ls -1t "$DEST"/noosphere-*.db 2>/dev/null | tail -n +$((KEEP_LOCAL+1)) | while read -r f; do
  rm -f "$f"; say "pruned: $(basename "$f")"
done
say "local: осталось копий $(ls -1 "$DEST"/noosphere-*.db 2>/dev/null | wc -l | tr -d ' ')"
