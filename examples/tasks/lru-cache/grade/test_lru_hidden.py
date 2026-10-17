import time
import unittest

from lru import LRUCache


class HiddenLRUTest(unittest.TestCase):
    def test_operations_are_constant_time(self):
        c = LRUCache(20000)
        start = time.perf_counter()
        for i in range(40000):
            c.put(i, i)
            c.get(i // 2)
        self.assertLess(time.perf_counter() - start, 1.0, "40k operations should take well under a second")


if __name__ == "__main__":
    unittest.main()
