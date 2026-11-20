# 001 Hermes Webhook Proxy

## Scope

Expose only the authenticated sender-to-Hermes message and health operations through the existing Foundry portal and the TLS-terminating reverse proxy in front of it, while keeping Hermes' native API on loopback.

## Acceptance Criteria

- [x] The Foundry portal exposes `POST /hermes/message` and `GET /hermes/health` and forwards them to the local webhook bridge.
- [x] The proxy preserves the request authorization header, content type, method, and body, and returns the upstream status, JSON body, and selected security headers.
- [x] Other paths under `/hermes/` are not exposed by the portal application.
- [x] Requests larger than 256 KiB are rejected before dispatch to the local webhook.
- [x] Unreachable upstreams and oversized upstream responses produce bounded, sanitized errors without leaking internal details.
- [x] The TLS-terminating reverse proxy applies a 256 KiB body limit to `/hermes/` and provides timeouts suitable for synchronous Hermes responses.
- [x] Existing Foundry portal routes remain available after the proxy is registered.

## Verification

- [x] Ran `python -m unittest discover -s tests` with the portal directory on `PYTHONPATH`; all 33 tests passed, including all 6 Hermes proxy contract tests.
- [x] Confirmed unauthenticated `GET /hermes/health` through the public TLS route returned HTTP 401.
- [x] Sent an authenticated request through the public TLS route and received the exact Hermes response `HERMES_WEBHOOK_RESUMED`.
- [x] Sent a follow-up request through the same public route and observed Hermes recall `HERMES_WEBHOOK_RESUMED`, confirming that proxying preserves the persistent chat path.
- [x] Confirmed the portal root still returned HTTP 200 after deploying the route.
- [x] Confirmed the portal service remained active and running after loading the proxy module.
- [x] Ran `nginx -t` on the proxy host after deployment; configuration syntax and validation succeeded.
- [x] Confirmed the proxy service was active after reloading the bounded `/hermes/` location.
