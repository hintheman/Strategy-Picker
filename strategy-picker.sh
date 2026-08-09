#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BOT_SCRIPT="/opt/tastydaytraders/strategy-picker/strategy-picker.py"
VENV="/home/ubuntu/venv/bin/python3"
LOG_DIR="/opt/tastydaytraders/strategy-picker/logs"

mkdir -p "$LOG_DIR"
find "$LOG_DIR" -type f -name "strategy-picker-*.log" -mtime +14 -delete

# Overridable ticker lists for the `futures` / `indices` convenience commands.
# Futures use full Schwab contract symbols, which roll quarterly (U=Sep, Z=Dec,
# H=Mar, M=Jun + 2-digit year) -- update these or export the env var when the
# front month rolls. Roots (MNQ/ES) also work if your broker accepts them.
SP_FUTURES_TICKERS="${SP_FUTURES_TICKERS:-/MNQU26 /MESU26}"
SP_INDICES_TICKERS="${SP_INDICES_TICKERS:-SPX QQQ}"

if [ $# -eq 0 ]; then
  echo "Usage: strategy-picker.sh <command> [bot args...]"
  echo "  Commands:"
  echo "    futures                 start a tagged continuous run on $SP_FUTURES_TICKERS (--state-tag futures)"
  echo "    indices                 start a tagged continuous run on $SP_INDICES_TICKERS (--state-tag indices)"
  echo "    start|stop|restart|status [--state-tag TAG]   manage a run (untagged if no --state-tag)"
  echo "  Examples:"
  echo "    strategy-picker.sh futures"
  echo "    strategy-picker.sh indices"
  echo "    strategy-picker.sh stop  --state-tag futures"
  echo "    strategy-picker.sh --tickers AAPL MSFT --run once --state-tag stocks"
  exit 0
fi

# Resolve the command. The `futures`/`indices` shortcuts expand to a tagged
# continuous start with the default ticker list (plus any extra args appended).
CMD=""
case "${1:-}" in
  start|stop|restart|status) CMD="$1"; shift ;;
  futures) shift; set -- --tickers $SP_FUTURES_TICKERS --run continuous --state-tag futures "$@"; CMD="start" ;;
  indices) shift; set -- --tickers $SP_INDICES_TICKERS --run continuous --state-tag indices "$@"; CMD="start" ;;
esac
BOT_ARGS=("$@")

# Pull --state-tag out of the bot args so the log file and the start/stop/status
# process match can be namespaced per run -- this is what lets a stocks run, an
# indices run, and a futures run coexist without clobbering each other. The tag
# stays in BOT_ARGS too, so it still reaches the bot (for the Discord footer).
STATE_TAG=""
for ((i = 0; i < ${#BOT_ARGS[@]}; i++)); do
  if [[ "${BOT_ARGS[$i]}" == "--state-tag" ]]; then
    STATE_TAG="${BOT_ARGS[$((i + 1))]}"
    break
  fi
done

if [[ -n "$STATE_TAG" ]]; then
  LOG="$LOG_DIR/strategy-picker-$STATE_TAG-$(date +%F).log"
  MATCH="--state-tag $STATE_TAG"
else
  LOG="$LOG_DIR/strategy-picker-$(date +%F).log"
  MATCH="$BOT_SCRIPT"
fi

_running() { pgrep -f -- "$MATCH" > /dev/null; }
_label() { [[ -n "$STATE_TAG" ]] && echo "strategy-picker [$STATE_TAG]" || echo "strategy-picker"; }

run_bot() {
  nohup "$VENV" -u "$BOT_SCRIPT" "${BOT_ARGS[@]}" >> "$LOG" 2>&1 &
  echo "$(_label) started (PID: $!)"
  echo "logging to $LOG"
}

start()  { if _running; then echo "$(_label) is already running"; else run_bot; fi; }
stop()   { pkill -f -- "$MATCH" && echo "$(_label) stopped" || echo "$(_label) not running"; }
status() { _running && echo "$(_label) is running" || echo "$(_label) is NOT running"; }

case "$CMD" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  *)       run_bot ;;
esac
