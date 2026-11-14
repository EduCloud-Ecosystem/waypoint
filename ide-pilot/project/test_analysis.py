import unittest
from analysis import mean_reading


class MeanReadingTests(unittest.TestCase):
    def test_missing_reading(self):
        self.assertEqual(mean_reading([10, None, 20]), 15)

    def test_complete_readings(self):
        self.assertEqual(mean_reading([10, 20]), 15)


if __name__ == "__main__":
    unittest.main()
