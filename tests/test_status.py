"""Offline tests for the portal's status probe and auth surface.

No network is used: every HTTP call is replaced with a scripted responder.
"""

import os
import tempfile
import unittest
import urllib.error

_TMP = tempfile.mkdtemp(prefix="foundry-portal-tests-")
os.environ.setdefault("PORTAL_CONFIG", os.path.join(_TMP, "absent-config.yaml"))
os.environ.setdefault("PORTAL_SECRET_FILE", os.path.join(_TMP, "secret-key"))
os.environ.setdefault("PORTAL_DISABLE_SCHEDULER", "1")
os.environ.setdefault("PORTAL_INSECURE_COOKIES", "1")

import app as portal  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

JOIN_PAGE = """<html><head><title>Avaraice at Angler&#39;s Tooth</title>
<style>body.background { --background-url: url("/custom_assets/avarice/cover.png"); }</style>
</head><body class="auth join"><h1>Avaraice at Angler&#39;s Tooth</h1></body></html>"""

AUTH_PAGE = """<html><head><title>Foundry Virtual Tabletop</title></head>
<body class="auth flexcol theme-fantasy"><h1>Foundry Virtual Tabletop</h1></body></html>"""


class Responder:
    def __init__(self, payload, pages=None, error=None):
        self.payload = payload
        self.pages = pages or {}
        self.error = error
        self.requested = []

    def __call__(self, url):
        self.requested.append(url)
        if self.error:
            raise self.error
        if url.endswith("/api/status"):
            import json

            return json.dumps(self.payload)
        for fragment, body in self.pages.items():
            if url.endswith(fragment):
                return body
        raise urllib.error.HTTPError(url, 404, "not found", None, None)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self._original = portal._http_get

    def tearDown(self):
        portal._http_get = self._original

    def test_active_world_uses_join_page_title_and_background(self):
        responder = Responder(
            {"active": True, "world": "avaraice-at-anglers-tooth", "users": 1},
            {"/join": JOIN_PAGE},
        )
        portal._http_get = responder

        status, world, background = portal.probe_instance("https://f.example.com")

        self.assertEqual(status, "active")
        self.assertEqual(world["name"], "Avaraice at Angler's Tooth")
        self.assertEqual(world["players"], "1 connected")
        self.assertEqual(world["background"], "https://f.example.com/custom_assets/avarice/cover.png")
        self.assertEqual(background, "https://f.example.com/custom_assets/avarice/cover.png")
        self.assertEqual(
            responder.requested,
            ["https://f.example.com/api/status", "https://f.example.com/join"],
        )

    def test_online_instance_reports_default_setup_background(self):
        responder = Responder({"active": False, "version": "14.365"}, {"/auth": AUTH_PAGE})
        portal._http_get = responder

        status, world, background = portal.probe_instance("https://f.example.com/")

        self.assertEqual(status, "online")
        self.assertIsNone(world)
        self.assertEqual(background, "https://f.example.com/ui/backgrounds/setup.webp")

    def test_unreachable_instance_is_offline(self):
        portal._http_get = Responder({}, {}, urllib.error.URLError("connection refused"))

        status, world, background = portal.probe_instance("https://down.example.com")

        self.assertEqual(status, "offline")
        self.assertIsNone(world)
        self.assertEqual(background, portal.DEFAULT_BACKGROUND)

    def test_http_failure_is_error_not_offline(self):
        portal._http_get = Responder({}, {}, urllib.error.HTTPError("u", 500, "boom", None, None))

        status, _, _ = portal.probe_instance("https://broken.example.com")

        self.assertEqual(status, "error")

    def test_route_prefix_is_not_duplicated(self):
        responder = Responder(
            {"active": True, "world": "mod", "users": 0},
            {
                "/join": '<html><head><title>Mod</title></head><body '
                'style="--background-url: url(\'/1/custom_assets/cover.png\')"><h1>Mod</h1></body></html>'
            },
        )
        portal._http_get = responder

        status, world, _ = portal.probe_instance("https://vtt.example.com/1")

        self.assertEqual(status, "active")
        self.assertEqual(responder.requested[0], "https://vtt.example.com/1/api/status")
        self.assertEqual(world["background"], "https://vtt.example.com/1/custom_assets/cover.png")


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.config_path = os.path.join(_TMP, f"config-{id(self)}.yaml")
        self._original_config = portal.CONFIG_FILE
        self._original_update = portal.update_instance_statuses
        self._original_seed = portal.initialize_instance_data
        portal.CONFIG_FILE = self.config_path
        portal.update_instance_statuses = lambda: None
        portal.initialize_instance_data = lambda: None
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(
                "shared_data_mode: true\n"
                "admin_password_hash: %r\n"
                "viewer_password_hash: %r\n"
                "instances:\n- name: Tiamat\n  url: https://foundry.example.com\n"
                % (generate_password_hash("s3cret-admin"), generate_password_hash("s3cret-viewer"))
            )
        portal.instance_data_cache = [
            {
                "name": "Tiamat",
                "url": "https://foundry.example.com",
                "status": "online",
                "active_world": None,
                "background": "/static/images/background.jpg",
            }
        ]
        self.client = portal.app.test_client()

    def tearDown(self):
        portal.CONFIG_FILE = self._original_config
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    def test_home_renders_viewport_and_state(self):
        response = self.client.get("/")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="viewport"', body)
        self.assertIn('"viewerLocked": true', body.replace("'", '"'))

    def test_instance_status_requires_viewer_when_locked(self):
        self.assertEqual(self.client.get("/api/instance-status").status_code, 401)

        denied = self.client.post(
            "/api/login", json={"password": "wrong", "role": "viewer"}
        )
        self.assertEqual(denied.status_code, 401)

        allowed = self.client.post(
            "/api/login", json={"password": "s3cret-viewer", "role": "viewer"}
        )
        self.assertEqual(allowed.status_code, 200)
        payload = self.client.get("/api/instance-status").get_json()
        self.assertEqual(payload[0]["name"], "Tiamat")

    def test_config_requires_admin_and_validates_urls(self):
        self.assertEqual(self.client.get("/api/config").status_code, 401)

        self.client.post("/api/login", json={"password": "s3cret-admin", "role": "admin"})
        rejected = self.client.post("/api/config", json={"instances": [{"name": "x", "url": "ftp://nope"}]})
        self.assertEqual(rejected.status_code, 400)

        accepted = self.client.post(
            "/api/config",
            json={"instances": [{"name": "Tiamat", "url": "https://a.example.com"}]},
        )
        self.assertEqual(accepted.status_code, 200)

    def test_init_is_local_only(self):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("shared_data_mode: false\ninstances: []\n")

        denied = self.client.post(
            "/api/init", json={"admin_password": "x"}, environ_overrides={"REMOTE_ADDR": "203.0.113.9"}
        )
        self.assertEqual(denied.status_code, 403)

        allowed = self.client.post(
            "/api/init", json={"admin_password": "x"}, environ_overrides={"REMOTE_ADDR": "127.0.0.1"}
        )
        self.assertEqual(allowed.status_code, 200)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.config_path = os.path.join(_TMP, f"cache-config-{id(self)}.yaml")
        self._original_config = portal.CONFIG_FILE
        self._original_get = portal._http_get
        self._original_cache = portal.instance_data_cache
        portal.CONFIG_FILE = self.config_path
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(
                "shared_data_mode: false\n"
                "instances:\n"
                "- name: Tiamat\n  url: https://a.example.com\n"
                "- name: Gruumsh\n  url: https://b.example.com\n"
            )
        portal.instance_data_cache = []

    def tearDown(self):
        portal.CONFIG_FILE = self._original_config
        portal._http_get = self._original_get
        portal.instance_data_cache = self._original_cache
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    def test_update_publishes_results_to_the_shared_cache(self):
        portal._http_get = _RouteResponder()
        portal.update_instance_statuses()

        statuses = {entry["name"]: entry["status"] for entry in portal.instance_data_cache}
        self.assertEqual(statuses, {"Tiamat": "online", "Gruumsh": "active"})


class _RouteResponder:
    def __call__(self, url):
        import json

        if url.endswith("/api/status"):
            active = url.startswith("https://b.example.com")
            return json.dumps({"active": active, "world": "mod" if active else None, "users": 2})
        if url.endswith("/join"):
            return JOIN_PAGE
        return AUTH_PAGE


if __name__ == "__main__":
    unittest.main()
