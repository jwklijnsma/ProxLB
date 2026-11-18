#!/usr/bin/env bash
# Regenerate docs/images/proxlb-gui.png from the real plugin code with sample
# data. Needs npm, python3 with playwright and a Chromium/Chrome browser.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d extjs ]; then
    npm pack extjs-gpl@6.2.0 --silent && tar xzf extjs-gpl-6.2.0.tgz && mv package extjs && rm extjs-gpl-6.2.0.tgz
fi
if [ ! -d fa ]; then
    npm pack font-awesome@4.7.0 --silent && tar xzf font-awesome-4.7.0.tgz && mv package fa && rm font-awesome-4.7.0.tgz
fi
python3 - "$PWD/preview.html" "$PWD/../../../../docs/images/proxlb-gui.png" <<'PY'
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=__import__("os").environ.get("CHROME") or None, args=["--allow-file-access-from-files"])
    page = browser.new_page(viewport={"width": 1400, "height": 640}, device_scale_factor=2)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("file://" + sys.argv[1])
    page.wait_for_function("window.READY === true", timeout=20000)
    page.screenshot(path=sys.argv[2])
    browser.close()
    if errors:
        sys.exit("page errors: " + "; ".join(errors))
    print("written", sys.argv[2])
PY
