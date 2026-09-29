import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from services import publish


class SharingErrors(unittest.TestCase):
    def failure(self, status, body):
        error = HTTPError("https://pobb.in/pob/", status, "Rejected", {}, io.BytesIO(body))
        with patch("services.fetch", side_effect=error), self.assertRaises(RuntimeError) as caught:
            publish("code", lambda value: value, "fingerprint", lambda value: value)
        return str(caught.exception)

    def test_format_rejection_retains_reason_and_does_not_suggest_waiting(self):
        result = self.failure(400, b'{"message":"Invalid PoB: missing field $value"}')
        self.assertIn("HTTP 400", result)
        self.assertIn("missing field $value", result)
        self.assertNotIn("retry sharing later", result)

    def test_rate_limit_remains_retryable_and_html_is_not_displayed(self):
        result = self.failure(429, b"<html><body>Rate limited</body></html>")
        self.assertIn("retry sharing later", result)
        self.assertNotIn("<html>", result)


if __name__ == "__main__":
    unittest.main()
