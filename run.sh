#!/usr/bin/env bash
# Command Center — start / stop / status helper (no dependencies).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${2:-9090}"
LOG="$DIR/logs/server.log"
PIDFILE="$DIR/logs/server.pid"
LABEL="com.vincentphan.command-center"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

# Once `install` has run, launchd owns the process (KeepAlive restarts it on
# crash and RunAtLoad starts it at login); start/stop/restart/status defer to it.
agent_installed() { [ -f "$PLIST" ]; }
agent_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
agent_pid() { launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '$1 == "pid" {print $3}'; }

write_plist() {
  local py
  py="$(command -v python3)"
  mkdir -p "$(dirname "$PLIST")" "$DIR/logs"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$py</string>
    <string>$DIR/server.py</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(dirname "$py"):/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" >/dev/null
}

cmd="${1:-start}"
case "$cmd" in
  start)
    if agent_installed; then
      agent_loaded || launchctl bootstrap "$DOMAIN" "$PLIST"
      launchctl kickstart "$DOMAIN/$LABEL"
      echo "Command Center running under launchd (pid $(agent_pid)) — http://127.0.0.1:$PORT/"
      exit 0
    fi
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
    if agent_loaded; then
      launchctl bootout "$DOMAIN/$LABEL"
      echo "stopped (launchd agent unloaded until next login or ./run.sh start)"
    elif [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
      echo "stopped"
    else
      echo "not running"
    fi
    ;;
  restart)
    if agent_loaded; then
      launchctl kickstart -k "$DOMAIN/$LABEL"
      echo "restarted under launchd (pid $(agent_pid))"
    else
      "$0" stop; "$0" start "$PORT"
    fi
    ;;
  status)
    if agent_loaded; then
      echo "running under launchd (pid $(agent_pid)) — http://127.0.0.1:$PORT/"
    elif agent_installed; then
      echo "launchd agent installed but not loaded — ./run.sh start"
    elif [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running (pid $(cat "$PIDFILE")) — http://127.0.0.1:$PORT/"
    else
      echo "not running"
    fi
    ;;
  install)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")"
    fi
    rm -f "$PIDFILE"
    agent_loaded && launchctl bootout "$DOMAIN/$LABEL"
    write_plist
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "installed $PLIST (starts at login, restarts on crash)"
    echo "log: $LOG"
    ;;
  uninstall)
    agent_loaded && launchctl bootout "$DOMAIN/$LABEL"
    rm -f "$PLIST"
    echo "uninstalled launchd agent"
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
    echo "usage: $0 {start|stop|restart|status|install|uninstall|open|test|health} [port]"
    exit 1
    ;;
esac
