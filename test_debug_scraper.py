import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

import debug_scraper


class DebugScraperTests(unittest.TestCase):
    def test_active_instance_is_probed_over_http(self):
        status_response = json.dumps({
            "active": True,
            "world": "world-slug",
            "background": "worlds/world-slug/background.webp",
        })
        join_html = """
            <html>
                <head><title>Human Readable World</title></head>
                <body>Current Players 3 / 10</body>
            </html>
        """

        output = io.StringIO()
        with patch(
            "debug_scraper.fetch_url",
            side_effect=[status_response, join_html],
        ) as fetch_url, redirect_stdout(output):
            result = debug_scraper.debug_scraper("https://foundry.example/join")

        self.assertEqual(result, 0)
        self.assertEqual(
            fetch_url.call_args_list,
            [
                call("https://foundry.example/api/status", timeout=10),
                call("https://foundry.example/join", timeout=10),
            ],
        )
        self.assertIn("State: active", output.getvalue())
        self.assertIn("World: Human Readable World", output.getvalue())
        self.assertIn("Players: 3 / 10", output.getvalue())
        self.assertIn(
            "Background: https://foundry.example/worlds/world-slug/background.webp",
            output.getvalue(),
        )

    def test_unreachable_status_endpoint_reports_offline(self):
        output = io.StringIO()
        with patch("debug_scraper.fetch_url", return_value=None) as fetch_url, redirect_stdout(output):
            result = debug_scraper.debug_scraper("https://foundry.example")

        self.assertEqual(result, 1)
        fetch_url.assert_called_once_with(
            "https://foundry.example/api/status",
            timeout=10,
        )
        self.assertIn("State: offline", output.getvalue())

    def test_utility_has_no_browser_automation_dependency(self):
        source = Path(debug_scraper.__file__).read_text(encoding="utf-8").lower()
        for browser_reference in ("selenium", "webdriver", "chromedriver"):
            with self.subTest(reference=browser_reference):
                self.assertNotIn(browser_reference, source)


if __name__ == "__main__":
    unittest.main()
