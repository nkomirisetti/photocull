#!/usr/bin/env python3
"""
PhotoCull — sort a folder of photos into keep / archive, fast.

    python3 photocull.py ~/Photos/FujiTrip
    python3 photocull.py ~/Photos/FujiTrip --archive ~/Photos/FujiTrip_rejects

Opens a local web UI (127.0.0.1 only):
  - Full-screen photo, filmstrip carousel along the bottom
  - ← / → to move between photos
  - K = keep (stays in the folder), D = archive, U = clear the mark
  - Carousel thumbnails get a green ring for keep, red for archive
  - Top bar shows how many are left to sort

NOTHING IS EVER DELETED. "Archive" *moves* the file to the archive folder
(default: a sibling folder named "<folder>_archive"). Changing your mind
moves it back. Decisions are saved to <folder>/.photocull-state.json so
you can quit and resume any time.

Requires: Pillow (for thumbnails). pillow-heif (for HEIC / Fujifilm HIF).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import secrets
import shutil
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("Pillow is required:  pip install Pillow")

try:
    from pillow_heif import register_heif_opener
except ImportError:
    sys.exit("pillow-heif is required for HEIC/HIF:  pip install pillow-heif")

register_heif_opener()

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".heic", ".heif", ".hif", ".gif"}
# Formats every browser can show as-is; anything else (HEIC/HIF, TIFF) is
# converted to a cached JPEG for the full-size view.
BROWSER_NATIVE = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
DISPLAY_SIZE = 3840  # long edge of converted full-size previews
STATE_NAME = ".photocull-state.json"
THUMB_DIR = Path(tempfile.gettempdir()) / "photocull-thumbs"
THUMB_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Library: one folder, a list of photos, decisions, archive moves
# ---------------------------------------------------------------------------


class Library:
    def __init__(self, folder: Path, archive: Path):
        self.folder = folder
        self.archive = archive
        self.state_path = folder / STATE_NAME
        self.lock = threading.Lock()
        # Image URLs are by index, so give each run its own token: otherwise the
        # browser can show a cached photo from a previous folder at that index.
        self.session = secrets.token_hex(4)
        self.decisions: dict[str, dict] = {}  # filename -> {"status":..., "as": stored_name}
        self.names: list[str] = []            # stable order: sorted filenames
        self._load()

    # -- scanning / persistence -------------------------------------------

    def _scan_files(self) -> list[str]:
        found = set()
        if self.folder.exists():
            for p in self.folder.iterdir():
                if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith("."):
                    found.add(p.name)
        # Archived photos live in the archive folder; include exactly the
        # ones this app put there (recorded in decisions), nothing stray.
        for orig, d in self.decisions.items():
            if d.get("status") == "archive":
                stored = self.archive / d.get("as", orig)
                if stored.exists():
                    found.add(orig)
        return sorted(found)

    def _load(self):
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                self.decisions = data.get("decisions", {})
            except Exception:
                self.decisions = {}
        self.names = self._scan_files()
        # Drop decisions for files that no longer exist anywhere
        self.decisions = {k: v for k, v in self.decisions.items() if k in self.names}

    def _save(self):
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"decisions": self.decisions}, indent=1))
        tmp.replace(self.state_path)

    # -- paths --------------------------------------------------------------

    def current_path(self, name: str) -> Path:
        d = self.decisions.get(name)
        if d and d.get("status") == "archive":
            return self.archive / d.get("as", name)
        return self.folder / name

    # -- API surface ----------------------------------------------------------

    def summary(self) -> dict:
        with self.lock:
            photos = []
            kept = archived = 0
            for i, name in enumerate(self.names):
                status = self.decisions.get(name, {}).get("status")
                if status == "keep":
                    kept += 1
                elif status == "archive":
                    archived += 1
                photos.append({"i": i, "name": name, "status": status})
            total = len(self.names)
            return {
                "session": self.session,
                "folder": str(self.folder),
                "archive": str(self.archive),
                "total": total,
                "kept": kept,
                "archived": archived,
                "left": total - kept - archived,
                "photos": photos,
            }

    def mark(self, index: int, action: str) -> dict:
        """action: keep | archive | clear. Moves the file when needed."""
        with self.lock:
            if not (0 <= index < len(self.names)):
                raise IndexError("bad index")
            name = self.names[index]
            current = self.current_path(name)
            status = self.decisions.get(name, {}).get("status")

            if action == "clear":
                if status == "archive" and current.exists():
                    self._move(current, self.folder / name)
                self.decisions.pop(name, None)

            elif action == "keep":
                if status == "archive" and current.exists():
                    self._move(current, self.folder / name)
                self.decisions[name] = {"status": "keep"}

            elif action == "archive":
                if status != "archive":
                    self.archive.mkdir(parents=True, exist_ok=True)
                    dest = self.archive / name
                    if dest.exists() and dest.resolve() != current.resolve():
                        stem, suf, n = dest.stem, dest.suffix, 1
                        while dest.exists():
                            dest = self.archive / f"{stem}_{n}{suf}"
                            n += 1
                    stored = dest.name
                    if current.exists():
                        self._move(current, dest)
                    self.decisions[name] = {"status": "archive", "as": stored}
            else:
                raise ValueError(f"unknown action {action}")

            self._save()
            return {"name": name, "status": self.decisions.get(name, {}).get("status")}

    @staticmethod
    def _move(src: Path, dst: Path):
        # shutil.move silently replaces an existing file, so refuse instead:
        # a different photo with the same name may have appeared at dst.
        if dst.exists() and dst.resolve() != src.resolve():
            raise FileExistsError(
                f"{dst} already exists — not overwriting it. "
                f"Rename or move one of them, then try again. (Your photo is safe at {src}.)"
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------


def thumb_for(path: Path, size: int = 320) -> Path:
    key = hashlib.sha1(f"{path}|{path.stat().st_mtime}|{size}".encode()).hexdigest()
    out = THUMB_DIR / f"{key}.jpg"
    if out.exists():
        return out
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")  # portrait shots stay upright
        im.thumbnail((size, size))
        im.save(out, quality=80)
    return out


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PhotoCull</title>
<style>
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; background: #111; color: #eee; font-family: -apple-system, system-ui, sans-serif; }
  #app { display: flex; flex-direction: column; height: 100vh; }
  header { display: flex; align-items: center; gap: 16px; padding: 10px 16px; background: #1b1b1b; border-bottom: 1px solid #333; flex-wrap: wrap; }
  header .folder { color: #999; font-size: 13px; max-width: 34vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #counter { font-size: 20px; font-weight: 700; }
  #counter .left { color: #ffd75e; }
  .counts { color: #aaa; font-size: 13px; }
  .counts b.keep { color: #2ecc71; } .counts b.arch { color: #e74c3c; }
  .spacer { flex: 1; }
  button { font-size: 15px; padding: 8px 18px; border-radius: 8px; border: 1px solid #444; background: #262626; color: #eee; cursor: pointer; }
  button:hover { background: #333; }
  button.keep { border-color: #2ecc71; color: #2ecc71; font-weight: 700; }
  button.arch { border-color: #e74c3c; color: #e74c3c; font-weight: 700; }
  #progress { height: 4px; background: #222; }
  #progressBar { height: 100%; background: linear-gradient(90deg, #2ecc71, #ffd75e); width: 0%; transition: width .2s; }
  main { flex: 1; display: flex; align-items: center; justify-content: center; position: relative; min-height: 0; padding: 10px; }
  #photo { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 6px; box-shadow: 0 6px 40px rgba(0,0,0,.6); }
  #caption { position: absolute; left: 16px; bottom: 12px; color: #ccc; font-size: 13px; background: rgba(0,0,0,.55); padding: 4px 10px; border-radius: 6px; }
  #badge { position: absolute; top: 14px; right: 16px; font-size: 15px; font-weight: 800; padding: 6px 14px; border-radius: 999px; display: none; }
  #badge.keep { display: block; background: rgba(46,204,113,.18); color: #2ecc71; border: 2px solid #2ecc71; }
  #badge.arch { display: block; background: rgba(231,76,60,.18); color: #e74c3c; border: 2px solid #e74c3c; }
  #strip { display: flex; gap: 8px; overflow-x: auto; padding: 10px 12px; background: #171717; border-top: 1px solid #333; min-height: 118px; }
  .thumb { flex: 0 0 auto; width: 84px; cursor: pointer; text-align: center; }
  .thumb img { width: 84px; height: 84px; object-fit: cover; border-radius: 8px; border: 3px solid transparent; opacity: .55; }
  .thumb.keep img { border-color: #2ecc71; opacity: .9; }
  .thumb.arch img { border-color: #e74c3c; opacity: .9; filter: saturate(.35); }
  .thumb.current img { outline: 3px solid #fff; outline-offset: 1px; opacity: 1; }
  .thumb .n { font-size: 10px; color: #888; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #done { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; flex-direction: column; gap: 10px; background: rgba(0,0,0,.72); font-size: 26px; text-align: center; padding: 20px; }
  #toast { position: absolute; top: 14px; left: 50%; transform: translateX(-50%); max-width: 70%; display: none; z-index: 2; background: #3a1414; color: #ffb4a8; border: 1px solid #e74c3c; padding: 8px 14px; border-radius: 8px; font-size: 14px; }
  .keys { color: #777; font-size: 12px; }
  kbd { background: #333; border-radius: 4px; padding: 1px 6px; font-family: inherit; }
</style></head>
<body><div id="app">
  <header>
    <div>
      <div id="counter">…</div>
      <div class="counts" id="counts"></div>
    </div>
    <div class="folder" id="folderInfo"></div>
    <div class="spacer"></div>
    <span class="keys"><kbd>←</kbd><kbd>→</kbd> move &nbsp;<kbd>K</kbd> keep &nbsp;<kbd>D</kbd> archive &nbsp;<kbd>U</kbd> unmark</span>
    <button class="keep" id="btnKeep">✓ Keep (K)</button>
    <button class="arch" id="btnArch">✗ Archive (D)</button>
  </header>
  <div id="progress"><div id="progressBar"></div></div>
  <main>
    <img id="photo" alt="">
    <div id="badge"></div>
    <div id="caption"></div>
    <div id="done"><div>🎉 All sorted</div><div class="counts" id="doneCounts"></div>
      <div class="keys">Nothing was deleted — archived photos were only moved. Press ← to review any photo and change it.</div></div>
    <div id="toast"></div>
  </main>
  <div id="strip"></div>
</div>
<script>
let S = null, cur = 0;
const $ = id => document.getElementById(id);

async function load() {
  S = await (await fetch('/api/photos')).json();
  if (S.total === 0) { $('counter').textContent = 'No photos found in that folder'; return; }
  cur = firstUndecided();
  renderStrip(); show(cur); refreshHeader();
}
const imgUrl = (kind, i) => `/${kind}/${i}?s=${S.session}`;
function firstUndecided(from = 0) {
  for (let k = 0; k < S.total; k++) { const i = (from + k) % S.total; if (!S.photos[i].status) return i; }
  return Math.min(from, S.total - 1);
}
function refreshHeader() {
  $('counter').innerHTML = S.left > 0 ? `<span class="left">${S.left} left</span> to sort` : 'All sorted';
  $('counts').innerHTML = `of ${S.total} · <b class="keep">${S.kept} kept</b> · <b class="arch">${S.archived} archived</b>`;
  $('folderInfo').textContent = S.folder + '  →  archive: ' + S.archive;
  $('progressBar').style.width = (S.total ? (100 * (S.total - S.left) / S.total) : 100) + '%';
  $('done').style.display = (S.left === 0 && S.total > 0) ? 'flex' : 'none';
  $('doneCounts').innerHTML = `<b class="keep">${S.kept} kept</b> · <b class="arch">${S.archived} archived (moved, not deleted)</b>`;
}
function renderStrip() {
  const strip = $('strip'); strip.innerHTML = '';
  S.photos.forEach((p, i) => {
    const d = document.createElement('div');
    d.className = 'thumb' + (p.status === 'keep' ? ' keep' : p.status === 'archive' ? ' arch' : '') + (i === cur ? ' current' : '');
    d.id = 'th' + i;
    d.innerHTML = `<img loading="lazy" src="${imgUrl('thumb', i)}"><div class="n">${p.name}</div>`;
    d.onclick = () => show(i);
    strip.appendChild(d);
  });
}
function markStrip(i) {
  const el = $('th' + i); if (!el) return;
  const st = S.photos[i].status;
  el.className = 'thumb' + (st === 'keep' ? ' keep' : st === 'archive' ? ' arch' : '') + (i === cur ? ' current' : '');
}
function show(i) {
  if (S.total === 0) return;
  document.querySelectorAll('.thumb.current').forEach(e => e.classList.remove('current'));
  cur = Math.max(0, Math.min(i, S.total - 1));
  const p = S.photos[cur];
  $('photo').src = imgUrl('img', cur);
  $('caption').textContent = `${cur + 1} / ${S.total} — ${p.name}`;
  const b = $('badge');
  b.className = p.status === 'keep' ? 'keep' : p.status === 'archive' ? 'arch' : '';
  b.textContent = p.status === 'keep' ? '✓ KEPT' : p.status === 'archive' ? '✗ ARCHIVED' : '';
  const el = $('th' + cur); if (el) { el.classList.add('current'); el.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' }); }
  // preload neighbours
  [cur + 1, cur - 1].forEach(j => { if (j >= 0 && j < S.total) { const im = new Image(); im.src = imgUrl('img', j); } });
}
function recount() {
  S.kept = S.photos.filter(p => p.status === 'keep').length;
  S.archived = S.photos.filter(p => p.status === 'archive').length;
  S.left = S.total - S.kept - S.archived;
}
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.style.display = 'none'; }, 8000);
}
// Marks are applied locally right away (so fast K, D, D… each hit the photo
// that was on screen) and sent to the server one at a time, in order.
let pending = Promise.resolve();
function mark(action) {
  if (!S || S.total === 0) return;
  const i = cur;
  S.photos[i].status = action === 'clear' ? null : action;
  recount();
  markStrip(i);
  // fly to the next undecided photo, like a real culling flow
  show(S.left > 0 ? firstUndecided(i + 1) : i);
  refreshHeader();
  pending = pending.then(async () => {
    const res = await fetch('/api/mark', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ index: i, action }) });
    const r = await res.json();
    if (!res.ok || r.error) throw new Error(r.error || `HTTP ${res.status}`);
  }).catch(async err => {
    toast(`Couldn't update ${S.photos[i].name}: ${err.message}`);
    // the server is the source of truth — resync so the UI matches the files
    S = await (await fetch('/api/photos')).json();
    renderStrip(); show(i); refreshHeader();
  });
}
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowRight') { show(cur + 1); e.preventDefault(); }
  else if (e.key === 'ArrowLeft') { show(cur - 1); e.preventDefault(); }
  else if (e.key === 'k' || e.key === 'K') mark('keep');
  else if (e.key === 'd' || e.key === 'D' || e.key === 'x' || e.key === 'X') mark('archive');
  else if (e.key === 'u' || e.key === 'U') mark('clear');
});
$('btnKeep').onclick = () => mark('keep');
$('btnArch').onclick = () => mark('archive');
load();
</script></body></html>
"""


