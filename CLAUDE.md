# localidraw

Local Excalidraw with a folder gallery, offline-first sync to a Hugging Face bucket, and a CLI. Python in
`localidraw.py`; the Excalidraw fork is `localidraw.patch` over a pinned upstream commit.

- `bin/localidraw` runs the CLI; `localidraw help` is the command tree, `localidraw serve` builds on first run.
- Test against throwaway state, never the real data or bucket:
  `LOCALIDRAW_DATA=$(mktemp -d) LOCALIDRAW_PORT=3700 LOCALIDRAW_BUCKET=nobody/none HF_HUB_OFFLINE=1`.
- The gallery is one HTML f-string in `gallery()`: braces are doubled, and a JS `\n` must be written `\\n`.
- Fork changes: edit in `excalidraw/`, then regenerate the patch with `git -C excalidraw add -A` and
  `git -C excalidraw diff --cached dc2c16d9 > localidraw.patch` (a plain diff misses new files), then `pixi run build`.
- Keep it minimal: no new dependencies, no speculative features.

## Workflow

- `pixi run lint` (ruff ALL, mypy strict) before every commit; `pixi run fmt` fixes what it can.
- Every feature is a PR into `main`, squash-merged. Commit as `lnotspotl <lnotspotl@gmail.com>`.
- Prove UI changes in headless Chrome against a throwaway server; say what wasn't checked with a real mouse.
