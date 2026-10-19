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
