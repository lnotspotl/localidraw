"""Local Excalidraw: a folder gallery, offline-first Hugging Face bucket sync, and a CLI (`localidraw help`).

Drawings live in DATA_DIR/drawings/<id>.excalidraw (+ .svg thumbnail); <id> is "<YYYYmmdd-HHMMSS>-<hostname>",
or the name of a bucket drawing saved before ids existed; ids reach the server URL-quoted.
"""

import argparse
import html
import json
import os
import queue
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast
from urllib.parse import parse_qs, quote, unquote, urlparse

if TYPE_CHECKING:
    from huggingface_hub import BucketFile, HfApi

WHEEL_BUILD_DIR = Path(__file__).parent / "localidraw_web"
BUILD_DIR = WHEEL_BUILD_DIR if WHEEL_BUILD_DIR.exists() else Path(__file__).parent / "excalidraw/excalidraw-app/build"
DATA_DIR = Path(os.environ.get("LOCALIDRAW_DATA", Path.home() / ".local/share/localidraw"))
DRAWINGS = DATA_DIR / "drawings"
HISTORY = DATA_DIR / "history"
STATE_FILE = DATA_DIR / "sync-state.json"
ORDER_FILE = DATA_DIR / "gallery-order.json"  # manual gallery order; unlisted drawings go first, newest first
FOLDERS_FILE = DATA_DIR / "folders.json"
PORT = int(os.environ.get("LOCALIDRAW_PORT", "3100"))
BUCKET = os.environ.get("LOCALIDRAW_BUCKET", "")
SYNC_OFF = "set LOCALIDRAW_BUCKET and HF_TOKEN (or hf auth login) to sync"
SYNC_EVERY = 60

Scene = dict[str, Any]


class RemoteInfo(TypedDict):
    hash: str
    path: str


class SyncState(TypedDict):
    dirty: list[str]
    remote: dict[str, RemoteInfo]
    deleted: list[str]


class Drawing(TypedDict):
    id: str
    name: str
    folder: str
    mtime: float
    status: str


class SyncStatus(TypedDict):
    online: bool | None
    last: float | None
    error: str | None


lock = threading.RLock()
sync_lock = threading.Lock()
wake_sync = threading.Event()
new_folder: dict[str, str] = {}
port_to_id: dict[int, str] = {}
exports: defaultdict[str, queue.Queue[bytes]] = defaultdict(queue.Queue)  # keyed by a per-export token
sync_status: SyncStatus = {"online": None, "last": None, "error": None}


# ---------------------------------------------------------------- storage


def new_id() -> str:
    base = f"{datetime.now():%Y%m%d-%H%M%S}-{socket.gethostname()}"
    drawing_id, n = base, 1
    while (DRAWINGS / f"{drawing_id}.excalidraw").exists():
        n += 1
        drawing_id = f"{base}-{n}"
    return drawing_id


def scene_path(drawing_id: str) -> Path:
    if "/" in drawing_id or drawing_id.startswith("."):
        raise ValueError(f"bad id: {drawing_id}")
    return DRAWINGS / f"{drawing_id}.excalidraw"


def read_scene(drawing_id: str) -> Scene:
    return cast("Scene", json.loads(scene_path(drawing_id).read_text()))


def history_path(drawing_id: str, kind: str) -> Path:
    """Where to keep an old copy of a drawing: history/<id>.<timestamp><kind>."""
    HISTORY.mkdir(exist_ok=True)
    return HISTORY / f"{drawing_id}.{datetime.now():%Y%m%d-%H%M%S}{kind}"


def write_scene(drawing_id: str, scene: Scene, svg: str | None = None) -> str:
    """Write a scene locally, mark it dirty for sync, and return its new revision."""
    path = scene_path(drawing_id)
    scene.setdefault("localidraw", {})["id"] = drawing_id
    with lock:
        old = read_scene(drawing_id) if path.exists() else None
        if old and live_elements(scene) == 0 and live_elements(old) > 0:
            # a save that wipes a whole drawing is kept but the old version is backed up
            shutil.copy2(path, history_path(drawing_id, ".before-empty.excalidraw"))
        elements_changed = old is None or old.get("elements") != scene.get("elements")
        path.write_text(json.dumps(scene, indent=2))
        svg_path = path.with_suffix(".svg")
        if svg is not None:
            svg_path.write_text(svg)
        elif elements_changed and svg_path.exists():
            svg_path.unlink()  # stale; the browser re-renders it on next open
        state = load_state()
        state["dirty"] = sorted(set(state["dirty"]) | {drawing_id})
        save_state(state)
    wake_sync.set()
    return revision(drawing_id)


def display_name(drawing_id: str, scene: Scene) -> str:
    return cast("str", (scene.get("appState") or {}).get("name") or drawing_id)


def name_taken(name: str, folder: str, except_id: str | None = None) -> bool:
    return any(d["name"] == name and d["folder"] == folder and d["id"] != except_id for d in drawings())


def taken_error(name: str, folder: str) -> str:
    return f'a drawing called "{name}" already exists in {folder or "the top level"}'


def remote_path(drawing_id: str) -> str:
    scene = read_scene(drawing_id)
    name = display_name(drawing_id, scene)
    folder = folder_of(scene)
    if name != drawing_id and name_taken(name, folder, drawing_id):
        name = f"{name} [{drawing_id}]"  # names are kept unique, but never lose data if they aren't
    return f"{folder + '/' if folder else ''}{name.replace('/', '∕')}.excalidraw"


def clean_folder(folder: str) -> str:
    return "/".join(p for p in (s.strip() for s in folder.split("/")) if p and p not in (".", ".."))


def folder_of(scene: Scene) -> str:
    return cast("str", scene.get("localidraw", {}).get("folder", ""))


def in_folder(folder: str, root: str) -> bool:
    return folder == root or folder.startswith(root + "/")


def saved_folders() -> set[str]:
    return set(json.loads(FOLDERS_FILE.read_text())) if FOLDERS_FILE.exists() else set()


def folders(items: list[Drawing]) -> set[str]:
    names = saved_folders() | {d["folder"] for d in items}
    return {"/".join(f.split("/")[: i + 1]) for f in names if f for i in range(f.count("/") + 1)}


def set_folder(drawing_id: str, folder: str) -> None:
    scene = read_scene(drawing_id)
    scene.setdefault("localidraw", {})["folder"] = folder
    write_scene(drawing_id, scene)


def move_folder(old: str, new: str) -> None:
    def moved(f: str) -> str:
        return new + f[len(old) :] if in_folder(f, old) else f

    for d in drawings():
        if in_folder(d["folder"], old):
            set_folder(d["id"], moved(d["folder"]))
    FOLDERS_FILE.write_text(json.dumps(sorted(moved(f) for f in saved_folders())))


def delete_drawing(drawing_id: str) -> None:
    """Remove a drawing here now and from the bucket on the next sync; keep a copy in history/."""
    path = scene_path(drawing_id)
    with lock:
        shutil.copy2(path, history_path(drawing_id, ".deleted.excalidraw"))
        path.unlink()
        path.with_suffix(".svg").unlink(missing_ok=True)
        state = load_state()
        state["dirty"] = [d for d in state["dirty"] if d != drawing_id]
        state["deleted"] = sorted(set(state["deleted"]) | {drawing_id})
        save_state(state)
    wake_sync.set()


def svg_path_of(path: str) -> str:
    return path.removesuffix(".excalidraw") + ".svg"


