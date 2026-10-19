# PhotoCull

Sort a folder of photos into **keep** / **archive**, fast — fullscreen photo, filmstrip at the bottom, arrow keys, done.

Nothing is ever deleted. "Archive" just *moves* the file to an archive folder, and you can change your mind at any point (it moves back).

![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-black) ![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Run it

```bash
git clone https://github.com/nkomirisetti/photocull.git
cd photocull
pip install -r requirements.txt

python3 photocull.py ~/Photos/FujiTrip
```

It starts a local-only web UI at `http://127.0.0.1:8765/` and opens your browser. Ctrl-C to stop; your progress is saved, so rerunning the same command resumes where you left off.

Options:

```
python3 photocull.py FOLDER [--archive FOLDER] [--port 8765] [--no-open]

  --archive   Where archived photos are moved
              (default: a sibling folder named "<folder>_archive")
```

## How sorting works

- The main area shows one photo, full-screen-fit. The strip along the bottom is every photo in the folder (top level only).
- **← / →** — previous / next photo (clicking a thumbnail jumps too)
- **K** — Keep. Green ring on its thumbnail. The file stays where it is.
- **D** (or **X**) — Archive. Red ring, thumbnail desaturates. The file is *moved* to the archive folder — never deleted.
- **U** — Unmark / undo. If it was archived, the file moves back.
- After you mark a photo, it auto-advances to the next unsorted one, so you can just go K, D, D, K… through a whole card.
- Top-left counter: **"7 left to sort"**, plus kept / archived totals and a progress bar.
- Finished state tells you the totals and reminds you nothing was deleted.

Decisions live in `<folder>/.photocull-state.json` (filename → keep/archive), written on every mark — quit mid-sort, come back tomorrow, it's all still there.

## What it supports

JPG, PNG, WebP, TIFF, GIF, and HEIC (HEIC thumbnails need `pip install pillow-heif`; full-size HEIC display depends on your browser — Safari handles it). Photos are listed in filename order.

Runs on macOS and Linux. The server only listens on `127.0.0.1` — your photos never leave the machine.

## Ideas / TODO

- [ ] Flag / star rating in addition to keep/archive
- [ ] Optional subfolder recursion
- [ ] Burst grouping (collapse near-duplicates in the strip)
- [ ] EXIF date in the caption

MIT — see [LICENSE](LICENSE).
