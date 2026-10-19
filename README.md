# PhotoCull

Sort a folder of photos into **keep** / **archive**, fast — fullscreen photo, filmstrip at the bottom, arrow keys, done.

Nothing is ever deleted. "Archive" just *moves* the file to an archive folder, and you can change your mind at any point (it moves back).

![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-black) ![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Run it

```bash
git clone https://github.com/nkomirisetti/photocull.git
cd photocull
pip install -r requirements.txt

python3 photocull.py                    # pick a folder in the browser
python3 photocull.py ~/Photos/FujiTrip  # or open one directly
```

It starts a local-only web UI at `http://127.0.0.1:8765/` and opens your browser. With no folder it opens a start page: **Choose Folder…** (the native macOS dialog), paste a path, or reopen a recent folder (with how far you got). The 📁 button in the header switches folders any time. Ctrl-C to stop; your progress is saved, so rerunning the same command resumes where you left off.

Options:

```
python3 photocull.py [FOLDER] [--archive FOLDER] [--port 8765] [--no-open]

  --archive   Where archived photos are moved
              (default: a sibling folder named "<folder>_archive")
```

## How sorting works

- The main area shows one photo, full-screen-fit. The strip along the bottom is every photo in the folder (top level only).
- **← / →** — previous / next photo (clicking a thumbnail jumps too)
- **K** — Keep. Green ring on its thumbnail. The file stays where it is.
- **D** (or **X**) — Archive. Red ring, thumbnail desaturates. The file is *moved* to the archive folder — never deleted.
- **U** — Unmark / undo. If it was archived, the file moves back.
- **F** — Full screen: the photo with a small HUD and a dimmed filmstrip overlay (F or Esc to exit).
- **G** — Grid of every photo. Arrows move the selection, K / D / U mark it, Enter (or G, or a click) opens it.
- **N** — Arrows skip photos you've already sorted (and the grid shows only unsorted ones). Press again for all photos; remembered between runs.
- After you mark a photo, it auto-advances to the next unsorted one, so you can just go K, D, D, K… through a whole card.
- Top-left counter: **"7 left to sort"**, plus kept / archived totals and a progress bar.
- Finished state tells you the totals and reminds you nothing was deleted.

Decisions live in `<folder>/.photocull-state.json` (filename → keep/archive), written on every mark — quit mid-sort, come back tomorrow, it's all still there.

## What it supports

JPG, PNG, WebP, TIFF, GIF, HEIC, and Fujifilm HIF. In Safari, HEIC/HIF originals are shown directly at full resolution (hardware-decoded). Other browsers get a JPEG preview converted on the fly; the bottom-right label shows which you're seeing. Thumbnails are rendered in the background (via macOS `sips` for HEIC/HIF), and originals are never touched. Photos are listed in filename order.

Runs on macOS and Linux. The server only listens on `127.0.0.1` — your photos never leave the machine.

## Development

```bash
pip install -e '.[dev]'
pytest
```

The browser tests drive your installed Google Chrome via Playwright and are skipped if it isn't available.

## Ideas / TODO

- [ ] Flag / star rating in addition to keep/archive
- [ ] Optional subfolder recursion
- [ ] Burst grouping (collapse near-duplicates in the strip)
- [ ] EXIF date in the caption

MIT — see [LICENSE](LICENSE).
