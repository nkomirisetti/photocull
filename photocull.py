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
import os
import secrets
import shutil
import subprocess
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
mimetypes.add_type("image/heif", ".hif")
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("image/heic", ".heic")
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


_render_locks: dict[str, threading.Lock] = {}
_render_locks_guard = threading.Lock()


def thumb_for(path: Path, size: int = 320) -> Path:
    # Keyed on name/size/mtime rather than the full path, so archiving a photo
    # (a move, which keeps mtime) doesn't throw away its cached render.
    st = path.stat()
    key = hashlib.sha1(f"{path.name}|{st.st_size}|{st.st_mtime_ns}|{size}".encode()).hexdigest()
    out = THUMB_DIR / f"{key}.jpg"
    if out.exists():
        return out
    # One render per image at a time: a preload and a click on the same photo
    # wait for the same decode instead of doing it twice.
    with _render_locks_guard:
        lock = _render_locks.setdefault(key, threading.Lock())
    with lock:
        if out.exists():
            return out
        # write-then-rename so a concurrent request never serves half a file
        tmp = out.with_name(f"{key}.{threading.get_ident()}.tmp.jpg")
        _render(path, size, tmp)
        tmp.replace(out)
    return out


SIPS = shutil.which("sips") if sys.platform == "darwin" else None
HEIF_SUFFIXES = {".heic", ".heif", ".hif"}


def _render(path: Path, size: int, out: Path):
    if SIPS and path.suffix.lower() in HEIF_SUFFIXES:
        # macOS ImageIO: hardware HEVC decode, and it uses the embedded preview
        # for small sizes — ~0.3s for a 40MP HIF thumbnail vs ~1.3s in Pillow.
        r = subprocess.run(
            [SIPS, "-s", "format", "jpeg", "-s", "formatOptions", "80", "-Z", str(size), str(path), "--out", str(out)],
            capture_output=True,
        )
        if r.returncode == 0 and out.exists():
            return
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")  # portrait shots stay upright
        im.thumbnail((size, size))
        im.save(out, format="JPEG", quality=80)


def display_path(path: Path) -> Path:
    """What the browser is sent for the full-size view."""
    if path.suffix.lower() in BROWSER_NATIVE:
        return path
    return thumb_for(path, DISPLAY_SIZE)


