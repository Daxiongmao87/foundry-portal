import json
import unittest
from unittest import mock
from urllib.error import URLError

import app


class CheckInstanceStatusTests(unittest.TestCase):
    def test_active_instance_uses_status_api_and_join_title(self):
        status_response = json.dumps({
            "active": True,
            "world": "the-world-slug",
            "background": "worlds/the-world/background.webp",
            "friends": 3,
        })
        join_response = "<html><head><title>The World</title></head></html>"

        with mock.patch.object(app, "fetch_url", side_effect=[status_response, join_response]) as fetch_url:
            result = app.check_instance_status("https://foundry.example/")

        self.assertEqual(
            result,
            (
                "active",
                {
                    "name": "The World",
                    "background": "https://foundry.example/worlds/the-world/background.webp",
                    "players": "3 connected",
                },
                "https://foundry.example/worlds/the-world/background.webp",
            ),
        )
        self.assertEqual(
            fetch_url.call_args_list,
            [
                mock.call("https://foundry.example/api/status"),
                mock.call("https://foundry.example/join"),
            ],
        )

    def test_active_instance_falls_back_when_optional_data_is_missing(self):
        status_response = json.dumps({
            "active": True,
            "world": "the-world-slug",
        })

        with mock.patch.object(app, "fetch_url", side_effect=[status_response, None]):
            result = app.check_instance_status("https://foundry.example")

        self.assertEqual(
            result,
            (
                "active",
                {
                    "name": "the-world-slug",
                    "background": "/static/images/background.jpg",
                    "players": "Unknown / Unknown",
                },
                None,
            ),
        )

    def test_active_instance_preserves_explicit_zero_player_count(self):
        status_response = json.dumps({
            "active": True,
            "world": "the-world-slug",
            "friends": 0,
        })

        with mock.patch.object(app, "fetch_url", side_effect=[status_response, None]):
            result = app.check_instance_status("https://foundry.example")

        self.assertEqual(result[1]["players"], "0 connected")

    def test_inactive_instance_is_online_and_preserves_absolute_background(self):
        status_response = json.dumps({
            "active": False,
            "background": "https://cdn.example/background.webp",
        })

        with mock.patch.object(app, "fetch_url", return_value=status_response) as fetch_url:
            result = app.check_instance_status("https://foundry.example/")

        self.assertEqual(
            result,
            ("online", None, "https://cdn.example/background.webp"),
        )
        fetch_url.assert_called_once_with("https://foundry.example/api/status")

    def test_unavailable_or_invalid_status_api_is_offline(self):
        for response in (None, "not json"):
            with self.subTest(response=response):
                with mock.patch.object(app, "fetch_url", return_value=response):
                    self.assertEqual(
                        app.check_instance_status("https://foundry.example"),
                        ("offline", None, None),
                    )


class FetchUrlTests(unittest.TestCase):
    def test_network_errors_return_none(self):
        with mock.patch("app.urllib.request.urlopen", side_effect=URLError("unreachable")):
            self.assertIsNone(app.fetch_url("https://foundry.example/api/status"))


if __name__ == "__main__":
    unittest.main()
