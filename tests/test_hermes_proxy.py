import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from flask import Flask

from hermes_proxy import create_hermes_blueprint


class FakeUpstreamResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status = status
        self.headers = headers or {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        }

    def read(self, size=-1):
        return self._body if size is None or size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class HermesProxyTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(
            create_hermes_blueprint("http://127.0.0.1:8765"),
            url_prefix="/hermes",
        )
        app.testing = True
        self.client = app.test_client()

    @patch("hermes_proxy.urllib.request.urlopen")
    def test_message_is_forwarded_with_auth_and_body(self, urlopen):
        urlopen.return_value = FakeUpstreamResponse(
            json.dumps({"reply": "hello", "session_id": "hermes-session"}).encode()
        )

        response = self.client.post(
            "/hermes/message",
            json={"message": "ping"},
            headers={"Authorization": "Bearer external-test-key"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"reply": "hello", "session_id": "hermes-session"},
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/message")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer external-test-key")
        self.assertEqual(json.loads(request.data), {"message": "ping"})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 910)

    @patch("hermes_proxy.urllib.request.urlopen")
    def test_authenticated_health_is_forwarded(self, urlopen):
        urlopen.return_value = FakeUpstreamResponse(b'{"status":"ok"}')

        response = self.client.get(
            "/hermes/health",
            headers={"Authorization": "Bearer external-test-key"},
        )

        self.assertEqual(response.status_code, 200)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/health")
        self.assertEqual(request.method, "GET")

    @patch("hermes_proxy.urllib.request.urlopen")
    def test_oversized_body_is_rejected_before_upstream(self, urlopen):
        response = self.client.post(
            "/hermes/message",
            data=b"x" * 262_145,
            content_type="application/json",
            headers={"Authorization": "Bearer external-test-key"},
        )

        self.assertEqual(response.status_code, 413)
        urlopen.assert_not_called()

    @patch("hermes_proxy.urllib.request.urlopen")
    def test_upstream_auth_error_is_preserved(self, urlopen):
        body = b'{"error":{"code":"invalid_api_key","message":"Invalid API key"}}'
        urlopen.side_effect = urllib.error.HTTPError(
            "http://127.0.0.1:8765/message",
            401,
            "Unauthorized",
            {"Content-Type": "application/json"},
            io.BytesIO(body),
        )

        response = self.client.post(
            "/hermes/message",
            json={"message": "ping"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_api_key")

    @patch("hermes_proxy.urllib.request.urlopen")
    def test_unreachable_upstream_returns_sanitized_502(self, urlopen):
        urlopen.side_effect = urllib.error.URLError("private internal detail")

        response = self.client.post(
            "/hermes/message",
            json={"message": "ping"},
            headers={"Authorization": "Bearer external-test-key"},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"]["code"], "hermes_bridge_unavailable")
        self.assertNotIn("private internal detail", response.get_data(as_text=True))

    def test_unrecognized_hermes_path_is_not_exposed(self):
        response = self.client.get("/hermes/api/sessions")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
