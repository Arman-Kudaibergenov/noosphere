"""qr.py — QR-код без зависимостей (byte mode, уровень коррекции M, версии 1–10).

Зачем свой: мобильный доступ показывает QR прямо в карточке профиля, а ставить
пакет `qrcode`/`segno` в боевой venv нельзя (requirements.txt фиксирован, установка
пакетов на сервисе запрещена). Поэтому здесь минимальный, но полный энкодер:
GF(256)/Рида–Соломон, раскладка матрицы, маски со штрафами, формат- и
версионная информация. Рендер — SVG (масштабируется без потерь, вставляется
в HTML как текст, без сети и без картинок-файлов).

Контракт:
    matrix(text) -> list[list[bool]]      # dark=True
    svg(text, ...) -> str                 # самостоятельный <svg>…</svg>
    fits(text) -> bool                    # влезает ли в поддерживаемые версии

Проверки (scripts/smoke.py): длина строки матрицы == 21 + 4*(ver-1), формат-биты
и версия-биты попадают в эталонные значения, QR на команду туннеля рисуется.
"""
from __future__ import annotations

import html as _html
from typing import Iterable

# уровень M: сколько ECC-кодовых слов на блок и сколько блоков (версии 1–10).
# Значения сверены с ёмкостью byte-режима стандарта (v1-M = 14 байт, v10-M = 213).
_ECC_PER_BLOCK = {1: 10, 2: 16, 3: 26, 4: 18, 5: 24, 6: 16, 7: 18, 8: 22, 9: 22, 10: 26}
_NUM_BLOCKS = {1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 4, 7: 4, 8: 4, 9: 5, 10: 5}
# координаты центров выравнивающих узоров
_ALIGN = {1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30),
          6: (6, 34), 7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50)}

_MAX_VERSION = 10
# формат-биты уровня M (L=1, M=0, Q=3, H=2) — цикл BCH(15,5), генератор 0x537
_ECL_BITS = 0
_MASK_FORMAT_XOR = 0x5412


# ─────────────────────────────────────────────────────────── Reed–Solomon GF(256)
def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree: int) -> list[int]:
    """Порождающий многочлен степени degree (старший коэффициент отброшен)."""
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: Iterable[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, d in enumerate(divisor):
            result[i] ^= _gf_mul(d, factor)
    return result


# ─────────────────────────────────────────────────────────── ёмкость и битстрим
def _num_raw_data_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        num_align = ver // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if ver >= 7:
            result -= 36
    return result


def _num_data_codewords(ver: int) -> int:
    return _num_raw_data_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]


def _count_bits(ver: int) -> int:
    """Ширина поля длины в byte-режиме: 8 бит до v9, 16 бит с v10."""
    return 8 if ver <= 9 else 16


def fits(text: str) -> bool:
    payload = (text or "").encode("utf-8")
    return len(payload) <= _num_data_codewords(_MAX_VERSION) - _count_bits(_MAX_VERSION) // 8


def _pick_version(nbytes: int) -> int:
    for ver in range(1, _MAX_VERSION + 1):
        if 4 + _count_bits(ver) + 8 * nbytes <= _num_data_codewords(ver) * 8:
            return ver
    raise ValueError(f"QR: данные не влезают в версии 1–{_MAX_VERSION} ({nbytes} байт)")


def _data_codewords(payload: bytes, ver: int) -> list[int]:
    capacity = _num_data_codewords(ver) * 8
    bits: list[int] = []

    def put(val: int, width: int) -> None:
        for i in range(width - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)                     # byte mode
    put(len(payload), _count_bits(ver))
    for b in payload:
        put(b, 8)
    bits += [0] * min(4, capacity - len(bits))          # терминатор
    bits += [0] * ((8 - len(bits) % 8) % 8)             # до границы байта
    pads = (0xEC, 0x11)
    i = 0
    while len(bits) < capacity:
        put(pads[i % 2], 8)
        i += 1
    return [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]


