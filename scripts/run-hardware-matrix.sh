#!/bin/zsh
# Hardware-matrix runner (M6): pair -> status -> hardware suite.
# Run from a Terminal.app/iTerm window (NOT tmux — tmux breaks the TCC
# responsible-process chain and macOS silently blocks Local Network access).
# The log lands in docs/hardware-runs/ for recording into the matrix.
set -u

DEVICE="${DEVKIT_TEST_DEVICE:-${1:-}}"
if [ -z "$DEVICE" ]; then
  echo "usage: $0 <device-name-or-ip>" >&2
  exit 2
fi

cd "$(dirname "$0")/.."
mkdir -p docs/hardware-runs
LOG="docs/hardware-runs/run-$(date +%Y%m%d-%H%M%S).log"

{
  echo "== devkit pair $DEVICE =="
  uv run devkit pair "$DEVICE" --timeout 110
  echo
  echo "== devkit status $DEVICE =="
  uv run devkit status "$DEVICE"
  echo
  echo "== hardware suite =="
  DEVKIT_TEST_DEVICE="$DEVICE" uv run pytest tests/hardware -v
} 2>&1 | tee "$LOG"

echo
echo "log written to: $LOG"
echo "record the results in docs/hardware-matrix.md"
