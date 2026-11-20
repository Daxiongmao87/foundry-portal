"""Narrow reverse proxy from the public Foundry portal to the local Hermes webhook."""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

from flask import Blueprint, Response, jsonify, request


LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 262_144
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
UPSTREAM_TIMEOUT_SECONDS = 910


def create_hermes_blueprint(
    upstream_url: str = "http://127.0.0.1:8765",
) -> Blueprint:
    blueprint = Blueprint("hermes_proxy", __name__)
    upstream_url = upstream_url.rstrip("/")

    def forward(path: str) -> Response:
        content_length = request.content_length
        if content_length is not None and content_length > MAX_REQUEST_BYTES:
            return jsonify(
                error={
                    "code": "request_too_large",
                    "message": "Request body exceeds the size limit",
                }
            ), 413

        payload = None
        if request.method == "POST":
            payload = request.stream.read(MAX_REQUEST_BYTES + 1)
            if len(payload) > MAX_REQUEST_BYTES:
                return jsonify(
                    error={
                        "code": "request_too_large",
                        "message": "Request body exceeds the size limit",
                    }
                ), 413

        headers = {}
        authorization = request.headers.get("Authorization")
        if authorization:
            headers["Authorization"] = authorization
        content_type = request.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type

        upstream_request = urllib.request.Request(
            f"{upstream_url}/{path}",
            data=payload,
            headers=headers,
            method=request.method,
        )
        try:
            with urllib.request.urlopen(
                upstream_request,
                timeout=UPSTREAM_TIMEOUT_SECONDS,
            ) as upstream:
                body = upstream.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    LOGGER.error("Hermes bridge response exceeded size limit")
                    return jsonify(
                        error={
                            "code": "invalid_hermes_response",
                            "message": "Hermes returned an invalid response",
                        }
                    ), 502
                return _proxy_response(body, upstream.status, upstream.headers)
        except urllib.error.HTTPError as error:
            body = error.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return jsonify(
                    error={
                        "code": "invalid_hermes_response",
                        "message": "Hermes returned an invalid response",
                    }
                ), 502
            return _proxy_response(body, error.code, error.headers)
        except (urllib.error.URLError, TimeoutError, OSError):
            LOGGER.error("Local Hermes webhook is unavailable")
            return jsonify(
                error={
                    "code": "hermes_bridge_unavailable",
                    "message": "Hermes webhook is unavailable",
                }
            ), 502

    @blueprint.post("/message")
    def message() -> Response:
        return forward("message")

    @blueprint.get("/health")
    def health() -> Response:
        return forward("health")

    return blueprint


def _proxy_response(body: bytes, status: int, upstream_headers) -> Response:
    response = Response(
        body,
        status=status,
        content_type=upstream_headers.get("Content-Type", "application/json"),
    )
    for name in (
        "Cache-Control",
        "WWW-Authenticate",
        "X-Content-Type-Options",
        "Referrer-Policy",
    ):
        value = upstream_headers.get(name)
        if value:
            response.headers[name] = value
    response.headers.setdefault("Cache-Control", "no-store")
    return response