class Prerender:
    """Renders thumbnails (and, for browsers that can't show the originals,
    full-size JPEG previews) in the background, nearest to the photo being
    viewed first, so moving between photos rarely waits on a decode."""

    def __init__(self, lib: Library, workers: int):
        self.lib = lib
        self.focus = 0
        self.previews = False  # turned on when the browser can't display originals
        self.stopped = False
        self.cv = threading.Condition()
        self.claimed: set[tuple[int, str]] = set()
        for _ in range(workers):
            threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        with self.cv:
            self.stopped = True
            self.cv.notify_all()

    def update(self, focus: int, previews: bool):
        with self.cv:
            self.focus = focus
            self.previews = self.previews or previews
            self.cv.notify_all()

    def _next_job(self, wait: bool = True) -> tuple[int, str] | None:
        with self.cv:
            while not self.stopped:
                n, f = len(self.lib.names), self.focus
                kinds = ("img", "thumb") if self.previews else ("thumb",)
                # ahead of the viewer first, then behind; previews before thumbs
                order = sorted(
                    ((i, kind) for i in range(n) for kind in kinds),
                    key=lambda j: (abs(j[0] - f) + (0.5 if j[0] < f else 0), j[1] == "thumb"),
                )
                for job in order:
                    if job not in self.claimed:
                        self.claimed.add(job)
                        return job
                if not wait:
                    return None
                self.cv.wait()  # until previews get switched on, or the folder changes
            return None

    def _run(self, wait: bool = True):
        while (job := self._next_job(wait)) is not None:
            i, kind = job
            try:
                path = self.lib.current_path(self.lib.names[i])
                display_path(path) if kind == "img" else thumb_for(path)
            except Exception:
                pass  # unreadable or just moved; served on demand instead


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
  header .folder { color: #bbb; font-size: 13px; padding: 6px 12px; max-width: 34vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #picker { display: none; min-height: 100vh; align-items: flex-start; justify-content: center; padding: 10vh 16px 40px; }
  #picker .card { width: 100%; max-width: 640px; display: flex; flex-direction: column; gap: 14px; }
  #picker h1 { font-size: 32px; }
  #picker h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: #888; margin-top: 18px; }
  .muted { color: #999; }
  button.primary { background: #2ecc71; border-color: #2ecc71; color: #0d1f14; font-weight: 700; font-size: 17px; padding: 12px 20px; align-self: flex-start; }
  button.primary:hover { background: #45d985; }
  button:disabled { opacity: .6; cursor: default; }
  #pathForm { display: flex; gap: 8px; }
  #pathInput { flex: 1; min-width: 0; font: inherit; font-size: 15px; padding: 8px 12px; border-radius: 8px; border: 1px solid #444; background: #1b1b1b; color: #eee; }
  #pickErr { color: #ff8a7a; font-size: 14px; min-height: 1em; }
  #recents { display: flex; flex-direction: column; gap: 8px; }
  .recent { text-align: left; display: flex; flex-direction: column; gap: 4px; padding: 12px 14px; }
  .recent .name { font-weight: 700; }
  .recent .path { color: #888; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .recent .bar { height: 3px; background: #333; border-radius: 2px; overflow: hidden; margin-top: 4px; }
  .recent .bar div { height: 100%; background: linear-gradient(90deg, #2ecc71, #ffd75e); }
  .recent .prog { color: #aaa; font-size: 12px; }
  #pickBack { align-self: flex-start; margin-top: 10px; }
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
  #stage { position: absolute; inset: 10px; }
  .photo { position: absolute; inset: 0; margin: auto; max-width: 100%; max-height: 100%; border-radius: 6px; box-shadow: 0 6px 40px rgba(0,0,0,.6); opacity: 0; }
  .photo.shown { opacity: 1; }
  #hud { display: none; position: absolute; top: 14px; left: 16px; z-index: 1; background: rgba(0,0,0,.55); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); padding: 7px 12px; border-radius: 8px; font-size: 13px; color: #ddd; }
  #hudLeft { color: #ffd75e; }
  #hudBar { height: 3px; background: #333; border-radius: 2px; overflow: hidden; margin: 6px 0; }
  #hudFill { height: 100%; background: linear-gradient(90deg, #2ecc71, #ffd75e); width: 0%; transition: width .2s; }
  #hud .keys kbd { font-size: 11px; }
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
  #res { position: absolute; right: 16px; bottom: 12px; color: #999; font-size: 12px; background: rgba(0,0,0,.55); padding: 4px 10px; border-radius: 6px; }
  body.fs header, body.fs #progress { display: none; }
  /* full screen: the filmstrip floats over the photo, dim until you reach for it */
  body.fs #strip { position: fixed; left: 0; right: 0; bottom: 0; z-index: 2; background: rgba(0,0,0,.35); -webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px); border-top: none; opacity: .45; transition: opacity .2s; }
  body.fs #strip:hover { opacity: 1; }
  body.fs #caption, body.fs #res { bottom: 132px; }
  .chip { display: none; font-size: 12px; padding: 4px 10px; border-radius: 999px; border-color: #ffd75e; color: #ffd75e; background: rgba(255,215,94,.1); }
  body.unsorted .chip { display: inline-block; }
  #hudMode { color: #ffd75e; }
  #toast.info { background: #1d2a36; color: #cfe6ff; border-color: #4a90d9; }
  #grid { display: none; flex: 1; min-height: 0; overflow-y: auto; padding: 14px; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); align-content: start; }
  .cell { cursor: pointer; min-width: 0; }
  .cell img { display: block; width: 100%; aspect-ratio: 3 / 2; object-fit: cover; border-radius: 8px; border: 3px solid transparent; background: #222; }
  .cell.keep img { border-color: #2ecc71; }
  .cell.arch img { border-color: #e74c3c; filter: saturate(.35); opacity: .8; }
  .cell.current img { outline: 3px solid #fff; outline-offset: 2px; }
  .cell .n { font-size: 11px; color: #999; margin-top: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #grid .empty { grid-column: 1 / -1; color: #999; padding: 40px; text-align: center; }
  body.grid #grid { display: grid; }
  body.grid main, body.grid #strip { display: none; }
  body.fs main { padding: 0; background: #000; }
  body.fs #stage { inset: 0; }
  body.fs .photo { border-radius: 0; box-shadow: none; }
  body.fs #hud { display: block; }
  .keys { color: #777; font-size: 12px; }
  kbd { background: #333; border-radius: 4px; padding: 1px 6px; font-family: inherit; }
</style></head>
<body>
<div id="picker">
  <div class="card">
    <h1>PhotoCull</h1>
    <p class="muted">Choose a folder of photos to sort. Nothing is ever deleted.</p>
    <button class="primary" id="btnChoose">Choose Folder…</button>
    <form id="pathForm"><input id="pathInput" placeholder="…or paste a folder path, e.g. ~/Pictures/Trip" autocomplete="off" spellcheck="false"><button>Open</button></form>
    <div id="pickErr"></div>
    <h2 id="recentsTitle">Recent</h2>
    <div id="recents"></div>
    <button id="pickBack">← Back to <span id="pickBackName"></span></button>
  </div>
</div>
<div id="app">
  <header>
    <div>
      <div id="counter">…</div>
      <div class="counts" id="counts"></div>
    </div>
    <button class="folder" id="btnFolder" title="Change folder">📁 <span id="folderName"></span></button>
    <button class="chip" id="modeChip" title="N: arrows skip sorted photos">Unsorted only ✕</button>
    <div class="spacer"></div>
    <span class="keys"><kbd>←</kbd><kbd>→</kbd> move &nbsp;<kbd>K</kbd> keep &nbsp;<kbd>D</kbd> archive &nbsp;<kbd>U</kbd> unmark &nbsp;<kbd>F</kbd> full screen &nbsp;<kbd>G</kbd> grid &nbsp;<kbd>N</kbd> unsorted only</span>
    <button class="keep" id="btnKeep">✓ Keep (K)</button>
    <button class="arch" id="btnArch">✗ Archive (D)</button>
  </header>
  <div id="progress"><div id="progressBar"></div></div>
  <main>
    <div id="stage"></div>
    <div id="hud"><b id="hudLeft"></b> <span id="hudCounts"></span> <span id="hudMode"></span>
      <div id="hudBar"><div id="hudFill"></div></div>
      <div class="keys"><kbd>K</kbd> keep <kbd>D</kbd> archive <kbd>U</kbd> undo <kbd>G</kbd> grid <kbd>N</kbd> unsorted <kbd>F</kbd> exit</div></div>
    <div id="badge"></div>
    <div id="caption"></div>
    <div id="res"></div>
    <div id="done"><div>🎉 All sorted</div><div class="counts" id="doneCounts"></div>
      <div class="keys">Nothing was deleted — archived photos were only moved. Press ← to review any photo and change it.</div></div>
    <div id="toast"></div>
  </main>
  <div id="grid"></div>
  <div id="strip"></div>
</div>
<script>
let S = null, cur = 0;
const $ = id => document.getElementById(id);

async function load() {
  S = await (await fetch('/api/photos')).json();
  if (S.needsFolder) { S = null; return showPicker(); }
  if (S.total === 0) { $('counter').textContent = 'No photos found in that folder'; return; }
  cur = firstUndecided();
  updateModeUI();
  renderStrip(); show(cur); refreshHeader();
}
const imgUrl = (kind, i) => `/${kind}/${i}?s=${S.session}`;
// Show the untouched original when the browser can decode it (Safari: HEIC/HIF,
// full resolution, hardware-decoded); otherwise fall back to a JPEG preview.
const WEB_FORMATS = ['jpg', 'jpeg', 'png', 'webp', 'gif'];
const nativeOK = {};  // extension -> true/false once we know
const extOf = i => S.photos[i].name.split('.').pop().toLowerCase();
const useOrig = i => WEB_FORMATS.includes(extOf(i)) || nativeOK[extOf(i)] !== false;
const photoUrl = i => imgUrl(useOrig(i) ? 'orig' : 'img', i);
const needPreviews = () => Object.values(nativeOK).includes(false);
function firstUndecided(from = 0) {
  for (let k = 0; k < S.total; k++) { const i = (from + k) % S.total; if (!S.photos[i].status) return i; }
  return Math.min(from, S.total - 1);
}
function refreshHeader() {
  $('counter').innerHTML = S.left > 0 ? `<span class="left">${S.left} left</span> to sort` : 'All sorted';
  $('counts').innerHTML = `of ${S.total} · <b class="keep">${S.kept} kept</b> · <b class="arch">${S.archived} archived</b>`;
  $('folderName').textContent = S.folder.split('/').pop();
  $('btnFolder').title = `${S.folder}\nArchive: ${S.archive}\nClick to change folder`;
  const pct = (S.total ? (100 * (S.total - S.left) / S.total) : 100) + '%';
  $('progressBar').style.width = pct; $('hudFill').style.width = pct;
  $('hudLeft').textContent = S.left > 0 ? `${S.left} left` : 'All sorted';
  $('hudCounts').innerHTML = `· <b class="keep">${S.kept} kept</b> · <b class="arch">${S.archived} archived</b>`;
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
  if (gridOn) { selectCell(); fetch(`/api/focus/${cur}`, { method: 'POST' }); return; }
  // keep the current photo and its neighbours decoded; switching just swaps which is visible
  const want = around(cur);
  dropPool(j => !want.includes(j));
  want.forEach(photoEl);
  const el = photoEl(cur), old = $('photo');
  if (old && old !== el) old.removeAttribute('id');
  el.id = 'photo';
  for (const im of pool.values()) im.classList.toggle('shown', im === el);
  if (el.complete && el.naturalWidth) showRes(el); else $('res').textContent = '';
  fetch(`/api/focus/${cur}${needPreviews() ? '?previews=1' : ''}`, { method: 'POST' });  // background rendering starts from here
  $('caption').textContent = `${cur + 1} / ${S.total} — ${p.name}`;
  const b = $('badge');
  b.className = p.status === 'keep' ? 'keep' : p.status === 'archive' ? 'arch' : '';
  b.textContent = p.status === 'keep' ? '✓ KEPT' : p.status === 'archive' ? '✗ ARCHIVED' : '';
  const th = $('th' + cur); if (th) { th.classList.add('current'); th.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' }); }
}
// Photos kept decoded around the current one, nearest first. Each decoded
// 40MP photo is ~160MB in the browser, so this is the memory/speed knob.
const AHEAD = 3, BEHIND = 2;
// N: arrows step through unsorted photos only
let onlyUnsorted = false;
try { onlyUnsorted = localStorage.getItem('photocull.onlyUnsorted') === '1'; } catch (e) {}
const navigable = j => !onlyUnsorted || !S.photos[j].status;
function nextFrom(i, dir) {
  for (let j = i + dir; j >= 0 && j < S.total; j += dir) if (navigable(j)) return j;
  return -1;
}
function around(i) {
  const ahead = [], behind = [];
  for (let j = i; ahead.length < AHEAD && (j = nextFrom(j, 1)) >= 0;) ahead.push(j);
  for (let j = i; behind.length < BEHIND && (j = nextFrom(j, -1)) >= 0;) behind.push(j);
  const out = [i];
  for (let k = 0; k < AHEAD || k < BEHIND; k++) { if (k < ahead.length) out.push(ahead[k]); if (k < behind.length) out.push(behind[k]); }
  return out;
}
function step(dir) {
  const j = nextFrom(cur, dir);
  if (j >= 0) show(j);
  else if (onlyUnsorted) toast(dir > 0 ? 'No unsorted photos after this one' : 'No unsorted photos before this one', 'info');
}
function toggleUnsorted() {
  onlyUnsorted = !onlyUnsorted;
  try { localStorage.setItem('photocull.onlyUnsorted', onlyUnsorted ? '1' : '0'); } catch (e) {}
  updateModeUI();
  toast(onlyUnsorted ? '← → now skip sorted photos' : '← → now go through every photo', 'info');
  if (gridOn) renderGrid(); else show(cur);
}
function updateModeUI() {
  document.body.classList.toggle('unsorted', onlyUnsorted);
  $('hudMode').textContent = onlyUnsorted ? '· unsorted only' : '';
}
// G: grid of every photo (or every unsorted one, with N)
let gridOn = false;
const gridList = () => S.photos.map((p, i) => i).filter(i => navigable(i) || i === cur);
const cellClass = i => { const st = S.photos[i].status; return 'cell' + (st === 'keep' ? ' keep' : st === 'archive' ? ' arch' : '') + (i === cur ? ' current' : ''); };
function renderGrid() {
  const g = $('grid'); g.innerHTML = '';
  const list = gridList();
  if (!list.length) { g.innerHTML = '<div class="empty">Nothing unsorted — press N to show every photo.</div>'; return; }
  list.forEach(i => {
    const d = document.createElement('div'); d.className = cellClass(i); d.id = 'gc' + i;
    d.innerHTML = `<img loading="lazy" src="${imgUrl('thumb', i)}"><div class="n"></div>`;
    d.querySelector('.n').textContent = S.photos[i].name;
    d.onclick = () => { cur = i; toggleGrid(false); };
    g.appendChild(d);
  });
  selectCell();
}
function selectCell() {
  document.querySelectorAll('.cell.current').forEach(e => e.classList.remove('current'));
  const c = $('gc' + cur); if (c) { c.classList.add('current'); c.scrollIntoView({ block: 'nearest' }); }
}
function gridRow(dir) {
  const cols = getComputedStyle($('grid')).gridTemplateColumns.split(' ').length;
  const list = gridList(), j = list[list.indexOf(cur) + dir * cols];
  if (j !== undefined) show(j);
}
function toggleGrid(on = !gridOn) {
  gridOn = on;
  document.body.classList.toggle('grid', on);
  if (on) renderGrid(); else show(cur);
}
const pool = new Map();  // photo index -> <img>
function photoEl(i) {
  let im = pool.get(i);
  if (im) return im;
  im = new Image(); im.alt = ''; im.className = 'photo';
  im.onload = () => {
    if (im.src.includes('/orig/')) nativeOK[extOf(i)] = true;
    if (i === cur) showRes(im);
  };
  im.onerror = () => {
    if (!im.src.includes('/orig/')) return;
    nativeOK[extOf(i)] = false;  // this browser can't show that format: use JPEG previews
    dropPool(j => !useOrig(j));
    show(cur);
  };
  im.src = photoUrl(i);
  im.decode().catch(() => {});
  $('stage').appendChild(im); pool.set(i, im);
  return im;
}
function dropPool(test) {
  for (const [j, im] of pool) if (test(j)) { im.onload = im.onerror = null; im.remove(); pool.delete(j); }
}
function showRes(im) {
  const preview = im.src.includes('/img/') && !WEB_FORMATS.includes(extOf(cur));
  $('res').textContent = `${im.naturalWidth}×${im.naturalHeight}` + (preview ? ' preview' : '');
}
function recount() {
  S.kept = S.photos.filter(p => p.status === 'keep').length;
  S.archived = S.photos.filter(p => p.status === 'archive').length;
  S.left = S.total - S.kept - S.archived;
}
function toast(msg, kind = 'error') {
  const t = $('toast'); t.textContent = msg; t.className = kind; t.style.display = 'block';
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.style.display = 'none'; }, kind === 'error' ? 8000 : 2500);
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
  // after moving on, so with N the photo just marked drops out of the grid
  if (gridOn) { if (onlyUnsorted) renderGrid(); else { $('gc' + i).className = cellClass(i); selectCell(); } }
  refreshHeader();
  pending = pending.then(async () => {
    const res = await fetch('/api/mark', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ index: i, action, session: S.session }) });
    const r = await res.json();
    if (!res.ok || r.error) throw new Error(r.error || `HTTP ${res.status}`);
  }).catch(async err => {
    toast(`Couldn't update ${S.photos[i].name}: ${err.message}`);
    // the server is the source of truth — resync so the UI matches the files
    S = await (await fetch('/api/photos')).json();
    renderStrip(); if (gridOn) renderGrid(); show(i); refreshHeader();
  });
}
document.addEventListener('keydown', e => {
  if (!S || $('picker').style.display === 'flex') return;  // typing a path, not sorting
  if (e.metaKey || e.ctrlKey || e.altKey) return;  // leave browser shortcuts alone
  const k = e.key.toLowerCase();
  if (e.key === 'ArrowRight') { step(1); e.preventDefault(); }
  else if (e.key === 'ArrowLeft') { step(-1); e.preventDefault(); }
  else if (gridOn && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { gridRow(e.key === 'ArrowDown' ? 1 : -1); e.preventDefault(); }
  else if (gridOn && e.key === 'Enter') toggleGrid(false);
  else if (k === 'k') mark('keep');
  else if (k === 'd' || k === 'x') mark('archive');
  else if (k === 'u') mark('clear');
  else if (k === 'f') toggleFullscreen();
  else if (k === 'g') toggleGrid();
  else if (k === 'n') toggleUnsorted();
});
function toggleFullscreen() {
  const d = document, el = d.documentElement;
  if (d.fullscreenElement || d.webkitFullscreenElement) (d.exitFullscreen || d.webkitExitFullscreen).call(d);
  else (el.requestFullscreen || el.webkitRequestFullscreen).call(el);
}
['fullscreenchange', 'webkitfullscreenchange'].forEach(ev => document.addEventListener(ev, () =>
  document.body.classList.toggle('fs', !!(document.fullscreenElement || document.webkitFullscreenElement))));
async function showPicker() {
  if (document.fullscreenElement || document.webkitFullscreenElement) toggleFullscreen();
  $('app').style.display = 'none'; $('picker').style.display = 'flex';
  $('pickBack').style.display = S ? '' : 'none';
  if (S) $('pickBackName').textContent = S.folder.split('/').pop();
  const r = await (await fetch('/api/recents')).json();
  $('btnChoose').style.display = r.canChoose ? '' : 'none';
  $('recentsTitle').style.display = r.recents.length ? '' : 'none';
  const list = $('recents'); list.innerHTML = '';
  r.recents.forEach(f => {
    const b = document.createElement('button'); b.className = 'recent';
    const pct = f.total ? Math.round(100 * f.sorted / f.total) : 0;
    b.innerHTML = '<span class="name"></span><span class="path"></span><span class="prog"></span><div class="bar"><div></div></div>';
    b.querySelector('.name').textContent = f.name;
    b.querySelector('.path').textContent = f.path;
    b.querySelector('.prog').textContent = f.sorted >= f.total && f.total ? `All ${f.total} sorted` : `${f.sorted} of ${f.total} sorted`;
    b.querySelector('.bar div').style.width = pct + '%';
    b.onclick = () => openFolder(f.path);
    list.appendChild(b);
  });
}
function hidePicker() { $('picker').style.display = 'none'; $('app').style.display = ''; }
async function openFolder(path) {
  if (!path) return;
  $('pickErr').textContent = '';
  await pending;  // let marks for the current folder land first
  const r = await (await fetch('/api/open', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) })).json();
  if (r.error) { $('pickErr').textContent = r.error; return; }
  location.reload();
}
$('btnChoose').onclick = async () => {
  const b = $('btnChoose'); b.disabled = true; b.textContent = 'Waiting for the folder dialog…';
  try {
    const r = await (await fetch('/api/choose', { method: 'POST' })).json();
    if (r.error) $('pickErr').textContent = r.error;
    else if (r.path) await openFolder(r.path);
  } finally { b.disabled = false; b.textContent = 'Choose Folder…'; }
};
$('pathForm').onsubmit = e => { e.preventDefault(); openFolder($('pathInput').value.trim()); };
$('btnFolder').onclick = showPicker;
$('modeChip').onclick = toggleUnsorted;
$('pickBack').onclick = hidePicker;
$('btnKeep').onclick = () => mark('keep');
$('btnArch').onclick = () => mark('archive');
load();
</script></body></html>
"""


RECENTS_PATH = Path.home() / ".config" / "photocull" / "recent.json"
OSASCRIPT = shutil.which("osascript") if sys.platform == "darwin" else None


def default_archive(folder: Path) -> Path:
    return folder.parent / f"{folder.name}_archive"


def folder_info(folder: Path) -> dict:
    """Name, photo count and sorting progress, for the start page."""
    info = {"path": str(folder), "name": folder.name, "exists": folder.is_dir(), "total": 0, "sorted": 0}
    if info["exists"]:
        try:
            decisions = json.loads((folder / STATE_NAME).read_text()).get("decisions", {})
        except Exception:
            decisions = {}
        here = {p.name for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith(".")}
        archived = {k for k, v in decisions.items() if v.get("status") == "archive"}
        info["total"] = len(here | archived)
        info["sorted"] = sum(1 for k in decisions if k in here or k in archived)
    return info


class App:
    """The folder being sorted, which can change while the server runs."""

    def __init__(self, recents_path: Path = RECENTS_PATH, workers: int = 0):
        self.recents_path = recents_path
        self.workers = workers
        self.lib: Library | None = None
        self.prerender: Prerender | None = None
        self.lock = threading.Lock()

    def open(self, folder: Path, archive: Path | None = None) -> Library:
        folder = folder.expanduser().resolve()
        if not folder.is_dir():
            raise ValueError(f"Not a folder: {folder}")
        lib = Library(folder, (archive.expanduser().resolve() if archive else default_archive(folder)))
        if not lib.names:
            raise ValueError(f"No photos in {folder} (JPG/PNG/HEIC/HIF/WebP/TIFF, top level only)")
        with self.lock:
            if self.prerender:
                self.prerender.stop()
            self.lib = lib
            self.prerender = Prerender(lib, self.workers) if self.workers else None
        self._remember(folder)
        return lib

    def recents(self) -> list[dict]:
        try:
            paths = json.loads(self.recents_path.read_text())
        except Exception:
            paths = []
        return [i for i in (folder_info(Path(p)) for p in paths) if i["exists"]]

    def _remember(self, folder: Path):
        try:
            paths = json.loads(self.recents_path.read_text())
        except Exception:
            paths = []
        paths = [str(folder)] + [p for p in paths if p != str(folder)]
        self.recents_path.parent.mkdir(parents=True, exist_ok=True)
        self.recents_path.write_text(json.dumps(paths[:8], indent=1))


def choose_folder_dialog() -> str | None:
    """The native macOS folder picker. Returns None if cancelled."""
    r = subprocess.run(
        [OSASCRIPT, "-e", 'POSIX path of (choose folder with prompt "Choose a folder of photos to sort")'],
        capture_output=True, text=True,
    )
    return r.stdout.strip() or None if r.returncode == 0 else None


def make_handler(app: App | Library, prerender: Prerender | None = None):
    if isinstance(app, Library):  # a fixed folder (tests, embedding)
        lib, app = app, App(recents_path=Path(tempfile.gettempdir()) / "photocull-unused.json")
        app.lib, app.prerender = lib, prerender

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

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def _file(self, path: Path):
            if not path.exists():
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            data = path.read_bytes()
            try:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # browser dropped a preload it no longer needs

        def do_GET(self):
            route = urlparse(self.path).path
            lib = app.lib  # one folder for the whole request, even if it's switched meanwhile
            if route == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/photos":
                self._json(lib.summary() if lib else {"needsFolder": True})
            elif route == "/api/recents":
                self._json({"recents": app.recents(), "canChoose": bool(OSASCRIPT)})
            elif lib is None:
                self.send_error(404)
            elif route.startswith("/img/"):
                try:
                    i = int(route.split("/")[2])
                    self._file(display_path(lib.current_path(lib.names[i])))
                except Exception:
                    self.send_error(404)
            elif route.startswith("/orig/"):
                # the untouched original — Safari displays HEIC/HIF natively
                try:
                    i = int(route.split("/")[2])
                    self._file(lib.current_path(lib.names[i]))
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
            lib, prerender = app.lib, app.prerender
            if route.startswith("/api/focus/"):
                if prerender:
                    prerender.update(int(route.split("/")[3]), "previews=1" in self.path)
                self.send_response(204)
                self.end_headers()
                return
            try:
                if route == "/api/open":
                    opened = app.open(Path(str(self._body()["path"])))
                    self._json({"ok": True, "folder": str(opened.folder)})
                elif route == "/api/choose":
                    if not OSASCRIPT:
                        raise ValueError("The folder dialog is only available on macOS")
                    self._json({"path": choose_folder_dialog()})
                elif route == "/api/mark" and lib:
                    payload = self._body()
                    if payload.get("session", lib.session) != lib.session:
                        raise ValueError("The folder was changed — that mark wasn't applied")
                    result = lib.mark(int(payload["index"]), str(payload["action"]))
                    result.update({k: lib.summary()[k] for k in ("left", "kept", "archived", "total")})
                    self._json(result)
                else:
                    self.send_error(404)
            except Exception as e:
                self._json({"error": str(e)}, 400)

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="photocull", description="Sort a folder of photos into keep / archive. Nothing is ever deleted.")
    ap.add_argument("folder", type=Path, nargs="?", help="Folder of photos to sort (top level only). Omit to pick one in the browser.")
    ap.add_argument("--archive", type=Path, default=None, help="Archive folder (default: '<folder>_archive' next to it)")
    ap.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    ap.add_argument("--no-open", action="store_true", help="Don't auto-open the browser")
    args = ap.parse_args(argv)

    app = App(workers=max(1, (os.cpu_count() or 2) // 2))
    if args.folder:
        try:
            lib = app.open(args.folder, args.archive)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"PhotoCull: {len(lib.names)} photos in {lib.folder}")
        print(f"  keep    -> stays in {lib.folder}")
        print(f"  archive -> moves to {lib.archive}  (nothing is ever deleted)")
    else:
        print("PhotoCull: pick a folder in the browser.")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    url = f"http://127.0.0.1:{args.port}/"
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
