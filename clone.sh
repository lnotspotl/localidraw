#!/usr/bin/env bash
# Fetch the pinned upstream Excalidraw and apply the localidraw changes.
set -euo pipefail
cd "$(dirname "$0")"
[ -d excalidraw ] && exit 0
git init -q excalidraw
git -C excalidraw fetch -q --depth 1 https://github.com/excalidraw/excalidraw.git dc2c16d9e2073078cb4b8f3dce942133b90eec3d
git -C excalidraw checkout -q FETCH_HEAD
git -C excalidraw apply ../localidraw.patch
