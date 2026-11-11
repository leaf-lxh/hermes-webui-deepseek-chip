#!/usr/bin/env python3
"""Build preview/standalone.html from preview.html + assets.

WebUI's HTML preview runs files in a sandboxed iframe (opaque origin), so a
page that loads ../assets/*.css|js makes unauthenticated subresource requests:
behind a Basic-auth reverse proxy that pops the nginx login dialog, and the
relative path resolves under /api/ anyway. The standalone file inlines both
assets, so the iframe makes zero sub-requests and renders as-is.

Usage: python3 scripts/build_preview.py
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "preview" / "preview.html"
OUT = ROOT / "preview" / "standalone.html"
CSS = ROOT / "assets" / "deepseek-quota.css"
JS = ROOT / "assets" / "deepseek-quota.js"


def main() -> int:
    html = SRC.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    if "</style>" in css or "</script>" in js:
        raise SystemExit("asset contains a closing tag; refusing to inline")

    link = '<link rel="stylesheet" href="../assets/deepseek-quota.css">'
    script = '<script src="../assets/deepseek-quota.js"></script>'
    if link not in html or script not in html:
        raise SystemExit("preview.html shape changed; update build_preview.py")

    html = html.replace(link, "<style>\n" + css + "\n</style>")
    html = html.replace(script, "<script>\n" + js + "\n</script>")
    html = html.replace(
        "<title>DeepSeek chip preview</title>",
        "<title>DeepSeek chip preview (standalone)</title>",
    )
    if "../assets/" in html:
        raise SystemExit("external asset reference remains after inlining")

    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT} ({len(html)} bytes, {html.count('</script>')} script blocks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