def live_elements(scene: Scene) -> int:
    return sum(not e.get("isDeleted") for e in scene.get("elements", []))


def empty_scene(name: str | None) -> Scene:
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "localidraw",
        "elements": [],
        "appState": {"name": name, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }


def revision(drawing_id: str) -> str:
    path = scene_path(drawing_id)
    return str(path.stat().st_mtime_ns) if path.exists() else "0"


def load_state() -> SyncState:
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("dirty", [])
    state.setdefault("remote", {})  # id -> {"hash", "path"}: bucket content hash and path at last sync
    state.setdefault("deleted", [])  # deleted here, still to be removed from the bucket
    return cast("SyncState", state)


def save_state(state: SyncState) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def drawings() -> list[Drawing]:
    """All drawings in gallery order."""
    state = load_state()
    items: dict[str, Drawing] = {}
    for path in DRAWINGS.glob("*.excalidraw"):
        scene = json.loads(path.read_text())
        items[path.stem] = {
            "id": path.stem,
            "name": display_name(path.stem, scene),
            "folder": folder_of(scene),
            "mtime": path.stat().st_mtime,
            "status": "pending" if path.stem in state["dirty"] else "synced",
        }
    order = json.loads(ORDER_FILE.read_text()) if ORDER_FILE.exists() else []
    rank = {drawing_id: i for i, drawing_id in enumerate(order)}
    newest_first = sorted(items.values(), key=lambda d: d["mtime"], reverse=True)
    return sorted(newest_first, key=lambda d: rank.get(d["id"], -1))


# ---------------------------------------------------------------- sync


def hf() -> "HfApi":
    import warnings

    from huggingface_hub import HfApi
    from huggingface_hub.utils.tqdm import disable_progress_bars

    disable_progress_bars()
    warnings.filterwarnings("ignore", message=".*not found in bucket.*")  # CLI-made drawings have no thumbnail yet
    return HfApi()


def remote_files(api: "HfApi") -> dict[str, "BucketFile"]:
    return {
        f.path: f
        for f in api.list_bucket_tree(BUCKET, recursive=True)
        if f.type == "file" and f.path.endswith(".excalidraw")
    }


def sync_on() -> bool:
    from huggingface_hub import get_token

    return bool(BUCKET) and get_token() is not None


def sync() -> bool:
    """Push local changes and pull remote ones. Returns False when the bucket is unreachable.

    Conflicts (changed both here and in the bucket since the last sync) keep the local
    version and save the bucket's version to history/.
    """
    if not sync_on():
        return False
    with sync_lock:
        try:
            _sync(hf())
        except Exception as e:  # noqa: BLE001
            sync_status.update({"online": False, "error": str(e).splitlines()[0][:200]})
            return False
    sync_status.update({"online": True, "last": time.time(), "error": None})
    return True


def _sync(api: "HfApi") -> None:
    remote = remote_files(api)
    with lock:
        state = load_state()
    synced = state["remote"]  # id -> {"hash", "path"} as of the last sync

    # 1  deletions made here
    gone = [synced[d]["path"] for d in state["deleted"] if d in synced and synced[d]["path"] in remote]
    if gone:
        api.batch_bucket_files(BUCKET, delete=[*gone, *(svg_path_of(p) for p in gone)])
    with lock:
        state = load_state()
        for drawing_id in state["deleted"]:
            state["remote"].pop(drawing_id, None)
        state["deleted"] = []
        save_state(state)
    remote = {p: f for p, f in remote.items() if p not in gone}

    # 2  pull everything new or changed, so all drawings are available offline
    by_path = {info["path"]: d for d, info in synced.items()}
    for rpath, r in remote.items():
        known = by_path.get(rpath)
        if known and r.xet_hash == synced[known].get("hash"):
            continue
        tmp = DATA_DIR / "downloads" / "incoming"
        download(api, rpath, tmp)
        drawing_id = (
            json.loads(Path(f"{tmp}.excalidraw").read_text()).get("localidraw", {}).get("id") or Path(rpath).stem
        )
        path = scene_path(drawing_id)
        with lock:
            state = load_state()
            if drawing_id in state["dirty"] or drawing_id in state["deleted"]:
                continue  # changed here too; handled below / next round
            if path.exists():
                shutil.copy2(path, history_path(drawing_id, ".excalidraw"))
            Path(f"{tmp}.excalidraw").replace(path)
            if Path(f"{tmp}.svg").exists():
                Path(f"{tmp}.svg").replace(path.with_suffix(".svg"))
            state["remote"][drawing_id] = {"hash": r.xet_hash, "path": rpath}
            save_state(state)
            synced[drawing_id] = state["remote"][drawing_id]

    # 3  drawings synced before but no longer in the bucket were deleted on another machine
    present = {info["path"] for info in synced.values() if info["path"] in remote}
    with lock:
        state = load_state()
        for drawing_id, info in list(state["remote"].items()):
            path = scene_path(drawing_id)
            if info["path"] in present or drawing_id in state["dirty"]:
                continue
            if path.exists():
                shutil.move(path, history_path(drawing_id, ".deleted-elsewhere.excalidraw"))
                path.with_suffix(".svg").unlink(missing_ok=True)
            del state["remote"][drawing_id]
        save_state(state)
        synced = state["remote"]
        dirty = set(state["dirty"])

    # 4  upload anything edited here, new, renamed (bucket path no longer matches its name),
    #    or whose thumbnail the bucket lacks
    remote_thumbs = {f.path for f in api.list_bucket_tree(BUCKET, recursive=True) if f.path.endswith(".svg")}
    upload: dict[str, tuple[str, int]] = {}
    for path in DRAWINGS.glob("*.excalidraw"):
        drawing_id = path.stem
        last = synced.get(drawing_id)
        target = remote_path(drawing_id)
        thumb_missing = path.with_suffix(".svg").exists() and svg_path_of(target) not in remote_thumbs
        if drawing_id not in dirty and last and last["path"] == target and not thumb_missing:
            continue
        current = remote.get(last["path"]) if last else None
        if drawing_id in dirty and last and current and current.xet_hash != last.get("hash"):
            download(api, last["path"], history_path(drawing_id, ".conflict"))
        upload[drawing_id] = (target, path.stat().st_mtime_ns)
    if not upload:
        return
    add: list[tuple[str | Path | bytes, str]] = []
    delete: list[str] = []
    for drawing_id, (target, _) in upload.items():
        path = scene_path(drawing_id)
        add.append((path.read_bytes(), target))
        if path.with_suffix(".svg").exists():
            add.append((path.with_suffix(".svg").read_bytes(), svg_path_of(target)))
        old = synced[drawing_id]["path"] if drawing_id in synced else None
        if old and old != target and old in remote:
            delete += [old, svg_path_of(old)]
    api.batch_bucket_files(BUCKET, add=add, delete=delete or None)
    remote = remote_files(api)
    with lock:
        state = load_state()
        for drawing_id, (target, uploaded_mtime) in upload.items():
            state["remote"][drawing_id] = {"hash": remote[target].xet_hash, "path": target}
            # keep it dirty if it was edited again while uploading
            if scene_path(drawing_id).stat().st_mtime_ns == uploaded_mtime and drawing_id in state["dirty"]:
                state["dirty"].remove(drawing_id)
        save_state(state)


