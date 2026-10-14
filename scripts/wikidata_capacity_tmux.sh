#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${WIKIDATA_CAPACITY_SESSION:-humansmap-wikidata-capacity}"
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_URL="${WIKIDATA_CAPACITY_SOURCE:-https://dumps.wikimedia.org/wikidatawiki/entities/20260831/wikidata-20260831-all.json.bz2}"
EXPECTED_SHA1="${WIKIDATA_CAPACITY_SHA1:-b24eea0dee9f2fbe7ca70e6efe3209eb69bc48af}"
THRESHOLDS="${WIKIDATA_CAPACITY_THRESHOLDS:-10,20,30,50}"

run_capacity_pass() {
  cd "$REPOSITORY_ROOT"
  local command=(
    docker compose --profile ingestion run --rm --no-deps --build ingestion
    python /app/scripts/global_capacity_pass.py
    --source "$SOURCE_URL"
    --expected-sha1 "$EXPECTED_SHA1"
    --thresholds "$THRESHOLDS"
    --max-rss-mib 900
    --min-free-gib 25
  )
  exec "${command[@]}"
}

start_session() {
  if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "tmux session already exists: $SESSION_NAME"
    echo "Attach with: tmux attach -t $SESSION_NAME"
    return 1
  fi

  tmux new-session -d -s "$SESSION_NAME" -c "$REPOSITORY_ROOT" \
    "bash -c './scripts/wikidata_capacity_tmux.sh _run; result=\$?; printf \"\\nCapacity pass exited with status %s.\\n\" \"\$result\"; exec bash'"

  echo "Started: $SESSION_NAME"
  echo "Attach with: tmux attach -t $SESSION_NAME"
  echo "Detach without stopping it: Ctrl-b, then d"
}

show_status() {
  if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "No tmux session named $SESSION_NAME"
    return 1
  fi
  tmux list-panes -t "$SESSION_NAME" \
    -F 'session=#{session_name} pid=#{pane_pid} command=#{pane_current_command} dead=#{pane_dead}'
  tmux capture-pane -p -t "$SESSION_NAME" -S -40
}

case "${1:-start}" in
  start)
    start_session
    ;;
  attach)
    exec tmux attach-session -t "$SESSION_NAME"
    ;;
  status)
    show_status
    ;;
  _run)
    run_capacity_pass
    ;;
  *)
    echo "Usage: $0 [start|attach|status]" >&2
    exit 2
    ;;
esac
