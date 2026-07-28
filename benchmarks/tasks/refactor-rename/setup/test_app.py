import unittest

from app import api, events, report, store


class TestStore(unittest.TestCase):
    def test_fetch(self):
        self.assertEqual(len(store.fetch_records("alpha")), 2)

    def test_limit(self):
        self.assertEqual(len(store.fetch_records("alpha", limit=1)), 1)

    def test_unknown_table(self):
        with self.assertRaises(store.StoreError):
            store.fetch_records("nope")


class TestApi(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(api.handle({"table": "beta"})["count"], 1)

    def test_missing(self):
        self.assertEqual(api.handle({"table": "nope"})["status"], 404)


class TestReport(unittest.TestCase):
    def test_totals(self):
        self.assertEqual(report.totals("alpha"), 30)

    def test_summary(self):
        self.assertEqual(report.summary(), {"alpha": 30, "beta": 30, "gamma": 0})


class TestEvents(unittest.TestCase):
    def test_wire_name_is_frozen(self):
        # Other services subscribe to this literal. Renaming Python symbols
        # must not change it.
        self.assertEqual(store.EVENT_NAME, "fetch_records")

    def test_dispatch(self):
        rows = events.dispatch("fetch_records", {"table": "beta"})
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
