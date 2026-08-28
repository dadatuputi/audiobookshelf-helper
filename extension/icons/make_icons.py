#!/usr/bin/env python3
"""
Generate the extension icon set.

Both stores require icons, and CI has to be able to rebuild them, so this
writes PNGs with nothing but the standard library - no Pillow, no ImageMagick.
The mark is drawn at 8x and box-filtered down, which is enough antialiasing to
stay legible at 16px.

    python3 extension/icons/make_icons.py

The design is the same "⤓ into a tray" idea as the injected toolbar button: a
downward arrow landing on a shelf, on a rounded indigo tile.
"""
import struct, zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIZES = (16, 32, 48, 96, 128)
SS = 8  # supersampling factor

BG = (79, 70, 229)      # indigo-600, reads as a single solid shape at 16px
FG = (255, 255, 255)


def rounded_rect(x, y, w, h, r):
    """Signed-ish membership test for a rounded rectangle."""
    def inside(px, py):
        if not (x <= px <= x + w and y <= py <= y + h):
            return False
        cx = min(max(px, x + r), x + w - r)
        cy = min(max(py, y + r), y + h - r)
        dx, dy = px - cx, py - cy
        return dx * dx + dy * dy <= r * r or (x + r <= px <= x + w - r) or (y + r <= py <= y + h - r)
    return inside


def draw(n):
    """Render one n*SS square of (r,g,b,a) tuples."""
    N = n * SS
    u = N / 100.0                      # work in a 0..100 design space
    px = [[(0, 0, 0, 0)] * N for _ in range(N)]

    tile = rounded_rect(3 * u, 3 * u, 94 * u, 94 * u, 22 * u)

    # Arrow shaft + head pointing down onto a shelf bar.
    shaft_x0, shaft_x1 = 44 * u, 56 * u
    shaft_y0, shaft_y1 = 20 * u, 55 * u
    head_y0, head_y1 = 55 * u, 74 * u
    head_hw = 19 * u
    bar_y0, bar_y1 = 80 * u, 88 * u
    bar_x0, bar_x1 = 28 * u, 72 * u

    for yy in range(N):
        row = px[yy]
        fy = yy + 0.5
        for xx in range(N):
            fx = xx + 0.5
            if not tile(fx, fy):
                continue
            col = BG
            if shaft_x0 <= fx <= shaft_x1 and shaft_y0 <= fy <= shaft_y1:
                col = FG
            elif head_y0 <= fy <= head_y1:
                # triangle narrowing to a point at head_y1
                t = (fy - head_y0) / (head_y1 - head_y0)
                hw = head_hw * (1 - t)
                if abs(fx - 50 * u) <= hw:
                    col = FG
            elif bar_y0 <= fy <= bar_y1 and bar_x0 <= fx <= bar_x1:
                col = FG
            row[xx] = (col[0], col[1], col[2], 255)
    return px


def downsample(px, n):
    """Box-filter the SS-scaled image to n x n, averaging colour and alpha."""
    out = []
    for y in range(n):
        row = bytearray()
        for x in range(n):
            r = g = b = a = 0
            for dy in range(SS):
                src = px[y * SS + dy]
                for dx in range(SS):
                    pr, pg, pb, pa = src[x * SS + dx]
                    # premultiply so transparent pixels do not darken the edge
                    r += pr * pa; g += pg * pa; b += pb * pa; a += pa
            if a:
                row += bytes((round(r / a), round(g / a), round(b / a),
                              round(a / (SS * SS * 255) * 255)))
            else:
                row += b"\0\0\0\0"
        out.append(bytes(row))
    return out


def write_png(path, rows, n):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    raw = b"".join(b"\0" + r for r in rows)          # filter type 0 per row
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def main():
    for n in SIZES:
        write_png(HERE / f"icon-{n}.png", downsample(draw(n), n), n)
        print(f"  wrote icon-{n}.png")


if __name__ == "__main__":
    main()
