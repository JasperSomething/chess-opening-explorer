"""Retone the wood4 board to the requested square colours, keeping the grain.

"not the entire square but it should look like that": the squares must *read* as
#e1b875 and #592904 without becoming flat fills. So each class of square (light/dark)
is mean-matched to its target: the average colour of every light square becomes
#e1b875, the average of every dark square becomes #592904, and the per-pixel deviation
from that mean (the wood grain) is carried over unchanged.

Implementation: decode the texture to raw RGB once, build a 256-entry lookup table per
channel per class, and map every pixel through it. LUTs keep the pass over a megapixel
cheap in pure Python.
"""
import subprocess
import sys
from pathlib import Path

SIZE = 1024
CELL = SIZE // 8
LIGHT_TARGET = (0xE1, 0xB8, 0x75)
DARK_TARGET = (0x59, 0x29, 0x04)
GRAIN = 1.0                       # 1.0 keeps the grain strength, <1 softens it


def decode(path):
    out = subprocess.run(['ffmpeg', '-v', 'quiet', '-i', str(path), '-f', 'rawvideo',
                          '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True)
    return bytearray(out.stdout)


def class_means(buf):
    light = [0, 0, 0]
    dark = [0, 0, 0]
    n_light = n_dark = 0
    for row in range(SIZE):
        for col in range(SIZE):
            index = (row * SIZE + col) * 3
            is_light = ((row // CELL) + (col // CELL)) % 2 == 0
            pixel = (buf[index], buf[index + 1], buf[index + 2])
            bucket = light if is_light else dark
            for channel in range(3):
                bucket[channel] += pixel[channel]
            if is_light:
                n_light += 1
            else:
                n_dark += 1
    return ([v / n_light for v in light], [v / n_dark for v in dark])


def lookup(source_mean, target):
    table = []
    for value in range(256):
        mapped = target + (value - source_mean) * GRAIN
        table.append(max(0, min(255, int(round(mapped)))))
    return table


def retone(buf, light_mean, dark_mean):
    tables = {}
    for name, mean, target in (('light', light_mean, LIGHT_TARGET),
                               ('dark', dark_mean, DARK_TARGET)):
        tables[name] = [lookup(mean[channel], target[channel]) for channel in range(3)]
    for row in range(SIZE):
        row_is_light = (row // CELL) % 2 == 0
        for col in range(SIZE):
            index = (row * SIZE + col) * 3
            tableset = tables['light'] if ((col // CELL) % 2 == 0) == row_is_light \
                else tables['dark']
            buf[index] = tableset[0][buf[index]]
            buf[index + 1] = tableset[1][buf[index + 1]]
            buf[index + 2] = tableset[2][buf[index + 2]]
    return buf


def encode(buf, path):
    subprocess.run(['ffmpeg', '-v', 'quiet', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                    '-s', f'{SIZE}x{SIZE}', '-i', '-', '-q:v', '3', str(path)],
                   input=bytes(buf), check=True)


def main():
    source = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    buf = decode(source)
    light_mean, dark_mean = class_means(buf)
    print(f'source {source.name}: light mean '
          f'({light_mean[0]:.0f},{light_mean[1]:.0f},{light_mean[2]:.0f}) dark mean '
          f'({dark_mean[0]:.0f},{dark_mean[1]:.0f},{dark_mean[2]:.0f})')
    buf = retone(buf, light_mean, dark_mean)
    encode(buf, destination)
    check = decode(destination)
    light_after, dark_after = class_means(check)
    print(f'output {destination.name}: light mean '
          f'({light_after[0]:.0f},{light_after[1]:.0f},{light_after[2]:.0f}) dark mean '
          f'({dark_after[0]:.0f},{dark_after[1]:.0f},{dark_after[2]:.0f})')
    print(f'targets: light {LIGHT_TARGET} dark {DARK_TARGET}')


if __name__ == '__main__':
    main()
