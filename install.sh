#!/bin/sh
# Install the tg-ask bridge into OpenCode.
#
#   ./install.sh          install/update the plugin and check settings
#   ./install.sh --test   same plus a test message in Telegram
#
# The only file outside this folder is the plugin in OpenCode's plugin folder.
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PLUGIN_DIR="${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}/plugins"
PLUGIN_SRC="$DIR/tg-ask.ts"
PLUGIN_DST="$PLUGIN_DIR/tg-ask.ts"
PYTHON="${TG_ASK_PYTHON:-python3}"
SEND_TEST=0

usage() {
  cat <<EOF
Install tg-ask (OpenCode questions → Telegram).

Usage: $0 [--test]

  --test   after checks, send a test message to Telegram
  -h       this help

Environment variables:
  OPENCODE_CONFIG_DIR  OpenCode config folder (default ~/.config/opencode)
  TG_ASK_PYTHON        Python interpreter (default python3)
EOF
}

for arg in "$@"; do
  case "$arg" in
    --test) SEND_TEST=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "tg-ask: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

fail() {
  echo "✗ $1" >&2
  exit 1
}

[ -f "$PLUGIN_SRC" ] || fail "missing $PLUGIN_SRC"
[ -f "$DIR/tg-ask.py" ] || fail "missing $DIR/tg-ask.py"
command -v "$PYTHON" >/dev/null 2>&1 || fail "$PYTHON not found (set TG_ASK_PYTHON=/path/to/python3)"

if [ ! -f "$DIR/.env" ]; then
  [ -f "$DIR/.env.example" ] || fail "missing $DIR/.env.example"
  umask 077
  cp "$DIR/.env.example" "$DIR/.env"
  echo "• created $DIR/.env — fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see README.md)"
fi

command -v opencode >/dev/null 2>&1 ||
  echo "⚠ opencode not found in PATH: form replies won't return to the session"

# Substitute the actual script path into the installed copy.
mkdir -p "$PLUGIN_DIR"
DIR_ESC=$(printf '%s' "$DIR" | sed 's/[&|\\]/\\&/g')
sed "s|^const SCRIPT = .*|const SCRIPT = process.env.TG_ASK_SCRIPT ?? \"$DIR_ESC/tg-ask.py\"|" \
  "$PLUGIN_SRC" >"$PLUGIN_DST"
grep -q "$DIR/tg-ask.py" "$PLUGIN_DST" || fail "failed to substitute script path in $PLUGIN_DST"
echo "✓ plugin: $PLUGIN_DST"

echo "• offline checks of the script"
"$PYTHON" "$DIR/tg-ask.py" --selfcheck || fail "offline checks failed"

echo "• settings check"
"$PYTHON" "$DIR/tg-ask.py" --check || fail "settings not ready: fill in $DIR/.env (see README.md)"

if [ "$SEND_TEST" -eq 1 ]; then
  echo "• test message"
  "$PYTHON" "$DIR/tg-ask.py" notify "tg-ask installed. This is a test message, no reply needed." ||
    fail "failed to send message"
fi

cat <<EOF

Done. OpenCode reloads the plugin automatically (plugin folder is watched).

  Check: ask a question with the question tool — it will arrive in Telegram as buttons.
  Log:   $DIR/tg-ask.log
  Remove:  rm "$PLUGIN_DST"
EOF