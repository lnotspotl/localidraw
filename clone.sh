#!/usr/bin/env bash
# Fetch the pinned upstream Excalidraw and apply the localidraw changes.
set -euo pipefail
cd "$(dirname "$0")"
[ -d excalidraw ] && exit 0
git clone -q git@github.com:excalidraw/excalidraw.git excalidraw
git -C excalidraw checkout -q dc2c16d9e2073078cb4b8f3dce942133b90eec3d
git -C excalidraw apply ../localidraw.patch
