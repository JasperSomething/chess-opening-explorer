"""Extract the known Lumbra archive safely; publish the PGN only after success."""
import argparse
from pathlib import Path
import shutil
import tempfile
import py7zr

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('archive', type=Path)
p.add_argument('--output', type=Path, default=Path('data'))
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=True)
with py7zr.SevenZipFile(a.archive) as archive:
    entries = archive.list()
    if len(entries) != 1 or Path(entries[0].filename).name != entries[0].filename or not entries[0].filename.endswith('.pgn'):
        raise SystemExit('Unexpected archive layout; inspect before extracting.')
    size = entries[0].uncompressed
    if shutil.disk_usage(a.output).free < size + 1024**3:
        raise SystemExit('Insufficient space: need PGN size plus 1 GiB headroom.')
    target = a.output / entries[0].filename
    if target.exists():
        raise SystemExit('Output already exists; verify it rather than overwriting.')
    with tempfile.TemporaryDirectory(prefix='.extract-', dir=a.output) as staging:
        print(f'Extracting {size:,} bytes...', flush=True)
        archive.extractall(path=staging)
        extracted = Path(staging) / entries[0].filename
        if extracted.stat().st_size != size:
            raise SystemExit('Extracted size does not match archive metadata.')
        extracted.rename(target)
print(target.resolve())
