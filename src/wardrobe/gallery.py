from __future__ import annotations

import webbrowser
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from wardrobe.store import Store

UNSECTIONED = "No section"


def safe_href(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    return escape(url.strip(), quote=True)


def render_gallery(rows: list) -> str:
    boards: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        board_id = row["board_id"]
        if board_id not in boards:
            boards[board_id] = {
                "name": row["board_name"] or "Untitled board",
                "description": row["board_description"] or "",
                "privacy": row["board_privacy"] or "",
                "sections": {},
            }
            order.append(board_id)
        if row["pin_id"] is None:
            continue
        section_name = row["section_name"] or UNSECTIONED
        sections = boards[board_id]["sections"]
        pins = sections.setdefault(section_name, {})
        pin = pins.setdefault(
            row["pin_id"],
            {
                "title": row["pin_title"] or "",
                "alt": row["alt_text"] or row["pin_title"] or "",
                "link": row["link"],
                "media_type": row["media_type"],
                "color": row["dominant_color"],
                "images": [],
            },
        )
        if row["local_path"]:
            pin["images"].append(row["local_path"])

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>Wardrobe</title>",
        "<style>",
        _CSS,
        "</style>",
        "</head>",
        "<body>",
        "<h1>Wardrobe</h1>",
        "<p class=\"lede\">Pins currently on the connected account. Missing pins stay in the archive and are hidden here.</p>",
    ]
    if not order:
        parts.append("<p>No boards yet. Run <code>wardrobe sync</code> first.</p>")
    for board_id in order:
        board = boards[board_id]
        privacy = f" · {escape(board['privacy'])}" if board["privacy"] else ""
        parts.append("<section class=\"board\">")
        parts.append(f"<h2>{escape(board['name'])}{privacy}</h2>")
        if board["description"]:
            parts.append(f"<p class=\"desc\">{escape(board['description'])}</p>")
        sections = board["sections"]
        if not sections:
            parts.append("<p class=\"empty\">No pins.</p>")
        for section_name, pins in sections.items():
            parts.append("<div class=\"section\">")
            parts.append(f"<h3>{escape(section_name)}</h3>")
            parts.append("<div class=\"grid\">")
            for pin in pins.values():
                parts.append(_render_pin(pin))
            parts.append("</div></div>")
        parts.append("</section>")
    parts.append("</body></html>")
    return "\n".join(parts)


def _render_pin(pin: dict) -> str:
    title = escape(pin["title"]) if pin["title"] else "Untitled pin"
    href = safe_href(pin["link"])
    heading = f'<a href="{href}">{title}</a>' if href else title
    color = pin["color"] if _is_hex_color(pin["color"]) else None
    swatch = (
        f'<span class="swatch" style="background:{escape(color, quote=True)}"></span>'
        if color
        else ""
    )
    media = escape(pin["media_type"] or "")
    images = []
    alt = escape(pin["alt"], quote=True)
    for relative in pin["images"]:
        src = escape(relative, quote=True)
        images.append(f'<img src="{src}" alt="{alt}">')
    if not images:
        images.append(f'<p class="nomedia">{media or "No image"}</p>')
    return (
        '<article class="pin">'
        + "".join(images)
        + f"<h4>{swatch}{heading}</h4>"
        + (f'<p class="meta">{media}</p>' if media else "")
        + "</article>"
    )


def _is_hex_color(value: str | None) -> bool:
    if not value or not value.startswith("#"):
        return False
    hex_digits = value[1:]
    return len(hex_digits) in {3, 6} and all(char in "0123456789abcdefABCDEF" for char in hex_digits)


_CSS = """
body { margin: 2rem auto; max-width: 1100px; padding: 0 1rem;
       font-family: Georgia, "Iowan Old Style", serif; background: #f6f3ee; color: #1c1917; }
h1 { font-weight: normal; }
.lede, .desc, .empty, .meta, .nomedia { color: #57534e; }
.board { margin: 2.5rem 0; }
.section { margin-top: 1.25rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 1rem; }
.pin { background: #fff; border: 1px solid #e7e5e4; padding: 0.5rem; }
.pin img { width: 100%; height: auto; display: block; background: #eee; }
.pin h4 { font-size: 0.95rem; font-weight: normal; margin: 0.5rem 0 0; }
.swatch { display: inline-block; width: 0.7rem; height: 0.7rem; margin-right: 0.35rem;
          border: 1px solid #d6d3d1; vertical-align: baseline; }
a { color: #9a3412; }
code { font-family: ui-monospace, monospace; }
"""


def write_gallery(store: Store, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_gallery(store.gallery_rows()), encoding="utf-8")
    return path


def open_gallery(path: Path) -> None:
    webbrowser.open(path.resolve().as_uri())
