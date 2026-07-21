import inspect
import json
from pathlib import Path
import ssl
import unittest
from unittest.mock import patch

import app


class FakeResponse:
    def __init__(self, body, encoding="utf-8"):
        self.body = body.encode(encoding)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class FetchUrlTests(unittest.TestCase):
    def test_fetch_url_uses_timeout_user_agent_and_unverified_tls_context(self):
        with patch("app.urllib.request.urlopen", return_value=FakeResponse("response")) as urlopen:
            result = app.fetch_url("https://foundry.example/api/status", timeout=7)

        self.assertEqual(result, "response")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://foundry.example/api/status")
        self.assertEqual(request.get_header("User-agent"), "FoundryPortal/1.0")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 7)
        context = urlopen.call_args.kwargs["context"]
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)

    def test_fetch_url_returns_none_when_request_fails(self):
        with patch("app.urllib.request.urlopen", side_effect=OSError("unreachable")):
            self.assertIsNone(app.fetch_url("https://foundry.example/api/status"))


class CheckInstanceStatusTests(unittest.TestCase):
    def test_unreachable_instance_is_offline_after_legacy_fallbacks_fail(self):
        with patch("app.fetch_url", return_value=None) as fetch_url:
            result = app.check_instance_status("https://foundry.example/")

        self.assertEqual(result, ("offline", None, None))
        self.assertEqual(
            fetch_url.call_args_list,
            [
                unittest.mock.call("https://foundry.example/api/status"),
                unittest.mock.call("https://foundry.example/join"),
                unittest.mock.call("https://foundry.example/auth"),
            ],
        )

    def test_missing_status_endpoint_falls_back_to_reachable_join_route(self):
        with patch(
            "app.fetch_url",
            side_effect=[None, "<html><body>Legacy Foundry join page</body></html>"],
        ) as fetch_url:
            result = app.check_instance_status("https://foundry.example/")

        self.assertEqual(result, ("online", None, None))
        self.assertEqual(
            fetch_url.call_args_list,
            [
                unittest.mock.call("https://foundry.example/api/status"),
                unittest.mock.call("https://foundry.example/join"),
            ],
        )

    def test_failed_status_endpoint_falls_back_to_reachable_auth_route(self):
        with patch(
            "app.fetch_url",
            side_effect=[None, None, "<html><body>Legacy Foundry auth page</body></html>"],
        ) as fetch_url:
            result = app.check_instance_status("https://foundry.example")

        self.assertEqual(result, ("online", None, None))
        self.assertEqual(
            fetch_url.call_args_list,
            [
                unittest.mock.call("https://foundry.example/api/status"),
                unittest.mock.call("https://foundry.example/join"),
                unittest.mock.call("https://foundry.example/auth"),
            ],
        )

    def test_non_json_status_endpoint_uses_legacy_route_fallback(self):
        with patch(
            "app.fetch_url",
            side_effect=["not json", "<html><body>Legacy Foundry join page</body></html>"],
        ) as fetch_url:
            result = app.check_instance_status("https://foundry.example")

        self.assertEqual(result, ("online", None, None))
        self.assertEqual(
            fetch_url.call_args_list,
            [
                unittest.mock.call("https://foundry.example/api/status"),
                unittest.mock.call("https://foundry.example/join"),
            ],
        )

    def test_inactive_instance_is_online_and_resolves_relative_background(self):
        response = json.dumps({
            "active": False,
            "background": "/ui/backgrounds/setup.webp",
        })
        with patch("app.fetch_url", return_value=response):
            result = app.check_instance_status("https://foundry.example/foundry/")

        self.assertEqual(
            result,
            (
                "online",
                None,
                "https://foundry.example/foundry/ui/backgrounds/setup.webp",
            ),
        )

    def test_absolute_background_url_is_preserved(self):
        background = "https://cdn.example/world.webp"
        response = json.dumps({"active": False, "background": background})
        with patch("app.fetch_url", return_value=response):
            result = app.check_instance_status("https://foundry.example")

        self.assertEqual(result, ("online", None, background))

    def test_active_v13_instance_uses_join_title_and_player_counts(self):
        status_response = json.dumps({
            "active": True,
            "world": "world-slug",
            "background": "worlds/world-slug/background.webp",
        })
        responses = [
            status_response,
            """
            <html>
              <head><title>Human Readable World</title></head>
              <body><span>Current Players</span><strong>3</strong> / <strong>10</strong></body>
            </html>
            """,
        ]
        with patch("app.fetch_url", side_effect=responses) as fetch_url:
            result = app.check_instance_status("https://foundry.example/")

        expected_background = "https://foundry.example/worlds/world-slug/background.webp"
        self.assertEqual(
            result,
            (
                "active",
                {
                    "name": "Human Readable World",
                    "background": expected_background,
                    "players": "3 / 10",
                },
                expected_background,
            ),
        )
        self.assertEqual(
            fetch_url.call_args_list,
            [
                unittest.mock.call("https://foundry.example/api/status"),
                unittest.mock.call("https://foundry.example/join"),
            ],
        )

    def test_active_instance_does_not_treat_status_friends_as_player_count(self):
        status_response = json.dumps({
            "active": True,
            "world": "world-slug",
            "friends": 0,
        })
        join_html = "<html><body>Current Players 4 / 12</body></html>"
        with patch("app.fetch_url", side_effect=[status_response, join_html]):
            result = app.check_instance_status("https://foundry.example")

        self.assertEqual(result[1]["players"], "4 / 12")

    def test_active_instance_reports_unavailable_players_when_join_page_has_no_count(self):
        status_response = json.dumps({
            "active": True,
            "world": "world-slug",
        })
        with patch("app.fetch_url", side_effect=[status_response, None]):
            result = app.check_instance_status("https://foundry.example")

        self.assertEqual(
            result,
            (
                "active",
                {
                    "name": "world-slug",
                    "background": "/static/images/background.jpg",
                    "players": "Unknown / Unknown",
                },
                None,
            ),
        )

    def test_status_check_has_no_browser_driver_code(self):
        source = inspect.getsource(app.check_instance_status).lower()
        for browser_reference in ("selenium", "webdriver", "chrome", "driver"):
            self.assertNotIn(browser_reference, source)


class RuntimeDependencyTests(unittest.TestCase):
    def test_application_runtime_does_not_install_browser_automation(self):
        repository_root = Path(__file__).parent
        runtime_files = [
            repository_root / "requirements.txt",
            repository_root / "Dockerfile",
        ]
        browser_references = ("selenium", "google-chrome", "chromedriver")

        for runtime_file in runtime_files:
            content = runtime_file.read_text(encoding="utf-8").lower()
            for browser_reference in browser_references:
                with self.subTest(file=runtime_file.name, reference=browser_reference):
                    self.assertNotIn(browser_reference, content)


class SchedulerTests(unittest.TestCase):
    def test_status_polling_interval_is_thirty_seconds(self):
        jobs = [job for job in app.scheduler.get_jobs() if job.func is app.update_instance_statuses]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].trigger.interval.total_seconds(), 30)


if __name__ == "__main__":
    unittest.main()