def make_handler(lib: Library):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path):
            if not path.exists():
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/photos":
                self._json(lib.summary())
            elif route.startswith("/img/"):
                try:
                    i = int(route.split("/")[2])
                    path = lib.current_path(lib.names[i])
                    if path.suffix.lower() not in BROWSER_NATIVE:
                        path = thumb_for(path, DISPLAY_SIZE)
                    self._file(path)
                except Exception:
                    self.send_error(404)
            elif route.startswith("/thumb/"):
                try:
                    i = int(route.split("/")[2])
                    self._file(thumb_for(lib.current_path(lib.names[i])))
                except Exception:
                    self.send_error(404)
            else:
                self.send_error(404)

        def do_POST(self):
            route = urlparse(self.path).path
            if route != "/api/mark":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = lib.mark(int(payload["index"]), str(payload["action"]))
                result.update({k: lib.summary()[k] for k in ("left", "kept", "archived", "total")})
                self._json(result)
            except Exception as e:
                self._json({"error": str(e)}, 400)

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="photocull", description="Sort a folder of photos into keep / archive. Nothing is ever deleted.")
    ap.add_argument("folder", type=Path, help="Folder of photos to sort (top level only)")
    ap.add_argument("--archive", type=Path, default=None, help="Archive folder (default: '<folder>_archive' next to it)")
    ap.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    ap.add_argument("--no-open", action="store_true", help="Don't auto-open the browser")
    args = ap.parse_args(argv)

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"error: not a folder: {folder}", file=sys.stderr)
        return 2
    archive = (args.archive.expanduser().resolve() if args.archive
               else folder.parent / f"{folder.name}_archive")

    lib = Library(folder, archive)
    if not lib.names:
        print(f"No photos found in {folder} (JPG/PNG/HEIC/HIF/WebP/TIFF, top level).")
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(lib))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"PhotoCull: {len(lib.names)} photos in {folder}")
    print(f"  keep    -> stays in {folder}")
    print(f"  archive -> moves to {archive}  (nothing is ever deleted)")
    print(f"Open: {url}   (Ctrl-C to stop)")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved — rerun the same command to resume.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
