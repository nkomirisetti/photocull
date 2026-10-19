"""Photo-safety tests.

    pip install -e '.[dev]'
    pytest

The browser test drives your installed Google Chrome via Playwright and is
skipped if Playwright or Chrome isn't available.
"""

from __future__ import annotations

import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

import photocull


def make_photos(folder: Path, n: int, color="red") -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        p = folder / f"IMG_{i:03}.jpg"
        Image.new("RGB", (64, 48), color).save(p)
        paths.append(p)
    return paths


@pytest.fixture
def lib(tmp_path):
    make_photos(tmp_path / "photos", 3)
    return photocull.Library(tmp_path / "photos", tmp_path / "photos_archive")


@pytest.mark.parametrize("undo_action", ["clear", "keep"])
def test_unarchive_never_overwrites_a_newer_same_name_photo(lib, undo_action):
    """Archive IMG_001, a *different* IMG_001 shows up in the folder (e.g. a
    re-import), then undo. The undo must refuse rather than overwrite it."""
    lib.mark(1, "archive")
    archived = lib.archive / "IMG_001.jpg"
    original_bytes = archived.read_bytes()

    newcomer = lib.folder / "IMG_001.jpg"
    Image.new("RGB", (64, 48), "yellow").save(newcomer)
    newcomer_bytes = newcomer.read_bytes()

    with pytest.raises(FileExistsError):
        lib.mark(1, undo_action)

    # Both photos still exist, untouched, and the app still knows where the archived one is.
    assert newcomer.read_bytes() == newcomer_bytes
    assert archived.read_bytes() == original_bytes
    assert lib.decisions["IMG_001.jpg"]["status"] == "archive"


def test_archive_and_undo_round_trip_keeps_every_photo(lib):
    before = {p.name: p.read_bytes() for p in lib.folder.glob("*.jpg")}
    lib.mark(0, "keep")
    lib.mark(1, "archive")
    lib.mark(2, "archive")
    lib.mark(2, "clear")
    lib.mark(1, "keep")
    after = {p.name: p.read_bytes() for p in lib.folder.glob("*.jpg")}
    assert after == before
    assert not any(lib.archive.glob("*.jpg"))


# ---------------------------------------------------------------------------
# Browser: fast keypresses must each apply to the photo that was on screen
# ---------------------------------------------------------------------------


@pytest.fixture
def server(lib):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), photocull.make_handler(lib))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Exception as e:
            pytest.skip(f"Google Chrome not available to Playwright: {e}")
        yield browser.new_page()
        browser.close()


def test_rapid_keypresses_mark_the_photo_on_screen(lib, server, page):
    page.goto(server)
    page.wait_for_selector("#th2")

    # Culling at full speed: no waiting for the server between keys.
    for key in ["k", "d", "d"]:
        page.keyboard.press(key)

    deadline = time.time() + 5
    while time.time() < deadline and len(lib.decisions) < 3:
        time.sleep(0.05)

    statuses = [lib.decisions.get(n, {}).get("status") for n in lib.names]
    assert statuses == ["keep", "archive", "archive"]
    assert (lib.folder / "IMG_000.jpg").exists()
    assert (lib.archive / "IMG_001.jpg").exists()
    assert (lib.archive / "IMG_002.jpg").exists()


def test_failed_restore_shows_an_error_in_the_ui(lib, server, page):
    lib.mark(0, "archive")
    Image.new("RGB", (64, 48), "yellow").save(lib.folder / "IMG_000.jpg")

    page.goto(server)
    page.wait_for_selector("#th0")
    page.click("#th0")
    page.keyboard.press("u")

    toast = page.locator("#toast")
    toast.wait_for(state="visible", timeout=5000)
    assert "already exists" in toast.inner_text()
    # UI resynced to the server: the photo is still shown as archived.
    assert "arch" in page.get_attribute("#th0", "class")


# ---------------------------------------------------------------------------
# Fujifilm HIF (HEIF) support
# ---------------------------------------------------------------------------


def fetch(url: str) -> tuple[str, bytes]:
    from urllib.request import urlopen

    with urlopen(url) as r:
        return r.headers["Content-Type"], r.read()


@pytest.fixture
def hif_lib(tmp_path):
    folder = tmp_path / "fuji"
    folder.mkdir()
    Image.new("RGB", (600, 400), "teal").save(folder / "DSCF0001.HIF", format="HEIF")
    Image.new("RGB", (600, 400), "navy").save(folder / "DSCF0002.HIF", format="HEIF")
    return photocull.Library(folder, tmp_path / "fuji_archive")


def test_hif_files_are_listed(hif_lib):
    assert hif_lib.names == ["DSCF0001.HIF", "DSCF0002.HIF"]


def test_hif_is_served_as_jpeg_for_any_browser(hif_lib):
    import io

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), photocull.make_handler(hif_lib))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        for route in ("/img/0", "/thumb/0"):
            ctype, body = fetch(base + route)
            assert ctype == "image/jpeg"
            with Image.open(io.BytesIO(body)) as im:
                assert im.format == "JPEG"
                assert im.width > im.height  # landscape preserved
    finally:
        httpd.shutdown()


def test_hif_archive_round_trip_keeps_original_bytes(hif_lib):
    original = (hif_lib.folder / "DSCF0001.HIF").read_bytes()
    hif_lib.mark(0, "archive")
    assert (hif_lib.archive / "DSCF0001.HIF").read_bytes() == original
    hif_lib.mark(0, "clear")
    assert (hif_lib.folder / "DSCF0001.HIF").read_bytes() == original


def test_thumbnails_respect_exif_orientation(tmp_path):
    """Portrait shots are stored landscape + an 'rotate 90°' EXIF tag."""
    p = tmp_path / "portrait.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° CW
    Image.new("RGB", (600, 400), "red").save(p, exif=exif)
    with Image.open(photocull.thumb_for(p)) as th:
        assert th.height > th.width


def test_restart_on_another_folder_never_shows_cached_photos(tmp_path, page):
    """Same port, different folder: the browser must not reuse the previous
    folder's cached /img/0 — you'd be judging the wrong photo."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    def shown_color():
        page.wait_for_function("document.getElementById('photo').complete && document.getElementById('photo').naturalWidth > 0")
        return page.evaluate("""() => {
            const img = document.getElementById('photo'), c = document.createElement('canvas');
            c.width = img.naturalWidth; c.height = img.naturalHeight;
            const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0);
            return Array.from(ctx.getImageData(5, 5, 1, 1).data.slice(0, 3));
        }""")

    for color, expected in [("red", (255, 0, 0)), ("blue", (0, 0, 255))]:
        make_photos(tmp_path / color, 1, color)
        lib = photocull.Library(tmp_path / color, tmp_path / f"{color}_archive")
        httpd = ThreadingHTTPServer(("127.0.0.1", port), photocull.make_handler(lib))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            page.goto(f"http://127.0.0.1:{port}/")
            r, g, b = shown_color()
            assert max(abs(r - expected[0]), abs(g - expected[1]), abs(b - expected[2])) < 40, (color, (r, g, b))
        finally:
            httpd.shutdown()
            httpd.server_close()
