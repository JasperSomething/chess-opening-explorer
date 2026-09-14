import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study import db as studydb  # noqa: E402


class SingletonLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'analysis.sqlite.lock'

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_acquire_in_process_fails(self):
        first = studydb.acquire_singleton(self.path)
        try:
            with self.assertRaises(RuntimeError):
                studydb.acquire_singleton(self.path)
        finally:
            first.close()

    def test_second_process_fails_while_first_holds(self):
        import fcntl
        holder = open(str(self.path), 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = subprocess.run(
                [sys.executable, '-c',
                 'import sys; sys.path.insert(0, %r);\n'
                 'from study import db;\n'
                 'try:\n'
                 '    db.acquire_singleton(%r); print("acquired")\n'
                 'except RuntimeError as e:\n'
                 '    print("refused")\n'
                 % (str(Path(__file__).resolve().parent.parent), str(self.path))],
                capture_output=True, text=True)
            self.assertIn('refused', result.stdout)
        finally:
            holder.close()

    def test_lock_is_released_when_holder_exits(self):
        first = studydb.acquire_singleton(self.path)
        first.close()
        second = studydb.acquire_singleton(self.path)   # must not raise
        second.close()


if __name__ == '__main__':
    unittest.main()
