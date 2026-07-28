import unittest

from ledger import amounts, mean, median, parse, spread, total

SAMPLE = """
# date        description   amount
2024-01-02    coffee        4
2024-01-03    books         10
2024-01-05    train         2
2024-01-09    lunch         8
"""


class TestParse(unittest.TestCase):
    def test_parse_skips_comments_and_blanks(self):
        self.assertEqual(len(parse(SAMPLE)), 4)

    def test_parse_line_fields(self):
        entry = parse(SAMPLE)[0]
        self.assertEqual(entry["date"], "2024-01-02")
        self.assertEqual(entry["description"], "coffee")
        self.assertEqual(entry["amount"], 4.0)


class TestStats(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total(amounts(parse(SAMPLE))), 24.0)

    def test_mean(self):
        self.assertEqual(mean(amounts(parse(SAMPLE))), 6.0)

    def test_spread(self):
        self.assertEqual(spread(amounts(parse(SAMPLE))), 8.0)

    def test_median_odd_count(self):
        self.assertEqual(median([5, 1, 3]), 3)

    def test_median_even_count(self):
        self.assertEqual(median([1, 2, 3, 4]), 2.5)

    def test_median_of_sample(self):
        # amounts are 4, 10, 2, 8 -> sorted 2, 4, 8, 10 -> (4 + 8) / 2
        self.assertEqual(median(amounts(parse(SAMPLE))), 6.0)

    def test_median_rejects_empty(self):
        with self.assertRaises(ValueError):
            median([])


if __name__ == "__main__":
    unittest.main()
