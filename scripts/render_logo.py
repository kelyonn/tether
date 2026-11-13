"""Render the logo to PNG for places that cannot use SVG (the Desktop extension icon).

    python scripts/render_logo.py        # needs: pip install playwright (and Chrome or Chromium)

Source of truth: docs/readme/logo.svg. Writes extension/icon.png (512x512, square).
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parent.parent
svg = (root / "docs" / "readme" / "logo.svg").read_text()
# A plain square: the OS rounds app icons itself.
big = svg.replace('width="64" height="64"', 'width="512" height="512"').replace('rx="14"', 'rx="0"')
html = f'<body style="margin:0;background:transparent">{big}</body>'
with sync_playwright() as p:
    try:
        browser = p.chromium.launch(channel="chrome")
    except Exception:
        browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 512, "height": 512})
    page.set_content(html)
    page.screenshot(path=str(root / "extension" / "icon.png"), omit_background=True,
                    clip={"x": 0, "y": 0, "width": 512, "height": 512})
    browser.close()
print("wrote extension/icon.png")