def download(api: "HfApi", rpath: str, dest: Path) -> None:
    """Download a bucket drawing and its thumbnail to <dest>.excalidraw / <dest>.svg."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for f in (Path(f"{dest}.excalidraw"), Path(f"{dest}.svg")):
        f.unlink(missing_ok=True)
    api.download_bucket_files(BUCKET, [(rpath, f"{dest}.excalidraw"), (svg_path_of(rpath), f"{dest}.svg")])


def sync_loop() -> None:
    while True:
        wake_sync.wait(SYNC_EVERY)
        time.sleep(3)  # batch bursts of edits
        wake_sync.clear()
        sync()


# ---------------------------------------------------------------- server


def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def start_instance(drawing_id: str | None = None) -> int:
    """Serve a drawing on its own port (reusing one if it's already open)."""
    with lock:
        for port, open_id in port_to_id.items():
            if open_id == drawing_id:
                return port
        drawing_id = drawing_id or new_id()
        port = PORT + 1
        while port in port_to_id or not port_is_free(port):
            port += 1
        server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=BUILD_DIR))
        port_to_id[port] = drawing_id
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


def gallery(folder: str) -> str:
    labels = {"synced": "✓ synced", "pending": "↑ waiting to sync"}
    items = drawings()
    entries: list[str] = []
    parent: list[str] = []
    if folder:
        parent.append(
            f'<div class="item folder parent" data-folder="{html.escape(folder.rpartition("/")[0])}" '
            f'data-in="{html.escape(folder)}" data-name="..">'
            f'<div class="icon"><svg viewBox="0 0 64 52"><use href="#folder"/></svg></div>'
            f'<span class="name">..</span><small></small><small>Parent folder</small></div>'
        )
    rest: list[str] = []  # the other folders' items, hidden until a search needs them
    for path in sorted(folders(items), key=str.lower):
        parent_path, _, name = path.rpartition("/")
        (entries if parent_path == folder else rest).append(
            f'<div class="item folder" draggable="true" data-folder="{html.escape(path)}" '
            f'data-in="{html.escape(parent_path)}" data-name="{html.escape(name)}"'
            f"{'' if parent_path == folder else ' hidden'}>"
            f'<div class="icon"><svg viewBox="0 0 64 52"><use href="#folder"/></svg></div>'
            f'<span class="name">{html.escape(name)}</span><small></small><small>Folder</small>'
            f'<small class="where">{html.escape(parent_path or "localidraw")}</small></div>'
        )
    for d in items:
        has_thumb = scene_path(d["id"]).with_suffix(".svg").exists()
        edited = datetime.fromtimestamp(d["mtime"]).strftime("%Y-%m-%d %H:%M")
        drawing_id, name = html.escape(d["id"]), html.escape(d["name"])
        thumb = f'<img src="/thumb/{quote(d["id"])}?v={d["mtime"]}" loading="lazy">' if has_thumb else ""
        (entries if d["folder"] == folder else rest).append(
            f'<div class="item drawing" draggable="true" data-id="{drawing_id}" '
            f'data-in="{html.escape(d["folder"])}" data-name="{name}"'
            f"{'' if d['folder'] == folder else ' hidden'}>"
            f'<div class="icon">{thumb}</div><span class="name">{name}</span>'
            f"<small>{edited}</small><small>{labels[d['status']]}</small>"
            f'<small class="where">{html.escape(d["folder"] or "localidraw")}</small></div>'
        )
    crumbs = ['<a href="?folder=" data-folder="">localidraw</a>']
    parts = folder.split("/") if folder else []
    for i, part in enumerate(parts):
        path = "/".join(parts[: i + 1])
        crumbs.append(f'<a href="?folder={quote(path)}" data-folder="{html.escape(path)}">{html.escape(part)}</a>')
    s = sync_status
    status = (
        "not synced yet"
        if s["online"] is None
        else ("online" if s["online"] else f"offline ({html.escape(s['error'] or '')})")
    )
    if s["last"]:
        status += f" · last sync {datetime.fromtimestamp(s['last']):%H:%M:%S}"
    on = sync_on()
    status = f"{BUCKET} · {status}" if on else SYNC_OFF
    sync_buttons = (
        f'<button id="sync" onclick="syncNow()">Sync now</button>\n'
        f'<a href="https://huggingface.co/buckets/{BUCKET}" target="_blank"><button>Open bucket</button></a>'
        if on
        else ""
    )
    return f"""<!doctype html><meta charset="utf-8"><title>{html.escape(parts[-1] if parts else "localidraw")}</title>
<style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#1e1e1e;--line:#e5e5e5;--border:#d0d0d0;--hover:#f0f0f0;
--hi:#e5f3ff;--sel:#cce8ff;--sel-border:#99d1ff;--muted:#666;--faint:#888;--shadow:#0003;--thumb:none}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121212;--fg:#e3e3e3;--line:#2e2e2e;--border:#444;--hover:#2a2a2a;
--hi:#1d3046;--sel:#1f4266;--sel-border:#2f6aa3;--muted:#a0a0a0;--shadow:#0009;
--thumb:invert(93%) hue-rotate(180deg)}}}}
body{{font-family:system-ui;margin:0;background:var(--bg);color:var(--fg);user-select:none}}
.bar{{display:flex;gap:.5rem;align-items:center;padding:.5rem 1rem;border-bottom:1px solid var(--line);flex-wrap:wrap}}
.bar button,.bar select{{font:inherit;font-size:.9rem;padding:.3rem .7rem;border:1px solid var(--border);background:var(--bg);color:inherit;border-radius:4px;cursor:pointer}}
.bar button:hover{{background:var(--hover)}}
.bar button[aria-pressed=true]{{background:var(--sel);border-color:var(--sel-border)}}
#up{{padding:.3rem .5rem;line-height:0}}
#up:disabled{{opacity:.35;cursor:default}}
nav{{flex:1;display:flex;align-items:center;border:1px solid var(--border);border-radius:4px;padding:.25rem .5rem;min-width:16rem}}
.bar a{{color:inherit}}
nav a{{text-decoration:none;padding:.1rem .4rem;border-radius:3px}}
nav a:hover,nav a.drop{{background:var(--hi)}}
nav a+a::before{{content:"›";color:var(--faint);margin-right:.6rem}}
input[type=search]{{font:inherit;font-size:.9rem;padding:.3rem .6rem;border:1px solid var(--border);border-radius:4px;width:14rem}}
main{{padding:1rem;min-height:calc(100vh - 8rem)}}
footer{{position:sticky;bottom:0;background:var(--bg);padding:.3rem 1rem;border-top:1px solid var(--line);font-size:.8rem;color:var(--muted)}}
.spinner{{display:none;width:.7rem;height:.7rem;margin-right:.35rem;vertical-align:-1px;border:2px solid var(--sel);border-top-color:#1971c2;border-radius:50%;animation:spin .8s linear infinite}}
.syncing .spinner{{display:inline-block}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
#grid{{display:grid;gap:.25rem;align-content:start}}
.item{{display:flex;flex-direction:column;align-items:center;gap:.3rem;padding:.5rem;border:1px solid transparent;border-radius:3px;cursor:default}}
.item:hover{{background:var(--hi)}}
.item.selected{{background:var(--sel);border-color:var(--sel-border)}}
.item.drop{{background:var(--sel);border-color:#1971c2}}
.item.dragging{{opacity:.4}}
.item[hidden]{{display:none}}
.icon{{display:flex;align-items:center;justify-content:center;overflow:hidden}}
.icon img{{max-width:100%;max-height:100%;background:#fff;box-shadow:0 0 0 1px var(--line);filter:var(--thumb)}}
.icon svg{{width:70%;height:70%}}
.name{{text-align:center;word-break:break-word;font-size:.9rem}}
.item small{{display:none;color:var(--muted);font-size:.8rem}}
.large #grid{{grid-template-columns:repeat(auto-fill,minmax(250px,1fr))}}
.large .icon{{width:100%;height:180px}}
.medium #grid{{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}}
.medium .icon{{width:100%;height:100px}}
.small #grid,.details #grid{{grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}}
.small .item,.details .item{{flex-direction:row;padding:.2rem .4rem}}
.small .icon,.details .icon{{width:20px;height:20px;flex:none}}
.small .icon svg,.details .icon svg{{width:100%;height:100%}}
.small .name,.details .name{{text-align:left;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.details #grid{{grid-template-columns:1fr;gap:0}}
.details .item small{{display:block;width:10rem}}
.cols{{display:none}}
.details .cols{{display:flex;gap:.3rem;padding:.2rem .45rem;font-size:.8rem;color:var(--muted);border-bottom:1px solid var(--line);margin-bottom:.2rem}}
.cols span{{width:10rem}}
.cols span:first-child{{flex:1;padding-left:26px}}
#peek{{position:fixed;display:none;background:var(--bg);border:1px solid var(--border);border-radius:4px;box-shadow:0 4px 16px var(--shadow);
padding:.5rem;z-index:5;pointer-events:none}}
#marquee{{position:fixed;display:none;border:1px solid #1971c2;background:#1971c233;pointer-events:none;z-index:4}}
#peek img{{display:block;max-width:420px;max-height:320px;filter:var(--thumb)}}
#menu{{position:fixed;display:none;flex-direction:column;background:var(--bg);border:1px solid var(--border);border-radius:4px;
box-shadow:0 4px 12px var(--shadow);padding:.25rem;z-index:10;min-width:10rem}}
#menu button{{font:inherit;font-size:.9rem;text-align:left;border:0;background:none;color:inherit;padding:.35rem .8rem;border-radius:3px;cursor:pointer}}
#menu button:hover{{background:var(--hi)}}
.empty{{color:var(--faint)}}
.where,.searching .empty{{display:none!important}}
.searching .where{{display:block!important}}
</style>
<script>document.documentElement.className = localStorage.view || "details"</script>
<svg style="display:none"><symbol id="folder" viewBox="0 0 64 52">
<path d="M2 6a4 4 0 0 1 4-4h17l6 6h29a4 4 0 0 1 4 4v34a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4z" fill="#dcaa3c"/>
<path d="M2 15a3 3 0 0 1 3-3h54a3 3 0 0 1 3 3v31a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4z" fill="#f6d36b"/></symbol></svg>
<div class="bar">
<button id="up" title="Up (Backspace)" onclick="up()"><svg width="16" height="16" viewBox="0 0 16 16"><path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.6"/></svg></button>
<nav>{"".join(crumbs)}</nav>
<input type="search" id="q" placeholder="Search all folders" oninput="search(this.value)">
</div>
<div class="bar">
<button onclick="newDrawing()">New drawing</button>
<button onclick="newFolder()">New folder</button>
<select id="view" onchange="setView(this.value)">
<option value="large">Large icons</option><option value="medium">Medium icons</option>
<option value="small">Small icons</option><option value="details">Details</option></select>
<button id="peektoggle" title="Show a larger preview on hover" onclick="setPeek(localStorage.peek === 'off')">Hover preview</button>
{sync_buttons}
</div>
<main>
<div class="cols"><span>Name</span><span>Date modified</span><span>Status</span><span class="where">Folder</span></div>
<div id="grid">{"".join(parent + entries + rest)}{"" if entries else '<p class="empty">Right-click for a new drawing or folder.</p>'}</div>
</main>
<footer>{len(entries)} item{"" if len(entries) == 1 else "s"} · <span id="syncstatus"><span class="spinner"></span><span>{status}</span></span> · double-click to open, Ctrl/Shift-click to select several, right-click for more, drag to move</footer>
<div id="menu"></div>
<div id="peek"><img></div>
<div id="marquee"></div>
<script>
const folder = {json.dumps(folder)};
const post = (url, data) => fetch(url, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(data)}});
const item = el => el?.closest?.(".item");
const grid = document.getElementById("grid"), menu = document.getElementById("menu");
const go = f => location.search = "?folder=" + encodeURIComponent(f);
document.getElementById("view").value = document.documentElement.className;
document.getElementById("up").disabled = !folder;
async function syncNow() {{
  const button = document.getElementById("sync"), box = document.getElementById("syncstatus"), label = box.lastChild;
  button.disabled = true;
  box.classList.add("syncing");
  label.textContent = "syncing with {BUCKET}…";
  const {{synced}} = await (await post("/sync", {{}})).json();
  box.classList.remove("syncing");
  if (synced) {{ label.textContent = "synced"; setTimeout(() => location.reload(), 600); return; }}
  const {{error}} = await (await fetch("/api/status")).json();
  label.textContent = "offline: " + (error || "bucket unreachable") + ", changes stay local";
  button.disabled = false;
}}
function setView(v) {{ document.documentElement.className = localStorage.view = v; }}
function setPeek(on) {{ localStorage.peek = on ? "on" : "off"; document.getElementById("peektoggle").ariaPressed = on; }}
setPeek(localStorage.peek !== "off");
function up() {{ if (folder) go(folder.split("/").slice(0, -1).join("/")); }}
const selected = () => [...document.querySelectorAll(".item.selected")];
const visible = () => [...grid.querySelectorAll(".item:not(.parent):not([hidden])")];
let anchor = null;
function select(el) {{
  selected().forEach(e => e.classList.remove("selected"));
  el?.classList.add("selected");
  anchor = el;
}}
function pick(e, el) {{
  const all = visible(), i = all.indexOf(anchor), j = all.indexOf(el);
  if (!el) {{ if (!e.ctrlKey && !e.metaKey) select(null); }}
  else if (e.ctrlKey || e.metaKey) {{ el.classList.toggle("selected"); anchor = el; }}
  else if (e.shiftKey && i >= 0 && j >= 0) {{
    selected().forEach(e => e.classList.remove("selected"));
    all.slice(Math.min(i, j), Math.max(i, j) + 1).forEach(e => e.classList.add("selected"));
  }}
  else select(el);
}}
async function openItem(el) {{
  if (el.classList.contains("folder")) return go(el.dataset.folder);
  const w = window.open("", "_blank");
  w.location = (await (await post("/open", {{id: el.dataset.id}})).json()).url;
}}
async function newDrawing() {{
  const w = window.open("", "_blank");
  w.location = (await (await post("/new", {{folder}})).json()).url;
}}
async function newFolder() {{
  const name = prompt("New folder");
  if (name?.trim()) {{ await post("/folder/new", {{folder: (folder ? folder + "/" : "") + name}}); location.reload(); }}
}}
async function rename(el) {{
  const old = el.dataset.name, name = prompt("Rename", old)?.trim();
  if (!name || name === old) return;
  const r = el.classList.contains("folder")
    ? await post("/folder/rename", {{folder: el.dataset.folder, to: (el.dataset.in ? el.dataset.in + "/" : "") + name.replaceAll("/", "∕")}})
    : await post("/rename", {{id: el.dataset.id, name}});
  r.ok ? location.reload() : alert((await r.json()).error);
}}
async function remove(els) {{
  els = els.filter(el => !el.classList.contains("parent"));
  const what = els.length === 1 ? `"${{els[0].dataset.name}}"` : `${{els.length}} items`;
  if (els.some(el => el.classList.contains("drawing"))
    && !confirm(`Delete ${{what}}?\n\nIt's removed here and from the Hugging Face bucket; a copy stays in history/.`)) return;
  const errors = [];
  for (const el of els) {{
    const r = el.classList.contains("folder")
      ? await post("/folder/delete", {{folder: el.dataset.folder}})
      : await post("/delete", {{id: el.dataset.id}});
    r.ok ? el.remove() : errors.push(`${{el.dataset.name}}: ${{(await r.json()).error}}`);
  }}
  if (errors.length) alert(errors.join("\\n"));
}}
function search(q) {{
  const query = q.trim().toLowerCase();
  document.documentElement.classList.toggle("searching", !!query);
  select(null);
  for (const el of grid.querySelectorAll(".item"))
    el.hidden = query ? el.classList.contains("parent") || !el.dataset.name.toLowerCase().includes(query) : el.dataset.in !== folder;
  const url = new URL(location);
  query ? url.searchParams.set("q", q) : url.searchParams.delete("q");
  history.replaceState(null, "", url);
}}
const main = document.querySelector("main"), marquee = document.getElementById("marquee");
let box = null, swallowClick = false;
main.addEventListener("click", e => {{ if (!swallowClick) pick(e, item(e.target)); }});
main.addEventListener("mousedown", e => {{
  if (e.button !== 0 || item(e.target)) return;
  if (!e.ctrlKey && !e.metaKey) select(null);
  box = {{x: e.clientX, y: e.clientY, keep: selected(), moved: false}};
}});
document.addEventListener("mousemove", e => {{
  if (!box) return;
  box.moved = true;
  const l = Math.min(box.x, e.clientX), t = Math.min(box.y, e.clientY), r = Math.max(box.x, e.clientX), b = Math.max(box.y, e.clientY);
  Object.assign(marquee.style, {{display: "block", left: l + "px", top: t + "px", width: r - l + "px", height: b - t + "px"}});
  for (const el of visible()) {{
    const q = el.getBoundingClientRect();
    el.classList.toggle("selected", box.keep.includes(el) || (q.left < r && q.right > l && q.top < b && q.bottom > t));
  }}
}});
document.addEventListener("mouseup", () => {{
  if (!box) return;
  swallowClick = box.moved;
  box = null;
  marquee.style.display = "none";
}});
grid.addEventListener("dblclick", e => item(e.target) && openItem(item(e.target)));
document.addEventListener("keydown", e => {{
  if (e.target.matches?.("input")) return;
  const sel = selected(), el = sel.length === 1 ? sel[0] : null;
  if (e.key === "Backspace") up();
  else if (e.key === "Escape") menu.style.display = "none";
  else if ((e.ctrlKey || e.metaKey) && e.key === "a") {{ e.preventDefault(); select(null); visible().forEach(e => e.classList.add("selected")); }}
  else if (el && e.key === "Enter") openItem(el);
  else if (el && e.key === "F2" && !el.classList.contains("parent")) {{ e.preventDefault(); rename(el); }}
  else if (sel.length && e.key === "Delete") remove(sel);
}});
document.addEventListener("click", () => menu.style.display = "none");
document.addEventListener("contextmenu", e => {{
  if (e.target.matches?.("input")) return;
  e.preventDefault();
  const el = item(e.target);
  if (!el?.classList.contains("selected")) select(el);
  const sel = selected();
  const actions = el?.classList.contains("parent")
    ? [["Open", () => openItem(el)]]
    : el
    ? [["Open", () => openItem(el)], ...(sel.length === 1 ? [["Rename", () => rename(el)]] : []), ["Delete", () => remove(sel)]]
    : [["New drawing", newDrawing], ["New folder", newFolder]];
  menu.replaceChildren(...actions.map(([label, run]) => {{
    const b = document.createElement("button");
    b.textContent = label;
    b.onclick = run;
    return b;
  }}));
  menu.style.display = "flex";
  menu.style.left = Math.min(e.clientX, innerWidth - menu.offsetWidth - 4) + "px";
  menu.style.top = Math.min(e.clientY, innerHeight - menu.offsetHeight - 4) + "px";
}});
const peek = document.getElementById("peek");
let peekTimer = null;
grid.addEventListener("mouseover", e => {{
  const el = item(e.target), img = el?.querySelector("img");
  if (!img || el.contains(e.relatedTarget) || localStorage.peek === "off") return;
  clearTimeout(peekTimer);
  peekTimer = setTimeout(() => {{
    if (dragged || menu.style.display === "flex") return;
    peek.firstChild.src = img.src;
    peek.style.display = "block";
    const r = el.getBoundingClientRect(), w = peek.offsetWidth, h = peek.offsetHeight;
    peek.style.left = (r.right + 8 + w < innerWidth ? r.right + 8 : Math.max(4, r.left - 8 - w)) + "px";
    peek.style.top = Math.max(4, Math.min(r.top, innerHeight - h - 4)) + "px";
  }}, 300);
}});
grid.addEventListener("mouseout", e => {{
  if (item(e.target)?.contains(e.relatedTarget)) return;
  clearTimeout(peekTimer);
  peek.style.display = "none";
}});
document.addEventListener("mousedown", () => {{ clearTimeout(peekTimer); peek.style.display = "none"; swallowClick = false; }});
let dragged = null;
const target = el => el?.closest?.(".folder, nav a");
async function moveInto(dest) {{
  const inside = (f, root) => f === root || f.startsWith(root + "/");
  const els = dragged.filter(el => el.dataset.in !== dest && !(el.dataset.folder && inside(dest, el.dataset.folder))), errors = [];
  if (!els.length) return;
  for (const el of els) {{
    const r = el.classList.contains("folder")
      ? await post("/folder/rename", {{folder: el.dataset.folder, to: (dest ? dest + "/" : "") + el.dataset.name}})
      : await post("/move", {{id: el.dataset.id, folder: dest}});
    if (!r.ok) errors.push(`${{el.dataset.name}}: ${{(await r.json()).error}}`);
  }}
  if (errors.length) alert(errors.join("\\n"));
  location.reload();
}}
document.addEventListener("dragstart", e => {{
  const el = item(e.target);
  if (!el) return;
  if (!el.classList.contains("selected")) select(el);
  dragged = selected().filter(el => !el.classList.contains("parent"));
  dragged.forEach(el => el.classList.add("dragging"));
}});
document.addEventListener("dragover", e => {{
  if (!dragged) return;
  e.preventDefault();
  document.querySelectorAll(".drop").forEach(el => el.classList.remove("drop"));
  const t = target(e.target);
  if (t && !dragged.includes(t)) return t.classList.add("drop");
  const over = item(e.target), [one] = dragged;
  if (!over || dragged.length !== 1 || over === one || !one.classList.contains("drawing") || document.documentElement.classList.contains("searching")) return;
  const r = over.getBoundingClientRect();
  const after = document.documentElement.className === "details" ? e.clientY > r.top + r.height / 2 : e.clientX > r.left + r.width / 2;
  grid.insertBefore(one, after ? over.nextSibling : over);
}});
document.addEventListener("drop", e => {{
  e.preventDefault();
  const t = target(e.target);
  if (dragged && t && !dragged.includes(t)) moveInto(t.dataset.folder);
}});
document.addEventListener("dragend", () => {{
  document.querySelectorAll(".drop").forEach(el => el.classList.remove("drop"));
  dragged?.forEach(el => el.classList.remove("dragging"));
  if (dragged?.length === 1 && dragged[0].isConnected && dragged[0].classList.contains("drawing"))
    post("/order", {{ids: [...grid.querySelectorAll(".drawing")].map(el => el.dataset.id)}});
  dragged = null;
}});
const startQuery = new URLSearchParams(location.search).get("q");
if (startQuery) {{ document.getElementById("q").value = startQuery; search(startQuery); }}
</script>"""  # noqa: E501, S608


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def send_json(self, data: object, status: int = 200) -> None:
        self.send_body(json.dumps(data), "application/json", status)

    def send_body(self, body: str | bytes, content_type: str, status: int = 200) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, url: str) -> None:
        self.send_response(303)
        self.send_header("Location", url)
        self.end_headers()

    def read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def read_json(self) -> Any:  # noqa: ANN401
        return json.loads(self.read_body() or b"{}")

    @property
    def drawing_id(self) -> str | None:
        return port_to_id.get(cast("tuple[str, int]", self.server.server_address)[1])

    def do_GET(self) -> None:
        url = urlparse(self.path)
        route = unquote(url.path)  # an id can hold spaces and non-ASCII
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        # the gallery port serves the gallery and the API; instance ports serve the app
        if self.drawing_id is None:
            if route == "/":
                return self.redirect("/localidraw/gallery")
            if route == "/localidraw/gallery":
                return self.send_body(gallery(clean_folder(query.get("folder", ""))), "text/html; charset=utf-8")
            if route == "/api/drawings":
                return self.send_json(drawings())
            if route.startswith("/api/drawings/"):
                drawing_id = route.removeprefix("/api/drawings/")
                return self.send_json(read_scene(drawing_id))
            if route.startswith("/thumb/"):
                svg_path = scene_path(route.removeprefix("/thumb/")).with_suffix(".svg")
                if not svg_path.exists():
                    return self.send_error(404)
                data = svg_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None
            if route == "/api/status":
                return self.send_json({**sync_status, "bucket": BUCKET, "open": port_to_id})
            return self.send_error(404)
        if route == "/localidraw/gallery":
            path = scene_path(self.drawing_id)
            folder = folder_of(read_scene(self.drawing_id)) if path.exists() else new_folder.get(self.drawing_id, "")
            return self.redirect(f"http://localhost:{PORT}/localidraw/gallery?folder={quote(folder)}")
        if route == "/localidraw/scene":
            rev = revision(self.drawing_id)
            if query.get("rev") == rev:
                return self.send_body("", "text/plain", 204)
            path = scene_path(self.drawing_id)
            scene = read_scene(self.drawing_id) if path.exists() else None
            return self.send_json({"rev": rev, "scene": scene, "hasThumb": path.with_suffix(".svg").exists()})
        if route.startswith("/localidraw/export/"):
            token = route.removeprefix("/localidraw/export/")
            try:
                return self.send_body(exports[token].get(timeout=60), "application/octet-stream")
            except queue.Empty:
                return self.send_error(504)
            finally:
                exports.pop(token, None)
        return super().do_GET()

    def do_POST(self) -> None:
        url = unquote(urlparse(self.path).path)
        # served on every port: the app's menu and Ctrl+S post to its own instance port
        if url in ("/new", "/localidraw/new"):
            data = self.read_json()
            drawing_id = new_id()
            folder = clean_folder(data.get("folder", ""))
            if data.get("name") and name_taken(data["name"], folder):
                return self.send_json({"error": taken_error(data["name"], folder)}, 409)
            if data.get("elements") or data.get("name"):
                scene = empty_scene(data.get("name"))
                scene["elements"] = data.get("elements", [])
                scene["localidraw"] = {"folder": folder}
                write_scene(drawing_id, scene)
            elif folder:
                new_folder[drawing_id] = folder
            return self.send_json({"id": drawing_id, "url": f"http://localhost:{start_instance(drawing_id)}"})
        if url in ("/open", "/localidraw/open"):
            return self.send_json({"url": f"http://localhost:{start_instance(self.read_json()['id'])}"})
        if url in ("/sync", "/localidraw/sync"):
            return self.send_json({"synced": sync()})
        if self.drawing_id:
            if url.startswith("/localidraw/export/"):
                exports[url.removeprefix("/localidraw/export/")].put(self.read_body())
                return self.send_body("", "text/plain", 204)
            if url != "/localidraw/save":
                return self.send_error(404)
            data = self.read_json()
            # a tab that never loaded this drawing (e.g. left open from an earlier
            # server run, when this port held another drawing) must not overwrite it
            if data.get("rev") != revision(self.drawing_id):
                return self.send_json({"rev": revision(self.drawing_id)}, 409)
            scene = json.loads(data["scene"])
            scene["appState"]["name"] = data.get("name")
            path = scene_path(self.drawing_id)
            folder = folder_of(read_scene(self.drawing_id)) if path.exists() else new_folder.pop(self.drawing_id, "")
            scene["localidraw"] = {"folder": folder}
            return self.send_json({"rev": write_scene(self.drawing_id, scene, data.get("svg"))})
        # the gallery port: the gallery's and the CLI's API
        data = self.read_json()
        if url == "/delete":
            delete_drawing(data["id"])
            return self.send_json({})
        if url == "/rename":
            scene = read_scene(data["id"])
            if name_taken(data["name"], folder_of(scene), data["id"]):
                return self.send_json({"error": taken_error(data["name"], folder_of(scene))}, 409)
            scene.setdefault("appState", {})["name"] = data["name"]
            return self.send_json({"rev": write_scene(data["id"], scene)})
        if url == "/order":
            ids = data["ids"]  # one folder's drawings
            old = json.loads(ORDER_FILE.read_text()) if ORDER_FILE.exists() else []
            ORDER_FILE.write_text(json.dumps(ids + [i for i in old if i not in ids]))
            return self.send_json({})
        if url == "/move":
            folder, name = clean_folder(data["folder"]), display_name(data["id"], read_scene(data["id"]))
            if name_taken(name, folder, data["id"]):
                return self.send_json({"error": taken_error(name, folder)}, 409)
            set_folder(data["id"], folder)
            return self.send_json({})
        if url == "/folder/new":
            FOLDERS_FILE.write_text(json.dumps(sorted(saved_folders() | {clean_folder(data["folder"])} - {""})))
            return self.send_json({})
        if url == "/folder/rename":
            to = clean_folder(data["to"])
            if not to:
                return self.send_json({"error": "a folder needs a name"}, 400)
            if to in folders(drawings()):
                parent, _, name = to.rpartition("/")
                return self.send_json(
                    {"error": f'a folder called "{name}" already exists in {parent or "the top level"}'}, 409
                )
            move_folder(data["folder"], to)
            return self.send_json({})
        if url == "/folder/delete":
            old = data["folder"]
            if any(in_folder(d["folder"], old) for d in drawings()):
                return self.send_json(
                    {"error": "only empty folders can be deleted; move or delete its drawings first"}, 409
                )
            FOLDERS_FILE.write_text(json.dumps(sorted(f for f in saved_folders() if not in_folder(f, old))))
            return self.send_json({})
        if url.startswith("/api/drawings/"):
            return self.send_json({"rev": write_scene(url.removeprefix("/api/drawings/"), data)})
        return self.send_error(404)


