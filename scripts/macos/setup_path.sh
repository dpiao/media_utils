#!/usr/bin/env bash
# Add scripts/macos to the user's shell profile PATH (once).
set -euo pipefail
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
MARKER="# media_utils scripts"
LINE="export PATH=\"$SCRIPTS_DIR:\$PATH\"  $MARKER"

for rc in "$HOME/.zprofile" "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc"; do
  if [[ -f "$rc" ]] && grep -qF "$MARKER" "$rc"; then
    echo "Already on PATH via $rc"
    exit 0
  fi
done

TARGET="$HOME/.zprofile"
if [[ ! -f "$TARGET" ]] && [[ -f "$HOME/.bash_profile" ]]; then
  TARGET="$HOME/.bash_profile"
fi
touch "$TARGET"
echo "" >> "$TARGET"
echo "$LINE" >> "$TARGET"
echo "Added to $TARGET:"
echo "  $SCRIPTS_DIR"
echo "Open a new terminal to use: mediactl, sync_media_to_s3"
