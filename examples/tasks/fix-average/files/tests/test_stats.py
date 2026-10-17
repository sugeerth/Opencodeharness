import unittest

from stats import average, median


class StatsTest(unittest.TestCase):
    def test_average(self):
        self.assertEqual(average([1, 2]), 1.5)

    def test_average_empty(self):
        self.assertIsNone(average([]))

    def test_median_even(self):
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_median_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)


if __name__ == "__main__":
    unittest.main()
