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

    def test_absolute_background_scheme_is_case_insensitive(self):
        for background in (
            "HTTPS://cdn.example/world.webp",
            "hTtP://cdn.example/world.webp",
        ):
            with self.subTest(background=background):
                status_response = json.dumps({
                    "active": False,
                    "background": background,
                })

                with mock.patch.object(app, "fetch_url", return_value=status_response):
                    result = app.check_instance_status("https://foundry.example")

                self.assertEqual(result, ("online", None, background))

    def test_protocol_relative_background_is_preserved(self):
        status_response = json.dumps({
            "active": False,
            "background": "//cdn.example/world.webp",
        })

        with mock.patch.object(app, "fetch_url", return_value=status_response):
            result = app.check_instance_status("https://foundry.example")

        self.assertEqual(
            result,
            ("online", None, "//cdn.example/world.webp"),
        )

    def test_truthy_non_string_background_is_treated_as_absent(self):
        for background in (1, True, ["world.webp"], {"url": "world.webp"}):
            with self.subTest(background=background):
                status_response = json.dumps({
                    "active": False,
                    "background": background,
                })

                with mock.patch.object(app, "fetch_url", return_value=status_response):
                    result = app.check_instance_status("https://foundry.example")

                self.assertEqual(result, ("online", None, None))

    def test_unavailable_or_invalid_status_api_is_offline(self):
        for response in (None, "not json"):
            with self.subTest(response=response):
                with mock.patch.object(app, "fetch_url", return_value=response):
                    self.assertEqual(
                        app.check_instance_status("https://foundry.example"),
                        ("offline", None, None),
                    )

    def test_valid_non_object_status_payloads_are_offline(self):
        for payload in (None, [], "online", 0, True):
            with self.subTest(payload=payload):
                with mock.patch.object(app, "fetch_url", return_value=json.dumps(payload)):
                    self.assertEqual(
                        app.check_instance_status("https://foundry.example"),
                        ("offline", None, None),
                    )

    def test_error_object_is_offline_but_inactive_status_is_online(self):
        with mock.patch.object(
            app,
            "fetch_url",
            return_value=json.dumps({"error": "not found"}),
        ):
            self.assertEqual(
                app.check_instance_status("https://foundry.example"),
                ("offline", None, None),
            )

        with mock.patch.object(
            app,
            "fetch_url",
            return_value=json.dumps({"active": False}),
        ):
            self.assertEqual(
                app.check_instance_status("https://foundry.example"),
                ("online", None, None),
            )

    def test_non_string_background_does_not_abort_status_refresh(self):
        config = {
            "instances": [
                {"name": "Malformed", "url": "https://malformed.example"},
                {"name": "Healthy", "url": "https://healthy.example"},
            ]
        }
        responses = {
            "https://malformed.example/api/status": json.dumps({
                "active": False,
                "background": 1,
            }),
            "https://healthy.example/api/status": json.dumps({"active": False}),
        }

        with mock.patch.object(app, "instance_data_cache", [{"name": "stale"}]), \
                mock.patch.object(app, "load_config", return_value=config), \
                mock.patch.object(app, "fetch_url", side_effect=responses.get):
            app.update_instance_statuses()

            self.assertEqual(
                app.instance_data_cache,
                [
                    {
                        "name": "Malformed",
                        "url": "https://malformed.example",
                        "status": "online",
                        "active_world": None,
                        "background": "/static/images/background.jpg",
                    },
                    {
                        "name": "Healthy",
                        "url": "https://healthy.example",
                        "status": "online",
                        "active_world": None,
                        "background": "/static/images/background.jpg",
                    },
                ],
            )

    def test_non_object_payload_does_not_abort_status_refresh(self):
        config = {
            "instances": [
                {"name": "Malformed", "url": "https://malformed.example"},
                {"name": "Healthy", "url": "https://healthy.example"},
            ]
        }
        responses = {
            "https://malformed.example/api/status": "null",
            "https://healthy.example/api/status": json.dumps({"active": False}),
        }

        with mock.patch.object(app, "instance_data_cache", [{"name": "stale"}]), \
                mock.patch.object(app, "load_config", return_value=config), \
                mock.patch.object(app, "fetch_url", side_effect=responses.get):
            app.update_instance_statuses()

            self.assertEqual(
                app.instance_data_cache,
                [
                    {
                        "name": "Malformed",
                        "url": "https://malformed.example",
                        "status": "offline",
                        "active_world": None,
                        "background": "/static/images/background.jpg",
                    },
                    {
                        "name": "Healthy",
                        "url": "https://healthy.example",
                        "status": "online",
                        "active_world": None,
                        "background": "/static/images/background.jpg",
                    },
                ],
            )


class FetchUrlTests(unittest.TestCase):
    def test_network_errors_return_none(self):
        with mock.patch("app.urllib.request.urlopen", side_effect=URLError("unreachable")):
            self.assertIsNone(app.fetch_url("https://foundry.example/api/status"))


if __name__ == "__main__":
    unittest.main()
