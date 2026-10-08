#!/usr/bin/env bash
# Command Center — start / stop / status helper (no dependencies).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${2:-9090}"
LOG="$DIR/logs/server.log"
PIDFILE="$DIR/logs/server.pid"

cmd="${1:-start}"
case "$cmd" in
  start)
    mkdir -p "$DIR/logs"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDFILE")) — http://127.0.0.1:$PORT/"
      exit 0
    fi
    nohup python3 "$DIR/server.py" --port "$PORT" >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 1
    echo "Command Center started (pid $(cat "$PIDFILE")) — http://127.0.0.1:$PORT/"
    echo "log: $LOG"
    ;;
  stop)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
      echo "stopped"
    else
      echo "not running"
    fi
    ;;
  restart)
    "$0" stop; "$0" start "$PORT"
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running (pid $(cat "$PIDFILE")) — http://127.0.0.1:$PORT/"
    else
      echo "not running"
    fi
    ;;
  open)
    open "http://127.0.0.1:$PORT/" 2>/dev/null || echo "run: open http://127.0.0.1:$PORT/"
    ;;
  test)
    python3 -m unittest discover -s "$DIR/tests" -p "test_*.py" -v
    ;;
  health)
    curl -fsS "http://127.0.0.1:$PORT/api/status" >/dev/null && echo "ok: server responding" || echo "not responding on $PORT"
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status|open|test|health} [port]"
    exit 1
    ;;
esac
