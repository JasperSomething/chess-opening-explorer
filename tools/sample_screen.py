"""Report the dominant colours in a screenshot crop.

Measures what is actually painted on the desktop, independently of what the page's own
DOM reports. Decodes the image downscaled, buckets colours to a coarse grid so that
near-identical wood grain pixels group together, and prints each bucket's colour with
the share of the image it covers.
"""
import subprocess
import sys
from collections import Counter

BUCKET = 8          # colour resolution for grouping near-identical pixels
TOP = 8             # how many clusters to report


def pixels(path, width=240):
    raw = subprocess.run(['ffmpeg', '-v', 'quiet', '-i', str(path),
                          '-vf', f'scale={width}:-2:flags=area',
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         capture_output=True, check=True).stdout
    return [raw[i:i + 3] for i in range(0, len(raw) - 2, 3)]


def clusters(path):
    counts = Counter()
    for pixel in pixels(path):
        key = tuple((value // BUCKET) * BUCKET + BUCKET // 2 for value in pixel)
        counts[key] += 1
    total = sum(counts.values()) or 1
    return [(colour, count / total) for colour, count in counts.most_common(TOP)]


def main():
    for path in sys.argv[1:]:
        print(f'== {path}')
        try:
            found = clusters(path)
        except subprocess.CalledProcessError as error:
            print('  could not analyse:', error)
            continue
        for colour, share in found:
            hex_value = '#' + ''.join(f'{value:02x}' for value in colour)
            print(f'  {hex_value}  rgb{colour!s:<18} {share * 100:5.1f}%')


if __name__ == '__main__':
    main()
