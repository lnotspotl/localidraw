# localidraw

Local Excalidraw: one tab per drawing, a folder gallery, offline-first sync to a Hugging Face bucket, and a CLI.

    export PATH="$HOME/localidraw/bin:$PATH"
    localidraw start      # build if needed, run in the background (or reuse), open http://localhost:3100
    localidraw stop       # stop it; it syncs before exiting
    localidraw status     # is the server up, and are drawings waiting to sync
    localidraw help       # every command

Drawings are saved to `~/.local/share/localidraw/` first and synced to `hf://buckets/kubjn7/localidraw` when online;
the bucket mirrors the gallery's folders. A drawing changed both here and in the bucket keeps the local copy and
saves the bucket's to `history/`.

The CLI takes elements as JSON on stdin, full Excalidraw elements or skeletons:

    echo '[{"type": "rectangle", "x": 0, "y": 0, "width": 200, "height": 100, "label": {"text": "Box"}}]' \
      | localidraw new "demo" --open

Config: `LOCALIDRAW_BUCKET`, `LOCALIDRAW_DATA`, `LOCALIDRAW_PORT`. Excalidraw changes live in `localidraw.patch`,
applied to a pinned upstream commit by `clone.sh`. Lint with `pixi run lint`.
