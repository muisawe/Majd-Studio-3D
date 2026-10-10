import tempfile
import unittest
from pathlib import Path

from majd_studio_3d.instance_lock import acquire_instance_lock


class InstanceLockTests(unittest.TestCase):
    def test_second_holder_is_refused_until_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "studio.lock"
            first = acquire_instance_lock(path)
            self.assertIsNotNone(first)
            self.assertIsNone(acquire_instance_lock(path))
            first.close()
            again = acquire_instance_lock(path)
            self.assertIsNotNone(again)
            again.close()


if __name__ == "__main__":
    unittest.main()
