#!/usr/bin/env bash
# Command Center — start / stop / status helper (no dependencies).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${2:-9090}"
LOG="$DIR/logs/server.log"
PIDFILE="$DIR/logs/server.pid"
# CC_LABEL lets a second checkout run its own agents (e.g. a test instance on another port).
LABEL="${CC_LABEL:-com.vincentphan.command-center}"
HC_LABEL="$LABEL.healthcheck"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
HC_PLIST="$AGENTS/$HC_LABEL.plist"
HC_LOG="$DIR/logs/healthcheck.log"
DOMAIN="gui/$(id -u)"

# Once `install` has run, launchd owns the dashboard (KeepAlive restarts it on
# crash and RunAtLoad starts it at login) plus a separate healthcheck job that
# notifies when the dashboard stops answering. start/stop/restart/status defer
# to launchd and always handle both jobs, so a deliberate stop never pages.
agent_installed() { [ -f "$PLIST" ]; }
loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }
agent_loaded() { loaded "$LABEL"; }
agent_pid() { launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '$1 == "pid" {print $3}'; }
load_agents() {
  loaded "$LABEL" || launchctl bootstrap "$DOMAIN" "$PLIST"
  if [ -f "$HC_PLIST" ] && ! loaded "$HC_LABEL"; then launchctl bootstrap "$DOMAIN" "$HC_PLIST"; fi
}
unload_agents() {
  if loaded "$HC_LABEL"; then launchctl bootout "$DOMAIN/$HC_LABEL"; fi
  if loaded "$LABEL"; then launchctl bootout "$DOMAIN/$LABEL"; fi
}

write_plists() {
  local py interval
  py="$(command -v python3)"
  interval="$("$py" -c 'import json,sys; print(int((json.load(open(sys.argv[1])).get("healthcheck") or {}).get("interval_seconds", 120)))' "$DIR/config.json")"
  mkdir -p "$AGENTS" "$DIR/logs"
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
  cat > "$HC_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$HC_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$py</string>
    <string>$DIR/healthcheck.py</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartInterval</key><integer>$interval</integer>
  <key>StandardOutPath</key><string>$HC_LOG</string>
  <key>StandardErrorPath</key><string>$HC_LOG</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" "$HC_PLIST" >/dev/null
}

cmd="${1:-start}"
case "$cmd" in
  start)
    if agent_installed; then
      load_agents
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
      unload_agents
      echo "stopped dashboard + healthcheck (unloaded until next login or ./run.sh start)"
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
      if loaded "$HC_LABEL"; then echo "healthcheck loaded (log: $HC_LOG)"; else echo "healthcheck NOT loaded — ./run.sh install"; fi
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
    unload_agents
    write_plists
    load_agents
    echo "installed $PLIST (starts at login, restarts on crash)"
    echo "installed $HC_PLIST (checks /api/health on an interval, notifies if down)"
    echo "logs: $LOG, $HC_LOG"
    ;;
  uninstall)
    unload_agents
    rm -f "$PLIST" "$HC_PLIST"
    echo "uninstalled launchd agents"
    ;;
  open)
    open "http://127.0.0.1:$PORT/" 2>/dev/null || echo "run: open http://127.0.0.1:$PORT/"
    ;;
  test)
    python3 -m unittest discover -s "$DIR/tests" -p "test_*.py" -v
    ;;
  health)
    curl -fsS "http://127.0.0.1:$PORT/api/health" && echo || echo "not responding on $PORT"
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status|install|uninstall|open|test|health} [port]"
    exit 1
    ;;
esac
