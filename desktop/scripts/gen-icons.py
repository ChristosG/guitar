#!/usr/bin/env python3
"""Generate the placeholder icon set Tauri requires, under desktop/src-tauri/icons/.

Produces: icon.png (512), 128x128.png, 128x128@2x.png (256), 32x32.png, icon.icns
(ICNS with 128/256/512 PNG members — modern ICNS members are literal PNG blobs,
so no Apple tooling is needed to write one).

The artwork is a deliberate placeholder: a dark rounded square, an amber
sound-hole ring, six strings. To replace it with real artwork, drop a 1024x1024
PNG next to this script as `icon-source.png` and re-run — or regenerate the
whole set from any square PNG with the official tooling:

    cargo tauri icon path/to/icon.png     # writes desktop/src-tauri/icons/*

Requires Pillow (any version); stdlib-only ICNS packing.
"""
from __future__ import annotations

import pathlib
import struct

from PIL import Image, ImageDraw

ICONS = pathlib.Path(__file__).resolve().parent.parent / "src-tauri" / "icons"
SOURCE = pathlib.Path(__file__).resolve().parent / "icon-source.png"

BG = (31, 34, 51, 255)        # dark indigo
RING = (232, 163, 61, 255)    # amber
HOLE = (22, 24, 38, 255)      # darker center
STRING = (222, 224, 235, 235)  # ivory


def render_base(size: int = 1024) -> Image.Image:
    if SOURCE.exists():
        return Image.open(SOURCE).convert("RGBA").resize((size, size), Image.LANCZOS)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size // 16
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=size // 5, fill=BG)
    cx = cy = size // 2
    r_outer, r_inner = int(size * 0.30), int(size * 0.22)
    d.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], fill=RING)
    d.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], fill=HOLE)
    # six strings, vertical, across the body
    w = max(2, size // 160)
    span = int(size * 0.42)
    for i in range(6):
        x = cx - span // 2 + i * span // 5
        d.rectangle([x - w // 2, pad * 3, x + w // 2, size - pad * 3], fill=STRING)
    return img


def icns(pngs: dict[str, bytes]) -> bytes:
    """Pack {ostype: png_bytes} into an ICNS container (ic07=128 ic08=256 ic09=512)."""
    body = b""
    for ostype, data in pngs.items():
        body += ostype.encode("ascii") + struct.pack(">I", 8 + len(data)) + data
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    base = render_base(1024)
    sizes = {"icon.png": 512, "128x128.png": 128, "128x128@2x.png": 256, "32x32.png": 32}
    rendered: dict[int, pathlib.Path] = {}
    for name, px in sizes.items():
        out = ICONS / name
        base.resize((px, px), Image.LANCZOS).save(out)
        rendered[px] = out
    members = {}
    for ostype, px in (("ic07", 128), ("ic08", 256), ("ic09", 512)):
        members[ostype] = rendered[px].read_bytes()
    (ICONS / "icon.icns").write_bytes(icns(members))
    print(f"wrote {len(sizes) + 1} icons to {ICONS}")


if __name__ == "__main__":
    main()
