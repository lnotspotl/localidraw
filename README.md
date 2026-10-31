# localidraw

Local Excalidraw: one tab per drawing, a folder gallery, offline-first sync to a Hugging Face bucket, and an agent-friendly CLI.

![localidraw: install, agent CLI, gallery, drawing](https://github.com/user-attachments/assets/56f0176c-1752-4bb1-a91a-e709ff32469c)

    curl -LsSf https://raw.githubusercontent.com/lnotspotl/localidraw/main/install.sh | sh   # uv if missing, then the latest release
    localidraw start      # run in the background (or reuse), open http://localhost:3100
    localidraw stop       # stop it; it syncs before exiting
    localidraw status     # is the server up, and are drawings waiting to sync
    localidraw help       # every command

Drawings are saved to `~/.local/share/localidraw/` first and synced to the Hugging Face bucket `LOCALIDRAW_BUCKET` when
online, given `HF_TOKEN` or `hf auth login`; without both, sync is off. The bucket mirrors the gallery's folders. A drawing changed both here and in the bucket keeps the local copy and
saves the bucket's to `history/`.

The CLI takes elements as JSON on stdin, full Excalidraw elements or skeletons:

    echo '[{"type": "rectangle", "x": 0, "y": 0, "width": 200, "height": 100, "label": {"text": "Box"}}]' \
      | localidraw new "demo" --open

Config: `LOCALIDRAW_BUCKET`, `LOCALIDRAW_DATA`, `LOCALIDRAW_PORT`. Excalidraw changes live in `localidraw.patch`,
applied to a pinned upstream commit by `clone.sh`. For development, put `bin/` on `PATH`; lint with `pixi run lint`.
`pixi run wheel` builds the wheel; pushing a `v<version>` tag matching `pyproject.toml` releases it.
