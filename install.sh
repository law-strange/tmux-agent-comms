#!/usr/bin/env bash
# Installer for tmux-agent-comms: symlink the CLI into ~/bin + seed a registry.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${AGENT_COMMS_BIN_DIR:-$HOME/bin}"
CONFIG_DIR="$HOME/.config/agent-comms"
TARGET="$BIN_DIR/agent_comms"

echo "tmux-agent-comms installer"

# --- deps ---
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found"; exit 1; }
if ! command -v tmux >/dev/null 2>&1; then
  echo "WARNING: tmux not found on PATH. Install it before using (e.g. 'brew install tmux' / 'apt install tmux')."
fi

# --- symlink CLI ---
mkdir -p "$BIN_DIR"
ln -sf "$REPO_DIR/agent_comms.py" "$TARGET"
chmod +x "$REPO_DIR/agent_comms.py"
echo "linked: $TARGET -> $REPO_DIR/agent_comms.py"

case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) echo "NOTE: $BIN_DIR is not on your PATH. Add:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

# --- seed registry from example if none ---
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/registry.json" ]; then
  cp "$REPO_DIR/registry.example.json" "$CONFIG_DIR/registry.json"
  echo "seeded registry: $CONFIG_DIR/registry.json (edit to match your tmux sessions)"
else
  echo "registry already present: $CONFIG_DIR/registry.json (left unchanged)"
fi

echo "done. try:  agent_comms list"
