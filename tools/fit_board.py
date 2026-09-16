"""Fit an 8x8 chessboard grid to a crop and report the two square colours.

Rather than hunting for the board's outline, this assumes the crop is mostly board and
solves for the grid itself: it tries candidate square sizes and grid offsets, measures
the average colour of the two square parities under each hypothesis, and keeps the fit
that separates the two colours most sharply while leaving each parity internally flat.

A wrong alignment mixes light squares into the dark group and vice versa, which collapses
the separation -- so the correct grid is the one that stands out, no board outline needed.
"""
import json
import subprocess
import sys

WIDTH = 600


def decode(path, crop=None, width=WIDTH):
    filters = []
    if crop:
        filters.append(f'crop={crop}')
    filters.append(f'scale={width}:-2:flags=area')
    raw = subprocess.run(['ffmpeg', '-v', 'quiet', '-i', str(path),
                          '-vf', ','.join(filters),
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         capture_output=True, check=True).stdout
    return raw, width


def shapes(raw, width):
    height = len(raw) // (width * 3)
    tables = []
    for channel in range(3):
        table = [[0] * (width + 1) for _ in range(height + 1)]
        for y in range(height):
            row_sum = 0
            previous = table[y]
            current = table[y + 1]
            base = y * width * 3 + channel
            for x in range(width):
                row_sum += raw[base + x * 3]
                current[x + 1] = previous[x + 1] + row_sum
        tables.append(table)
    return tables, width, height


def region(table, x0, y0, x1, y1):
    return table[y1][x1] - table[y0][x1] - table[y1][x0] + table[y0][x0]


def fit(tables, width, height, cells, x, y, cell):
    """Average colour of each parity, using the centre of each cell only."""
    light = [0.0, 0.0, 0.0]
    dark = [0.0, 0.0, 0.0]
    inset = max(2, cell // 5)
    for row in range(cells):
        for col in range(cells):
            x0 = int(x + col * cell) + inset
            x1 = int(x + (col + 1) * cell) - inset
            y0 = int(y + row * cell) + inset
            y1 = int(y + (row + 1) * cell) - inset
            if x1 <= x0 or y1 <= y0 or x1 >= width or y1 >= height:
                return None
            area = (x1 - x0) * (y1 - y0)
            bucket = light if (row + col) % 2 == 0 else dark
            for channel in range(3):
                bucket[channel] += region(tables[channel], x0, y0, x1, y1) / area
    half = (cells * cells) / 2
    return ([v / half for v in light], [v / half for v in dark])


def distance(a, b):
    return sum((a[c] - b[c]) ** 2 for c in range(3)) ** 0.5


def search(tables, width, height, cells=8, coarse=True):
    best = None
    # a board worth matching fills a good part of the crop, so square sizes live in a
    # narrow band; that plus a 2x2 pre-screen keeps the search from exploding
    cell_low = max(8, int(min(width, height) / (cells * 2.2)))
    cell_high = min(width, height) // cells
    step = 2 if coarse else 1
    offset_step = 4 if coarse else 1
    for cell in range(cell_low, cell_high, step):
        span = cell * cells
        for y in range(0, height - span, offset_step):
            for x in range(0, width - span, offset_step):
                screen = fit(tables, width, height, 2, x, y, cell)
                if not screen:
                    continue
                if distance(*screen) < 40:
                    continue
                result = fit(tables, width, height, cells, x, y, cell)
                if not result:
                    continue
                light, dark = result
                gap = distance(light, dark)
                if gap < 40:
                    continue
                if best is None or gap > best[0]:
                    best = (gap, x, y, cell, light, dark)
    return best


def main():
    for spec in sys.argv[1:]:
        if ':' in spec:
            path, crop = spec.split(':', 1)
        else:
            path, crop = spec, None
        try:
            raw, width = decode(path, crop)
            tables, width, height = shapes(raw, width)
            coarse = search(tables, width, height)
            if not coarse:
                print(f'{path} {crop or ""}: no board fit')
                continue
            _, x, y, cell, _, _ = coarse
            best = None
            for candidate in range(max(6, cell - 3), cell + 4):
                for offset_y in range(max(0, y - 3), y + 4):
                    for offset_x in range(max(0, x - 3), x + 4):
                        result = fit(tables, width, height, 8, offset_x, offset_y, candidate)
                        if not result:
                            continue
                        light, dark = result
                        gap = distance(light, dark)
                        if best is None or gap > best[0]:
                            best = (gap, offset_x, offset_y, candidate, light, dark)
            if not best:
                best = coarse
            gap, x, y, cell, light, dark = best
            light_hex = '#' + ''.join(f'{int(round(v)):02x}' for v in light)
            dark_hex = '#' + ''.join(f'{int(round(v)):02x}' for v in dark)
            print(f'{path}  crop={crop}  ({width}x{height})')
            print(f'   grid at ({x},{y}) cell {cell}px  separation {gap:.0f}')
            print(f'   light {light_hex} rgb{tuple(int(round(v)) for v in light)}')
            print(f'   dark  {dark_hex} rgb{tuple(int(round(v)) for v in dark)}')
        except Exception as error:                        # noqa: BLE001
            print(f'{path}: failed ({error})')


if __name__ == '__main__':
    main()
