"""Генератор `noosphere.ico` — stdlib (zlib + struct), без Pillow.

Под Python 3.14 колёс Pillow может не быть, а иконка нужна и трею, и ярлыкам.
Рисуем процедурно 256×256 RGBA в палитре фронта (calm-light, коммит bce2462):
скруглённый квадрат лесным акцентом #155D4B, белая «Н», янтарная точка сигнала.
Размеры 16/32/48/256 пакуются в один ICO как PNG-блоки — Windows 10/11 это
понимает, а меньшие размеры получены усреднением по блоку (без «пилы»).

Перегенерировать: `python client\\desktop\\make_icon.py`
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

try:
    from . import appconfig
except ImportError:      # запуск как скрипт: python client\desktop\make_icon.py
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from desktop import appconfig

SIZES = (256, 48, 32, 16)

ACCENT = (0x15, 0x5D, 0x4B, 255)      # лесной акцент фронта
PAPER = (0xF4, 0xF7, 0xF2, 255)       # почти белый из палитры
AMBER = (0xE8, 0xB3, 0x3C, 255)       # точка сигнала (warn-графика)
CLEAR = (0, 0, 0, 0)


def _inside_rounded(x: float, y: float, size: int, radius: float) -> bool:
    lo, hi = radius, size - radius
    if lo <= x <= hi or lo <= y <= hi:
        return 0 <= x < size and 0 <= y < size
    cx = lo if x < lo else hi
    cy = lo if y < lo else hi
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def draw(size: int) -> bytearray:
    """Один кадр RGBA: фон-плитка, буква «Н», янтарная точка."""
    px = bytearray(CLEAR * size * size)
    radius = size * 0.22
    # «Н»: две вертикали и средняя перекладина, доли от размера
    bars = ((0.30, 0.42, 0.26, 0.74), (0.58, 0.70, 0.26, 0.74), (0.30, 0.70, 0.44, 0.56))
    dot_cx, dot_cy, dot_r = size * 0.795, size * 0.235, size * 0.085

    for y in range(size):
        row = y * size * 4
        for x in range(size):
            if not _inside_rounded(x + 0.5, y + 0.5, size, radius):
                continue
            fx, fy = x / size, y / size
            color = ACCENT
            if any(x0 <= fx <= x1 and y0 <= fy <= y1 for x0, x1, y0, y1 in bars):
                color = PAPER
            elif (x + 0.5 - dot_cx) ** 2 + (y + 0.5 - dot_cy) ** 2 <= dot_r ** 2:
                color = AMBER
            i = row + x * 4
            px[i:i + 4] = bytes(color)
    return px


def downscale(src: bytearray, src_size: int, dst_size: int) -> bytearray:
    """Усреднение по блоку: честный альфа-канал на краях скругления."""
    if dst_size == src_size:
        return bytearray(src)
    out = bytearray(CLEAR * dst_size * dst_size)
    step = src_size / dst_size
    for dy in range(dst_size):
        for dx in range(dst_size):
            y0, y1 = int(dy * step), max(int(dy * step) + 1, int((dy + 1) * step))
            x0, x1 = int(dx * step), max(int(dx * step) + 1, int((dx + 1) * step))
            total = (y1 - y0) * (x1 - x0)
            acc = [0, 0, 0, 0]
            for sy in range(y0, y1):
                base = (sy * src_size + x0) * 4
                span = src[base:base + (x1 - x0) * 4]
                for k, val in enumerate(span):
                    acc[k % 4] += val
            i = (dy * dst_size + dx) * 4
            out[i:i + 4] = bytes(min(255, c // total) for c in acc)
    return out


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def png(width: int, height: int, rgba: bytearray) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgba[y * width * 4:(y + 1) * width * 4])
                   for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


def ico(frames: list[tuple[int, bytes]]) -> bytes:
    """ICO из PNG-блоков; у 256×256 в заголовке стоит 0 (так требует формат)."""
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries = b""
    for size, blob in frames:
        side = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    return header + entries + b"".join(blob for _, blob in frames)


def build(path: Path | None = None) -> Path:
    target = path or appconfig.ICON_PATH
    base = draw(256)
    frames = [(size, png(size, size, downscale(base, 256, size))) for size in SIZES]
    target.write_bytes(ico(frames))
    return target


def main() -> None:
    target = build()
    sizes = ", ".join(str(s) for s in SIZES)
    print(f"ICO written: {target} bytes={target.stat().st_size} sizes={sizes}")


if __name__ == "__main__":
    main()
