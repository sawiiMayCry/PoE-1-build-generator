import unittest
from pathlib import Path
from unittest.mock import patch

from pob_engine import export_with_pob


class ExportRecovery(unittest.TestCase):
    def test_export_retries_after_po_b_saver_registry_teardown(self):
        class Worker:
            def __init__(self):
                self.calls = []

            def request(self, operation, **payload):
                self.calls.append(operation)
                if operation == "export" and self.calls.count("export") == 1:
                    raise RuntimeError("Build.lua:2044: bad argument #1 to 'pairs' (table expected, got nil)")
                return ({"xml": "saved", "stats": {"Life": 4000}}
                        if operation == "export" else {"stats": {"Life": 4000}})

        worker = Worker()
        with patch("pob_engine.get_worker", return_value=worker):
            result = export_with_pob("candidate", Path("."), Path("."))
        self.assertEqual(worker.calls, ["export", "export", "calculate"])
        self.assertEqual(worker.export_recoveries, 1)
        self.assertEqual(result, {"xml": "saved", "stats": {"Life": 4000}})

    def test_export_does_not_retry_unrelated_po_b_errors(self):
        class Worker:
            calls = 0

            def request(self, operation, **payload):
                self.calls += 1
                raise RuntimeError("Path of Building could not load candidate")

        worker = Worker()
        with patch("pob_engine.get_worker", return_value=worker):
            with self.assertRaisesRegex(RuntimeError, "could not load candidate"):
                export_with_pob("candidate", Path("."), Path("."))
        self.assertEqual(worker.calls, 1)

    def test_export_normalizes_po_b_default_config_before_strict_roundtrip(self):
        class Worker:
            def __init__(self):
                self.exports = 0
                self.calculations = 0

            def request(self, operation, **payload):
                if operation == "export":
                    self.exports += 1
                    if self.exports == 1:
                        return {"xml": "normalized-input", "stats": {"TotalEHP": 100}}
                    return {"xml": "stable-export", "stats": {"TotalEHP": 120}}
                self.calculations += 1
                return {"stats": {"TotalEHP": 99 if self.calculations == 1 else 120}}

        worker = Worker()
        with patch("pob_engine.get_worker", return_value=worker):
            result = export_with_pob("candidate", Path("."), Path("."))
        self.assertEqual(result, {"xml": "stable-export", "stats": {"TotalEHP": 120}})
        self.assertEqual((worker.exports, worker.calculations), (2, 2))


if __name__ == "__main__":
    unittest.main()
