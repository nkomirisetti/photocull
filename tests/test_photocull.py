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
def server_for():
    servers = []

    def start(library):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), photocull.make_handler(library))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_port}"

    yield start
    for h in servers:
        h.shutdown()


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


# ---------------------------------------------------------------------------
# Rendering speed: dedupe, cache reuse, background prerender
# ---------------------------------------------------------------------------


def count_decodes(monkeypatch) -> list:
    calls = []
    real_render = photocull._render

    def spy(path, size, out):
        calls.append(path)
        return real_render(path, size, out)

    monkeypatch.setattr(photocull, "_render", spy)
    return calls


def test_concurrent_requests_decode_once_and_get_a_whole_file(hif_lib, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    calls = count_decodes(monkeypatch)
    path = hif_lib.folder / "DSCF0001.HIF"
    with ThreadPoolExecutor(8) as ex:
        outs = list(ex.map(lambda _: photocull.display_path(path), range(8)))
    assert len(set(outs)) == 1
    assert len(calls) == 1
    with Image.open(outs[0]) as im:
        im.load()  # a truncated file would raise here


def test_archiving_keeps_the_cached_render(hif_lib, monkeypatch):
    photocull.display_path(hif_lib.current_path("DSCF0001.HIF"))
    calls = count_decodes(monkeypatch)
    hif_lib.mark(0, "archive")
    photocull.display_path(hif_lib.current_path("DSCF0001.HIF"))
    assert calls == []


def test_prerender_renders_everything_starting_near_the_viewer(tmp_path, monkeypatch):
    folder = tmp_path / "many"
    folder.mkdir()
    for i in range(10):
        Image.new("RGB", (120, 80), (i * 20, 0, 0)).save(folder / f"P{i:02}.HIF", format="HEIF")
    lib = photocull.Library(folder, tmp_path / "many_archive")
    calls = count_decodes(monkeypatch)

    pre = photocull.Prerender(lib, workers=0)  # no threads: drive it by hand
    pre.update(focus=6, previews=True)
    pre._run(wait=False)

    first = [Path(c).name for c in calls[:4]]
    assert first == ["P06.HIF", "P06.HIF", "P07.HIF", "P07.HIF"]  # preview + thumb, viewer first
    for name in lib.names:
        p = folder / name
        assert photocull.display_path(p).exists() and photocull.thumb_for(p).exists()
    assert len(calls) == 20  # nothing rendered twice


def test_prerender_skips_previews_until_the_browser_needs_them(hif_lib, monkeypatch):
    calls = count_decodes(monkeypatch)
    pre = photocull.Prerender(hif_lib, workers=0)
    pre._run(wait=False)
    assert len(calls) == 2  # thumbnails only: Safari shows the originals itself
    pre.update(focus=0, previews=True)
    pre._run(wait=False)
    assert len(calls) == 4


def test_originals_are_served_untouched_with_a_heif_type(hif_lib, server_for):
    base = server_for(hif_lib)
    ctype, body = fetch(base + "/orig/0")
    assert ctype == "image/heif"
    assert body == (hif_lib.folder / "DSCF0001.HIF").read_bytes()


@pytest.mark.skipif(photocull.SIPS is None, reason="macOS only")
def test_hif_thumbnails_use_sips_on_macos(hif_lib, monkeypatch):
    def no_pillow(*a, **kw):
        raise AssertionError("fell back to Pillow")

    monkeypatch.setattr(photocull.Image, "open", no_pillow)
    out = photocull.thumb_for(hif_lib.folder / "DSCF0002.HIF")
    monkeypatch.undo()
    with Image.open(out) as im:
        assert im.format == "JPEG" and max(im.size) == 320


def test_browser_without_heif_falls_back_to_preview(hif_lib, server_for, page):
    # Playwright's Chrome can't decode HEIF, so it must switch to the JPEG preview.
    page.goto(server_for(hif_lib))
    page.wait_for_function("document.getElementById('res').textContent.includes('preview')")
    assert "/img/" in page.get_attribute("#photo", "src")


def test_f_toggles_full_screen(lib, server, page):
    page.goto(server)
    page.wait_for_selector("#th0")
    page.keyboard.press("f")
    page.wait_for_function("document.body.classList.contains('fs')")
    assert not page.is_visible("header")
    # filmstrip floats over the photo, dimmed
    assert page.is_visible("#strip")
    assert page.evaluate("getComputedStyle(document.getElementById('strip')).position") == "fixed"
    page.wait_for_function("parseFloat(getComputedStyle(document.getElementById('strip')).opacity) < 0.6")
    assert page.is_visible("#hud")
    assert "3 left" in page.inner_text("#hud")
    page.keyboard.press("k")
    page.wait_for_function("document.getElementById('hud').innerText.includes('1 kept')")
    page.keyboard.press("f")
    page.wait_for_function("!document.body.classList.contains('fs')")
    assert page.is_visible("#strip")


def test_next_photo_is_already_loaded_before_you_move_to_it(lib, server, page):
    page.goto(server)
    page.wait_for_selector("#th2")
    # neighbours are fetched and decoded in the background
    page.wait_for_function("[...document.querySelectorAll('.photo')].length === 3 && [...document.querySelectorAll('.photo')].every(i => i.complete && i.naturalWidth)")
    next_el = page.evaluate_handle("document.querySelectorAll('.photo')[1]")

    requests = []
    page.on("request", lambda r: requests.append(r.url))
    page.keyboard.press("ArrowRight")

    assert page.evaluate("el => el.id === 'photo' && el.classList.contains('shown')", next_el)
    assert not [u for u in requests if "/orig/1?" in u or "/img/1?" in u]  # no refetch of the photo now shown


# ---------------------------------------------------------------------------
# Start page: picking and switching folders
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    return photocull.App(recents_path=tmp_path / "recent.json")


def post(url: str, payload: dict | None = None) -> dict:
    import json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    req = Request(url, data=json.dumps(payload or {}).encode(), headers={"Content-Type": "application/json"})
    try:
        with urlopen(req) as r:
            return json.loads(r.read())
    except HTTPError as e:
        return json.loads(e.read())


def test_open_rejects_missing_and_empty_folders(app, tmp_path):
    with pytest.raises(ValueError, match="Not a folder"):
        app.open(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="No photos"):
        app.open(tmp_path / "empty")
    assert app.lib is None and app.recents() == []


def test_recents_are_most_recent_first_with_progress(app, tmp_path):
    make_photos(tmp_path / "a", 4)
    make_photos(tmp_path / "b", 2)
    app.open(tmp_path / "a").mark(0, "archive")
    app.open(tmp_path / "b")
    app.open(tmp_path / "a")
    recents = app.recents()
    assert [r["name"] for r in recents] == ["a", "b"]
    assert (recents[0]["sorted"], recents[0]["total"]) == (1, 4)  # archived photo still counts


def test_a_mark_meant_for_the_previous_folder_is_refused(app, tmp_path, server_for):
    make_photos(tmp_path / "a", 2)
    make_photos(tmp_path / "b", 2)
    old = app.open(tmp_path / "a")
    base = server_for(app)
    assert post(base + "/api/open", {"path": str(tmp_path / "b")})["ok"]

    r = post(base + "/api/mark", {"index": 0, "action": "archive", "session": old.session})
    assert "folder was changed" in r["error"]
    assert (tmp_path / "b" / "IMG_000.jpg").exists()  # nothing moved in the new folder


def test_switching_folders_stops_the_old_background_renderer(tmp_path):
    make_photos(tmp_path / "a", 2)
    make_photos(tmp_path / "b", 2)
    app = photocull.App(recents_path=tmp_path / "recent.json", workers=1)
    app.open(tmp_path / "a")
    old = app.prerender
    app.open(tmp_path / "b")
    assert old.stopped and old._next_job() is None


def test_start_page_picks_a_folder_and_keys_dont_mark_while_typing(app, tmp_path, server_for, page):
    make_photos(tmp_path / "trip", 3)
    page.goto(server_for(app))
    page.wait_for_selector("#picker", state="visible")
    assert not page.is_visible("#app")

    page.fill("#pathInput", "/nowhere/at/all")
    page.keyboard.press("Enter")
    page.wait_for_function("document.getElementById('pickErr').textContent.includes('Not a folder')")

    page.fill("#pathInput", "")
    page.type("#pathInput", str(tmp_path / "trip"))  # real keystrokes: typing k/d must not mark
    page.keyboard.press("Enter")
    page.wait_for_selector("#th2")
    assert app.lib.folder == (tmp_path / "trip").resolve()
    assert app.lib.decisions == {}

    page.click("#btnFolder")
    page.wait_for_selector("#picker", state="visible")
    page.wait_for_function("document.getElementById('recents').innerText.includes('0 of 3 sorted')")
    assert "trip" in page.inner_text("#recents")
    page.keyboard.press("d")  # picker is open: must not archive anything
    page.click("#pickBack")
    page.wait_for_selector("#app", state="visible")
    assert app.lib.decisions == {}



# ---------------------------------------------------------------------------
# N (unsorted-only navigation) and G (grid view)
# ---------------------------------------------------------------------------


@pytest.fixture
def five(tmp_path):
    make_photos(tmp_path / "five", 5)
    lib = photocull.Library(tmp_path / "five", tmp_path / "five_archive")
    lib.mark(1, "keep")
    lib.mark(2, "archive")
    return lib


def caption_index(page) -> int:
    return int(page.inner_text("#caption").split(" / ")[0]) - 1


def test_n_makes_arrows_skip_sorted_photos(five, server_for, page):
    page.goto(server_for(five))
    page.wait_for_selector("#th4")
    assert caption_index(page) == 0

    page.keyboard.press("n")
    assert page.is_visible("#modeChip")
    page.keyboard.press("ArrowRight")
    assert caption_index(page) == 3  # skipped the kept #1 and archived #2
    page.keyboard.press("ArrowLeft")
    assert caption_index(page) == 0

    page.keyboard.press("n")
    assert not page.is_visible("#modeChip")
    page.keyboard.press("ArrowRight")
    assert caption_index(page) == 1  # every photo again


def test_n_is_remembered_after_reload(five, server_for, page):
    page.goto(server_for(five))
    page.wait_for_selector("#th4")
    page.keyboard.press("n")
    page.reload()
    page.wait_for_selector("#th4")
    assert page.is_visible("#modeChip")


def test_g_grid_marks_and_opens_photos(five, server_for, page):
    page.goto(server_for(five))
    page.wait_for_selector("#th4")
    page.keyboard.press("g")
    page.wait_for_selector("#gc4")
    assert not page.is_visible("main")
    assert page.locator(".cell").count() == 5

    page.keyboard.press("ArrowRight")
    page.keyboard.press("k")  # marks the selected cell (#1, already kept: stays kept) then moves on
    page.keyboard.press("ArrowRight")
    sel = int(page.get_attribute(".cell.current", "id")[2:])
    page.keyboard.press("d")
    deadline = time.time() + 5
    while time.time() < deadline and five.decisions.get(five.names[sel], {}).get("status") != "archive":
        time.sleep(0.05)
    assert five.decisions[five.names[sel]]["status"] == "archive"
    assert "arch" in page.get_attribute(f"#gc{sel}", "class")

    page.keyboard.press("Enter")
    page.wait_for_selector("main", state="visible")
    assert not page.is_visible("#grid")


def test_grid_with_n_shows_only_unsorted(five, server_for, page):
    page.goto(server_for(five))
    page.wait_for_selector("#th4")
    page.keyboard.press("n")
    page.keyboard.press("g")
    page.wait_for_selector("#gc4")
    assert sorted(int(c[2:]) for c in page.eval_on_selector_all(".cell", "els => els.map(e => e.id)")) == [0, 3, 4]
    page.keyboard.press("k")  # keep #0: it drops out of the grid
    page.wait_for_function("!document.getElementById('gc0')")
    page.click("#gc4")
    page.wait_for_selector("main", state="visible")
    assert caption_index(page) == 4
