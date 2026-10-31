#!/bin/sh
# curl -LsSf https://raw.githubusercontent.com/lnotspotl/localidraw/main/install.sh | sh
set -eu
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
fi
whl=$(curl -fsSL https://api.github.com/repos/lnotspotl/localidraw/releases/latest | grep -o 'https://[^"]*\.whl' | head -n 1 || true)
[ -n "$whl" ] || { echo "localidraw: no release wheel found" >&2; exit 1; }
uv tool install --force "$whl"
echo 'zsh completion: add  eval "$(localidraw completion)"  to ~/.zshrc'