def _interleaved_codewords(payload: bytes, ver: int) -> list[int]:
    data = _data_codewords(payload, ver)
    num_blocks = _NUM_BLOCKS[ver]
    ecc_len = _ECC_PER_BLOCK[ver]
    raw = _num_raw_data_modules(ver) // 8
    num_short = num_blocks - raw % num_blocks
    short_len = raw // num_blocks
    divisor = _rs_divisor(ecc_len)

    datas: list[list[int]] = []
    eccs: list[list[int]] = []
    k = 0
    for i in range(num_blocks):
        dlen = short_len - ecc_len + (0 if i < num_short else 1)
        block = data[k:k + dlen]
        k += dlen
        datas.append(block)
        eccs.append(_rs_remainder(block, divisor))

    out: list[int] = []
    for i in range(short_len - ecc_len + 1):
        for block in datas:
            if i < len(block):
                out.append(block[i])
    for i in range(ecc_len):
        for block in eccs:
            out.append(block[i])
    return out


# ─────────────────────────────────────────────────────────── матрица
def _format_bits(mask: int) -> int:
    data = (_ECL_BITS << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | (rem & 0x3FF)) ^ _MASK_FORMAT_XOR


def _version_bits(ver: int) -> int:
    rem = ver
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (ver << 12) | (rem & 0xFFF)


class _Grid:
    def __init__(self, size: int) -> None:
        self.size = size
        self.mod = [[False] * size for _ in range(size)]
        self.fn = [[False] * size for _ in range(size)]

    def set_fn(self, x: int, y: int, dark: bool) -> None:
        if 0 <= x < self.size and 0 <= y < self.size:
            self.mod[y][x] = dark
            self.fn[y][x] = True


def _draw_function_patterns(g: _Grid, ver: int) -> None:
    size = g.size
    for i in range(size):                      # тайминги
        g.set_fn(6, i, i % 2 == 0)
        g.set_fn(i, 6, i % 2 == 0)
    for cx, cy in ((0, 0), (size - 7, 0), (0, size - 7)):   # поисковые + сепараторы
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                x, y = cx + dx, cy + dy
                if not (0 <= x < size and 0 <= y < size):
                    continue
                inner = 0 <= dx <= 6 and 0 <= dy <= 6
                dark = inner and (dx in (0, 6) or dy in (0, 6)
                                  or (2 <= dx <= 4 and 2 <= dy <= 4))
                g.set_fn(x, y, dark)
    pos = _ALIGN[ver]
    for ax in pos:                              # выравнивающие узоры
        for ay in pos:
            if (ax == 6 and ay == 6) or (ax == 6 and ay == size - 7) \
                    or (ax == size - 7 and ay == 6):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    g.set_fn(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    _draw_format(g, 0)                          # место под формат-биты резервируем сразу
    if ver >= 7:                                # версия (18 бит, два блока)
        bits = _version_bits(ver)
        for i in range(18):
            dark = ((bits >> i) & 1) != 0
            a, b = size - 11 + i % 3, i // 3
            g.set_fn(a, b, dark)
            g.set_fn(b, a, dark)


def _draw_format(g: _Grid, mask: int) -> None:
    size = g.size
    bits = _format_bits(mask)

    def bit(i: int) -> bool:
        return ((bits >> i) & 1) != 0

    for i in range(6):                          # первая копия вокруг левого верхнего
        g.set_fn(8, i, bit(i))
    g.set_fn(8, 7, bit(6))
    g.set_fn(8, 8, bit(7))
    g.set_fn(7, 8, bit(8))
    for i in range(9, 15):
        g.set_fn(14 - i, 8, bit(i))
    for i in range(8):                          # вторая копия: низ слева + верх справа
        g.set_fn(size - 1 - i, 8, bit(i))
    for i in range(8, 15):
        g.set_fn(8, size - 15 + i, bit(i))
    g.set_fn(8, size - 8, True)                 # тёмный модуль всегда тёмный


def _draw_codewords(g: _Grid, data: list[int]) -> None:
    size = g.size
    bits: list[int] = []
    for b in data:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)
    i = 0
    # пары столбцов справа налево; столбец тайминга (6) пара не занимает,
    # поэтому при start<=6 пара сдвигается на один столбец влево
    for start in range(size - 1, 0, -2):
        right = start - 1 if start <= 6 else start
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = (size - 1 - vert) if upward else vert
                if not (0 <= x < size) or g.fn[y][x]:
                    continue
                if i < len(bits):
                    g.mod[y][x] = bits[i] == 1
                    i += 1


