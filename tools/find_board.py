"""Locate a chessboard inside a screenshot and report its two square colours.

Scanning for the board by hand does not scale to several screenshots, so this finds it
mechanically: for a candidate square region it divides the region into 8x8 cells,
measures the average colour of the light-parity cells and of the dark-parity cells, and
scores the candidate on how well separated those two means are relative to how uneven
each group is internally. A real board wins on both counts at once -- two flat colour
groups far apart -- while UI chrome around it gives either similar means or noisy ones.

The search is coarse to fine: a wide first pass at 16px granularity (a board cannot be
smaller than a quarter of the window, so few candidates qualify), then a refinement pass
around the winner. Cell means come from summed-area tables, so a candidate costs 64
table lookups instead of a re-scan of its pixels.
"""
import json
import subprocess
import sys

WIDTH = 480
MIN_FRACTION = 0.20
MAX_FRACTION = 0.95


def decode(path, width=WIDTH):
    probe = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json',
                            '-show_streams', str(path)], capture_output=True, text=True)
    stream = json.loads(probe.stdout)['streams'][0]
    aspect = stream['height'] / stream['width']
    height = int(round(width * aspect / 2)) * 2
    raw = subprocess.run(['ffmpeg', '-v', 'quiet', '-i', str(path),
                          '-vf', f'scale={width}:{height}:flags=area',
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         capture_output=True, check=True).stdout
    return raw, width, height


def integral(raw, width, height, channel):
    table = [[0] * (width + 1) for _ in range(height + 1)]
    for y in range(height):
        row_sum = 0
        previous = table[y]
        current = table[y + 1]
        base = y * width * 3 + channel
        for x in range(width):
            row_sum += raw[base + x * 3]
            current[x + 1] = previous[x + 1] + row_sum
    return table


def region_sum(table, x0, y0, x1, y1):
    return table[y1][x1] - table[y0][x1] - table[y1][x0] + table[y0][x0]


def parity_means(tables, x, y, size, cells):
    """Average colour of the light-parity and dark-parity cells of the region."""
    light = [0.0, 0.0, 0.0]
    dark = [0.0, 0.0, 0.0]
    step = size / cells
    for row in range(cells):
        y0 = int(y + row * step) + 1
        y1 = max(y0 + 1, int(y + (row + 1) * step) - 1)
        for col in range(cells):
            x0 = int(x + col * step) + 1
            x1 = max(x0 + 1, int(x + (col + 1) * step) - 1)
            area = (x1 - x0) * (y1 - y0)
            bucket = light if (row + col) % 2 == 0 else dark
            for channel in range(3):
                bucket[channel] += region_sum(tables[channel], x0, y0, x1, y1) / area
    cells_count = (cells * cells) / 2
    return ([v / cells_count for v in light], [v / cells_count for v in dark])


def separation(a, b):
    return sum((a[c] - b[c]) ** 2 for c in range(3)) ** 0.5


def unevenness(tables, x, y, size, coarse_light, coarse_dark):
    """How much the halves disagree when the region is cut into twice as many cells."""
    fine_light, fine_dark = parity_means(tables, x, y, size, 16)
    return separation(fine_light, coarse_light) + separation(fine_dark, coarse_dark)


def scan(tables, width, height, low, high, step):
    best = None
    for size in range(low, high, step):
        for y in range(0, height - size, step):
            for x in range(0, width - size, step):
                light, dark = parity_means(tables, x, y, size, 8)
                distance = separation(light, dark)
                if distance < 24:
                    continue
                if best is None or distance > best[0]:
                    best = (distance, x, y, size, light, dark)
    return best


def find(path):
    raw, width, height = decode(path)
    tables = [integral(raw, width, height, channel) for channel in range(3)]
    low = int(width * MIN_FRACTION)
    high = int(min(width, height) * MAX_FRACTION)
    coarse = scan(tables, width, height, low, high, 16)
    if not coarse:
        return None, (width, height)
    _, x, y, size, _, _ = coarse
    best = None
    for candidate_size in range(max(low, size - 16), min(high, size + 17), 2):
        for candidate_y in range(max(0, y - 16), min(height - candidate_size, y + 17), 2):
            for candidate_x in range(max(0, x - 16), min(width - candidate_size, x + 17), 2):
                light, dark = parity_means(tables, candidate_x, candidate_y,
                                           candidate_size, 8)
                distance = separation(light, dark)
                if distance < 24:
                    continue
                flat = unevenness(tables, candidate_x, candidate_y, candidate_size,
                                  light, dark)
                value = distance / (1 + flat)
                if best is None or value > best[0]:
                    best = (value, candidate_x, candidate_y, candidate_size,
                            light, dark, distance, flat)
    return best, (width, height)


def main():
    for path in sys.argv[1:]:
        try:
            best, dims = find(path)
        except Exception as error:                       # noqa: BLE001
            print(f'{path}: could not analyse ({error})')
            continue
        if not best:
            print(f'{path}: no board found')
            continue
        value, x, y, size, light, dark, distance, flat = best
        light_hex = '#' + ''.join(f'{int(round(v)):02x}' for v in light)
        dark_hex = '#' + ''.join(f'{int(round(v)):02x}' for v in dark)
        print(f'{path}')
        print(f'   board at ({x},{y}) size {size} of {dims[0]}x{dims[1]}  score {value:.1f}')
        print(f'   light squares {light_hex}  rgb{tuple(int(round(v)) for v in light)}')
        print(f'   dark  squares {dark_hex}  rgb{tuple(int(round(v)) for v in dark)}')
        print(f'   separation {distance:.1f}  unevenness {flat:.1f}')


if __name__ == '__main__':
    main()