def ensure_build() -> None:
    if not (BUILD_DIR / "index.html").exists():
        subprocess.run(
            ["pixi", "run", "--manifest-path", str(Path(__file__).parent / "pixi.toml"), "build"], check=True
        )


def serve(*, open_browser: bool) -> None:
    ensure_build()
    gallery_server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=BUILD_DIR))
    threading.Thread(target=gallery_server.serve_forever, daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()
    wake_sync.set()
    print(
        f"localidraw: http://localhost:{PORT}/localidraw/gallery  "
        f"({f'bucket {BUCKET}' if sync_on() else SYNC_OFF}, data {DATA_DIR})",
        flush=True,
    )
    if open_browser:
        webbrowser.open(f"http://localhost:{PORT}/localidraw/gallery")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        threading.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        if not sync_on():
            return
        print("syncing before exit...", flush=True)
        ok = sync()
        print(
            "synced" if ok else f"offline ({sync_status['error']}): changes stay local and sync next time", flush=True
        )


# ---------------------------------------------------------------- cli


def server_up() -> bool:
    return not port_is_free(PORT)


UNIT = f"localidraw-{PORT}"


def unit_active() -> bool:
    return subprocess.run(["systemctl", "--user", "--quiet", "is-active", UNIT], check=False).returncode == 0


def status_rows() -> list[dict[str, Any]]:
    up, dirty = server_up(), len(load_state()["dirty"])
    origin = f"({UNIT})" if unit_active() else "(not started by localidraw start)"
    return [
        {
            "name": "server",
            "on": up,
            "detail": f"http://localhost:{PORT}/localidraw/gallery {origin}" if up else "not running",
        },
        {"name": "sync", "on": False, "detail": SYNC_OFF}
        if not sync_on()
        else {"name": "sync", "on": dirty > 0, "detail": f"{dirty} drawings pending sync" if dirty else "all synced"},
    ]


def start(*, open_browser: bool) -> None:
    """Run the server as a systemd user service, or reuse the one already on PORT."""
    if server_up():
        print(f"localidraw: already running on http://localhost:{PORT}/localidraw/gallery")
    else:
        ensure_build()
        # systemd starts services with an empty environment; pass on config, PATH and custom CA bundles
        passed = ("LOCALIDRAW_", "HF_", "SSL_CERT_", "REQUESTS_CA_BUNDLE", "PATH")
        env = [f"--setenv={k}={v}" for k, v in os.environ.items() if k.startswith(passed)]
        subprocess.run(
            [
                "systemd-run",
                "--user",
                f"--unit={UNIT}",
                f"--description=localidraw on :{PORT}",
                "--collect",
                "--quiet",
                *env,
                sys.executable,
                str(Path(__file__).resolve()),
                "serve",
                "--no-browser",
            ],
            check=True,
        )
        deadline = time.monotonic() + 10  # systemd-run returns at once; wait until the server accepts connections
        while not server_up():
            if time.monotonic() > deadline:
                sys.exit(f"localidraw: {UNIT} did not come up in 10s; see journalctl --user -u {UNIT}")
            time.sleep(0.1)
        print(f"localidraw: started {UNIT} on http://localhost:{PORT}/localidraw/gallery")
    if open_browser:
        webbrowser.open(f"http://localhost:{PORT}/localidraw/gallery")


def stop() -> None:
    if unit_active():
        subprocess.run(["systemctl", "--user", "stop", UNIT], check=True)
        print(f"localidraw: stopped {UNIT}; it syncs on exit, see journalctl --user -u {UNIT}")
    elif server_up():
        sys.exit(
            f"localidraw: the server on port {PORT} was not started with `localidraw start`; stop it where it runs"
        )
    else:
        print("localidraw: not running")


CHROMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def require_server() -> None:
    if not server_up():
        sys.exit("server not running; start it with `localidraw start`")


def export(drawing_id: str, file: Path) -> None:
    suffix = file.suffix.lower()
    if suffix not in (".svg", ".png"):
        sys.exit(f"localidraw: can't export to {file.name}; use a .svg or .png file")
    require_server()
    if not scene_path(drawing_id).exists():
        sys.exit(f"localidraw: no drawing {drawing_id}")
    chrome = next(filter(None, map(shutil.which, CHROMES)), None)
    if chrome is None:
        sys.exit("localidraw: export needs Chrome or Chromium, and neither was found")
    url, token = call("POST", "/open", {"id": drawing_id})["url"], os.urandom(8).hex()
    with (
        tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile,
        subprocess.Popen(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                f"--user-data-dir={profile}",
                f"{url}/?export={suffix[1:]}&token={token}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as browser,
    ):
        try:
            with urllib.request.urlopen(f"{url}/localidraw/export/{token}", timeout=90) as response:
                file.write_bytes(response.read())
        except OSError as e:
            sys.exit(f"localidraw: Chrome did not export {drawing_id}: {e}")
        finally:
            os.killpg(browser.pid, signal.SIGKILL)


def call(method: str, path: str, data: object = None) -> Any:  # noqa: ANN401
    req = urllib.request.Request(
        f"http://localhost:{PORT}{quote(path)}",
        method=method,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read() or b"null")


def stdin_json() -> Any:  # noqa: ANN401
    if sys.stdin.isatty():
        return None
    text = sys.stdin.read().strip()
    return json.loads(text) if text else None


def update(drawing_id: str, change: Callable[[Scene], object]) -> None:
    """Read-modify-write a scene, through the server when running so open tabs refresh."""
    scene = call("GET", f"/api/drawings/{drawing_id}") if server_up() else read_scene(drawing_id)
    change(scene)
    if server_up():
        call("POST", f"/api/drawings/{drawing_id}", scene)
    else:
        write_scene(drawing_id, scene)


COMPLETIONS = {
    "zsh": r"""_localidraw() {
  if (( CURRENT == 2 )); then
    local -a entries=(@ENTRIES@)
    _describe command entries
  elif (( CURRENT == 3 )) && [[ $words[2] == (@IDS@) ]]; then
    local -a ids=(${${(f)"$(localidraw list 2>/dev/null)"}%%$'\t'*})
    compadd -a ids
  else
    _files
  fi
}
compdef _localidraw localidraw
""",
    "bash": r"""_localidraw() {
  local cur=${COMP_WORDS[COMP_CWORD]}
  if (( COMP_CWORD == 1 )); then
    COMPREPLY=($(compgen -W "@NAMES@" -- "$cur"))
  elif (( COMP_CWORD == 2 )); then
    case ${COMP_WORDS[1]} in
      @IDS@) COMPREPLY=($(compgen -W "$(localidraw list 2>/dev/null | cut -f1)" -- "$cur")) ;;
    esac
  fi
}
complete -o default -F _localidraw localidraw
""",
}
HELP_EXAMPLES = """examples:
  $ localidraw start
  $ echo '[{"type": "rectangle", "x": 0, "y": 0, "width": 200, "height": 100, "label": {"text": "Box"}}]' \\
      | localidraw new demo --open
  $ localidraw list --json

Elements are Excalidraw JSON on stdin: full elements, or skeletons like the rectangle above.
Sync: set LOCALIDRAW_BUCKET, and HF_TOKEN or hf auth login. Also: LOCALIDRAW_DATA, LOCALIDRAW_PORT.
`localidraw <command> --help` shows a command's options."""


type Subcommands = argparse._SubParsersAction[argparse.ArgumentParser]  # noqa: SLF001


def print_help(parser: argparse.ArgumentParser, sub: Subcommands) -> None:
    rows = []
    for action in sub._choices_actions:  # noqa: SLF001
        usage = (
            sub.choices[action.dest].format_usage().split()[3:]
        )  # drop "usage: localidraw <cmd>"; joins wrapped lines
        rows.append((" ".join([action.dest, *(t for t in usage if t != "[-h]")]), action.help or ""))
    width = max(len(cmd) for cmd, _ in rows)
    print("localidraw: local Excalidraw with a folder gallery, Hugging Face sync and an agent-friendly CLI\n")
    print(f"{parser.format_usage()}\ncommands:")
    for cmd, text in rows:
        print(f"  {cmd.ljust(width)}  {text}")
    print(f"\n{HELP_EXAMPLES}")


def completion_script(sub: Subcommands, shell: str) -> str:
    """Bake the commands into the script, so completing them needs no localidraw call."""
    helps = {a.dest: a.help or "" for a in sub._choices_actions}  # noqa: SLF001
    ids = "|".join(name for name in helps if any(a.dest == "id" for a in sub.choices[name]._actions))  # noqa: SLF001
    entries = " ".join(shlex.quote(f"{name}:{text}") for name, text in helps.items())
    return COMPLETIONS[shell].replace("@ENTRIES@", entries).replace("@NAMES@", " ".join(helps)).replace("@IDS@", ids)


def main() -> None:
    parser = argparse.ArgumentParser(prog="localidraw", usage="localidraw <command> [options]", add_help=False)
    parser.add_argument("-h", "--help", action="store_true")
    sub = parser.add_subparsers(dest="cmd", metavar="<command>", prog="localidraw")
    p = sub.add_parser("start", help="run the server in the background (or reuse it) and open the gallery")
    p.add_argument("--no-browser", action="store_true", help="don't open the gallery")
    sub.add_parser("stop", help="stop the server; it syncs first")
    p = sub.add_parser("status", help="is the server up, and are drawings waiting to sync")
    p.add_argument("--json", action="store_true", help="print JSON")
    sub.add_parser("sync", help="sync with the Hugging Face bucket now")
    p = sub.add_parser("list", help="list drawings: id, sync status, date, folder/name")
    p.add_argument("--json", action="store_true", help="print JSON")
    p = sub.add_parser("new", help="create a drawing from elements on stdin (or empty); prints its id")
    p.add_argument("name", nargs="?", metavar="NAME", help="drawing name")
    p.add_argument("--open", action="store_true", help="open it in the browser")
    for cmd, text in (
        ("open", "open a drawing in the browser"),
        ("show", "print a drawing's scene JSON"),
        ("add", "append elements from stdin"),
        ("set", "replace the elements with those on stdin"),
    ):
        sub.add_parser(cmd, help=text).add_argument("id", metavar="ID")
    p = sub.add_parser("rename", help="rename a drawing")
    p.add_argument("id", metavar="ID")
    p.add_argument("name", metavar="NAME")
    p = sub.add_parser("mv", help='move a drawing to a folder: "a/b", or "" for the top level')
    p.add_argument("id", metavar="ID")
    p.add_argument("folder", metavar="FOLDER")
    p = sub.add_parser("export", help="export a drawing to FILE, .svg or .png; needs the server and Chrome")
    p.add_argument("id", metavar="ID")
    p.add_argument("file", metavar="FILE", type=Path)
    sub.add_parser("rm", help="delete a drawing here and in the bucket; a copy stays in history/").add_argument(
        "id", metavar="ID"
    )
    p = sub.add_parser("completion", help='print shell completion; add eval "$(localidraw completion zsh)" to ~/.zshrc')
    p.add_argument("shell", choices=COMPLETIONS)
    p = sub.add_parser("serve")  # no help=: argparse leaves serve and help out of _choices_actions
    p.add_argument("--no-browser", action="store_true")
    sub.add_parser("help")
    args = parser.parse_args()
    if args.help or args.cmd in (None, "help"):
        return print_help(parser, sub)
    if args.cmd == "completion":
        return print(completion_script(sub, args.shell), end="")
    if args.cmd == "status":
        rows = status_rows()
        if args.json:
            return print(json.dumps(rows, separators=(",", ":")))
        width = max(len(r["name"]) for r in rows)
        for r in rows:
            print(f"{r['name']:<{width}}  {'on' if r['on'] else 'off':<3}  {r['detail']}")
        return None
    DRAWINGS.mkdir(parents=True, exist_ok=True)

    if args.cmd == "start":
        return start(open_browser=not args.no_browser)
    if args.cmd == "stop":
        return stop()
    if args.cmd == "serve":
        return serve(open_browser=not args.no_browser)
    if args.cmd == "list":
        if args.json:
            return print(json.dumps(drawings(), separators=(",", ":")))
        for d in drawings():
            print(
                f"{d['id']}\t{d['status']}\t{datetime.fromtimestamp(d['mtime']):%Y-%m-%d %H:%M}\t"
                f"{d['folder'] + '/' if d['folder'] else ''}{d['name']}"
            )
    elif args.cmd == "sync":
        if not sync_on():
            sys.exit(SYNC_OFF)
        ok = call("POST", "/sync")["synced"] if server_up() else sync()
        print("synced" if ok else f"offline: {sync_status['error'] or 'bucket unreachable'}")
    elif args.cmd == "new":
        elements = stdin_json() or []
        if args.name and name_taken(args.name, ""):
            sys.exit(taken_error(args.name, ""))
        if server_up():
            result = call("POST", "/new", {"name": args.name, "elements": elements})
            drawing_id = result["id"]
            if args.open:
                webbrowser.open(result["url"])
        else:
            drawing_id = new_id()
            scene = empty_scene(args.name)
            scene["elements"] = elements
            write_scene(drawing_id, scene)
        print(drawing_id)
    elif args.cmd == "show":
        scene = call("GET", f"/api/drawings/{args.id}") if server_up() else read_scene(args.id)
        print(json.dumps(scene, indent=2))
    elif args.cmd == "add":
        elements = stdin_json() or []
        update(args.id, lambda s: s["elements"].extend(elements))
    elif args.cmd == "set":
        elements = stdin_json() or []
        update(args.id, lambda s: s.update(elements=elements))
    elif args.cmd == "rename":
        folder = folder_of(read_scene(args.id))
        if name_taken(args.name, folder, args.id):
            sys.exit(taken_error(args.name, folder))
        update(args.id, lambda s: s.setdefault("appState", {}).update(name=args.name))
    elif args.cmd == "mv":
        folder, name = clean_folder(args.folder), display_name(args.id, read_scene(args.id))
        if name_taken(name, folder, args.id):
            sys.exit(taken_error(name, folder))
        update(args.id, lambda s: s.setdefault("localidraw", {}).update(folder=folder))
    elif args.cmd == "rm":
        if server_up():
            call("POST", "/delete", {"id": args.id})
        else:
            delete_drawing(args.id)
    elif args.cmd == "open":
        require_server()
        webbrowser.open(call("POST", "/open", {"id": args.id})["url"])
    elif args.cmd == "export":
        export(args.id, args.file)
    return None


if __name__ == "__main__":
    main()
