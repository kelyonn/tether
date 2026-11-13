import threading

import pytest

pytest.importorskip("playwright.sync_api", reason="install the e2e extra: pip install -e '.[e2e]'")

from playwright.sync_api import sync_playwright

from tether.ui import make_server


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome")       # an installed Chrome, no download
        except Exception:
            try:
                b = p.chromium.launch()                   # or Playwright's own Chromium
            except Exception:
                pytest.skip("no Chrome or Playwright Chromium available")
        yield b
        b.close()


@pytest.fixture
def seeded(store):
    a = store.create_project("Alpha", "first project")
    b = store.create_project("Beta", "second project")
    store.add_task(a["id"], "alpha task one")
    store.add_task(b["id"], "beta task one")
    store.add_memory("Loose idea", "not filed yet")
    return store


@pytest.fixture
def page(browser, seeded):
    httpd, _token = make_server(seeded, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"http://127.0.0.1:{httpd.server_address[1]}/")
    pg.wait_for_selector("#side .brand")
    pg.js_errors = errors
    yield pg
    ctx.close()
    httpd.shutdown()
    httpd.server_close()
