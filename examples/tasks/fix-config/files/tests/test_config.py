import os
import tempfile
import unittest

from config import load_config


class ConfigTest(unittest.TestCase):
    def test_parse(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as f:
            f.write("# comment\n\n name = demo \nurl=http://x?a=b\n")
        try:
            self.assertEqual(load_config(f.name), {"name": "demo", "url": "http://x?a=b"})
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