_MASK_FN = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def _apply_mask(g: _Grid, mask: int) -> _Grid:
    out = _Grid(g.size)
    out.fn = [row[:] for row in g.fn]
    fn = _MASK_FN[mask]
    for y in range(g.size):
        for x in range(g.size):
            dark = g.mod[y][x]
            if not g.fn[y][x] and fn(y, x):
                dark = not dark
            out.mod[y][x] = dark
    return out


def _penalty(mod: list[list[bool]]) -> int:
    size = len(mod)
    score = 0
    # правило 1: серии одинаковых модулей длиной ≥5
    for line in list(mod) + [list(col) for col in zip(*mod)]:
        run, prev = 1, line[0]
        for v in line[1:]:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run, prev = 1, v
        if run >= 5:
            score += 3 + run - 5
    # правило 2: блоки 2×2 одного цвета
    for y in range(size - 1):
        row0, row1 = mod[y], mod[y + 1]
        for x in range(size - 1):
            if row0[x] == row0[x + 1] == row1[x] == row1[x + 1]:
                score += 3
    # правило 3: «поисковый» узор 1:1:3:1:1 с прозрачной полосой
    pat1 = [True, False, True, True, True, False, True, False, False, False, False]
    pat2 = pat1[::-1]
    for line in list(mod) + [list(col) for col in zip(*mod)]:
        for i in range(size - 10):
            win = list(line[i:i + 11])
            if win == pat1 or win == pat2:
                score += 40
    # правило 4: перекос тёмного/светлого
    dark = sum(sum(1 for v in row if v) for row in mod)
    total = size * size
    score += abs(dark * 20 - total * 10) // total * 10
    return score


def matrix(text: str) -> list[list[bool]]:
    """Матрица модулей QR для строки (UTF-8, byte mode, уровень M)."""
    payload = (text or "").encode("utf-8")
    if not payload:
        raise ValueError("QR: пустая строка")
    ver = _pick_version(len(payload))
    size = ver * 4 + 17
    g = _Grid(size)
    _draw_function_patterns(g, ver)
    _draw_codewords(g, _interleaved_codewords(payload, ver))
    best: tuple[int, list[list[bool]]] | None = None
    for mask in range(8):
        cand = _apply_mask(g, mask)
        _draw_format(cand, mask)
        score = _penalty(cand.mod)
        if best is None or score < best[0]:
            best = (score, cand.mod)
    assert best is not None
    return best[1]


def svg(text: str, *, scale: int = 6, border: int = 4,
        dark: str = "#0b2b24", light: str = "#ffffff",
        label: str = "") -> str:
    """QR как самостоятельный SVG: светлый фон, тёмные модули, «тихая зона»."""
    mod = matrix(text)
    n = len(mod)
    scale = max(1, int(scale))
    border = max(0, int(border))
    dim = (n + 2 * border) * scale
    parts: list[str] = []
    for y, row in enumerate(mod):
        x = 0
        while x < n:
            if not row[x]:
                x += 1
                continue
            run = 1
            while x + run < n and row[x + run]:
                run += 1
            parts.append(
                f'<rect x="{(x + border) * scale}" y="{(y + border) * scale}" '
                f'width="{run * scale}" height="{scale}"/>')
            x += run
    aria = _html.escape(label or "QR-код")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{aria}" '
        f'width="{dim}" height="{dim}" viewBox="0 0 {dim} {dim}" '
        f'shape-rendering="crispEdges">'
        f'<rect width="{dim}" height="{dim}" fill="{light}"/>'
        f'<g fill="{dark}">' + "".join(parts) + "</g></svg>"
    )


def svg_data_uri(text: str, **kw) -> str:
    """Тот же SVG как data:URI — для <img src> без лишних запросов."""
    from urllib.parse import quote
    return "data:image/svg+xml;utf8," + quote(svg(text, **kw))
