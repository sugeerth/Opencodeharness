import unittest

from duration import parse_duration


class DurationTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("45s"), 45)
        self.assertEqual(parse_duration("1h2m3s"), 3723)

    def test_invalid(self):
        for bad in ["", "abc", "10", "1x", "h1"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_duration(bad)


if __name__ == "__main__":
    unittest.main()
