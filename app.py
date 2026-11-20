"""Foundry Portal: a status board for a hive of Foundry VTT instances.

Instance state is read from Foundry's public ``/api/status`` endpoint and the
``/join`` page. No browser is launched: the previous Selenium implementation
spawned one headless Chrome per instance every ten seconds and leaked zombie
processes when Chrome crashed (upstream issue #3).
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from hermes_proxy import create_hermes_blueprint

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
LOG = logging.getLogger("foundry_portal")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("PORTAL_CONFIG", os.path.join(APP_DIR, "config.yaml"))
SECRET_FILE = os.environ.get("PORTAL_SECRET_FILE", os.path.join(APP_DIR, ".secret_key"))
DEFAULT_BACKGROUND = "/static/images/background.jpg"
REFRESH_SECONDS = int(os.environ.get("PORTAL_REFRESH_SECONDS", "10"))
REQUEST_TIMEOUT = float(os.environ.get("PORTAL_REQUEST_TIMEOUT", "8"))
VERIFY_TLS = os.environ.get("PORTAL_VERIFY_TLS", "0") == "1"
MAX_INSTANCES = 50
MAX_PAGE_BYTES = 2 * 1024 * 1024
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_FAILURES = 10


def _load_secret_key() -> str:
    """Return a stable secret key so sessions survive restarts."""
    env_key = os.environ.get("PORTAL_SECRET_KEY")
    if env_key:
        return env_key
    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as handle:
            stored = handle.read().strip()
        if stored:
            return stored
    except FileNotFoundError:
        pass
    key = os.urandom(32).hex()
    fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key)
    return key


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PORTAL_INSECURE_COOKIES", "0") != "1",
)
app.register_blueprint(create_hermes_blueprint(), url_prefix="/hermes")

instance_data_cache: list[dict] = []
_cache_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config() -> dict | None:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
    except FileNotFoundError:
        LOG.error("Config file %s not found.", CONFIG_FILE)
        return None
    except yaml.YAMLError as exc:
        LOG.error("Config file %s is not valid YAML: %s", CONFIG_FILE, exc)
        return None
    if not isinstance(config, dict):
        LOG.error("Config file %s is not a mapping.", CONFIG_FILE)
        return None
    if not isinstance(config.get("instances", []), list):
        LOG.error("Config file %s has a non-list 'instances' value.", CONFIG_FILE)
        return None
    config.setdefault("instances", [])
    config["shared_data_mode"] = bool(config.get("shared_data_mode", False))
    return config


def save_config(config: dict) -> None:
    """Write the configuration atomically so a crash cannot truncate it."""
    temp_path = f"{CONFIG_FILE}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=False)
    os.replace(temp_path, CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def _has_admin(config: dict | None) -> bool:
    return bool(config and config.get("admin_password_hash"))


def _viewer_locked(config: dict | None) -> bool:
    if not config or not config.get("viewer_password_hash"):
        return False
    return not (session.get("viewer_logged_in") or session.get("admin_logged_in"))


# ---------------------------------------------------------------------------
# Foundry instance probing
# ---------------------------------------------------------------------------


class _PageParser(HTMLParser):
    """Extract the first <h1> and the <title> text from a Foundry page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.h1: str | None = None
        self.title: str | None = None
        self._current: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "h1" and self.h1 is None:
            self._current = "h1"
            self._buffer = []
        elif tag == "title" and self.title is None:
            self._current = "title"
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == self._current:
            text = " ".join("".join(self._buffer).split())
            if text:
                if tag == "h1" and self.h1 is None:
                    self.h1 = text
                elif tag == "title" and self.title is None:
                    self.title = text
            self._current = None
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._current:
            self._buffer.append(data)


_BACKGROUND_RE = re.compile(
    r"--background-url\s*:\s*url\(\s*[\"']?([^\"')]+)[\"']?\s*\)",
    re.IGNORECASE,
)


def _http_get(url: str) -> str:
    context = ssl.create_default_context()
    if not VERIFY_TLS:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = Request(url, headers={"User-Agent": "FoundryPortal/2"})
    with urlopen(request, timeout=REQUEST_TIMEOUT, context=context) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read(MAX_PAGE_BYTES).decode(charset, "replace")


def _fetch_json(url: str) -> dict:
    payload = json.loads(_http_get(url))
    if not isinstance(payload, dict):
        raise ValueError("status payload is not an object")
    return payload


def _absolute_url(base: str, candidate: str | None) -> str | None:
    """Resolve a background URL without duplicating a host's route prefix."""
    if not candidate:
        return None
    try:
        resolved = urljoin(base.rstrip("/") + "/", candidate.strip())
    except ValueError:
        return None
    parsed = urlparse(resolved)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return resolved


def _page_details(html: str, base: str) -> tuple[str | None, str | None]:
    parser = _PageParser()
    try:
        parser.feed(html)
    except Exception:  # HTMLParser is forgiving; never fail a status check on it
        LOG.debug("Could not parse page for %s", base, exc_info=True)
    match = _BACKGROUND_RE.search(html)
    background = _absolute_url(base, match.group(1)) if match else None
    return (parser.h1 or parser.title), background


def probe_instance(instance_url: str) -> tuple[str, dict | None, str]:
    """Return (status, active_world, background) for one Foundry instance.

    ``status`` is one of ``active``, ``online``, ``offline`` or ``error``.
    """
    base = instance_url.strip().rstrip("/")
    if not base:
        return "error", None, DEFAULT_BACKGROUND

    try:
        data = _fetch_json(base + "/api/status")
    except HTTPError as exc:
        LOG.warning("Status endpoint for %s returned HTTP %s", base, exc.code)
        return "error", None, DEFAULT_BACKGROUND
    except (URLError, TimeoutError, OSError) as exc:
        LOG.info("Instance %s is unreachable: %s", base, exc)
        return "offline", None, DEFAULT_BACKGROUND
    except (ValueError, json.JSONDecodeError) as exc:
        LOG.warning("Instance %s returned an invalid status payload: %s", base, exc)
        return "error", None, DEFAULT_BACKGROUND

    if data.get("active") and data.get("world"):
        world_name = str(data["world"])
        background = None
        try:
            page, background = _page_details(_http_get(base + "/join"), base)
            if page:
                world_name = page
        except (URLError, TimeoutError, OSError, HTTPError) as exc:
            LOG.warning("Could not read the join page for %s: %s", base, exc)
        users = data.get("users")
        players = f"{users} connected" if isinstance(users, int) else "Unknown"
        active_world = {
            "name": world_name,
            "background": background,
            "players": players,
        }
        return "active", active_world, background or DEFAULT_BACKGROUND

    background = None
    try:
        _, background = _page_details(_http_get(base + "/auth"), base)
    except (URLError, TimeoutError, OSError, HTTPError):
        background = None
    if not background:
        background = _absolute_url(base, "ui/backgrounds/setup.webp")
    return "online", None, background or DEFAULT_BACKGROUND


def initialize_instance_data() -> None:
    global instance_data_cache
    config = load_config()
    if not config:
        with _cache_lock:
            instance_data_cache = []
        return
    seeded = []
    for entry in config.get("instances", []):
        if not isinstance(entry, dict) or "name" not in entry or "url" not in entry:
            continue
        seeded.append(
            {
                "name": str(entry["name"]),
                "url": str(entry["url"]),
                "status": "offline",
                "active_world": None,
                "background": DEFAULT_BACKGROUND,
            }
        )
    with _cache_lock:
        instance_data_cache = seeded
    LOG.info("Seeded %d instances from configuration.", len(seeded))


def update_instance_statuses() -> None:
    global instance_data_cache
    config = load_config()
    if not config:
        LOG.error("Skipping status update: configuration could not be loaded.")
        return

    entries = [
        entry
        for entry in config.get("instances", [])
        if isinstance(entry, dict) and entry.get("name") and entry.get("url")
    ]
    if not entries:
        with _cache_lock:
            instance_data_cache = []
        return

    def check(entry: dict) -> dict:
        status, active_world, background = probe_instance(str(entry["url"]))
        return {
            "name": str(entry["name"]),
            "url": str(entry["url"]),
            "status": status,
            "active_world": active_world,
            "background": background,
        }

    with ThreadPoolExecutor(max_workers=min(8, len(entries))) as pool:
        results = list(pool.map(check, entries))
    with _cache_lock:
        instance_data_cache = results
    LOG.info("Updated %d instance statuses.", len(results))


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------


def _client_ip() -> str:
    return request.remote_addr or "unknown"


def _too_many_failures(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        attempts = [stamp for stamp in _login_failures.get(ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
        _login_failures[ip] = attempts
        return len(attempts) >= LOGIN_MAX_FAILURES


def _record_failure(ip: str) -> None:
    now = time.time()
    with _login_lock:
        _login_failures.setdefault(ip, []).append(now)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        return view(*args, **kwargs)

    return wrapped


def viewer_or_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _viewer_locked(load_config()):
            return jsonify({"error": "Unauthorized"}), 401
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/api/instance-status")
@viewer_or_admin_required
def api_instance_status():
    with _cache_lock:
        snapshot = list(instance_data_cache)
    return jsonify(snapshot)


@app.route("/api/login", methods=["POST"])
def login():
    ip = _client_ip()
    if _too_many_failures(ip):
        return jsonify({"success": False, "error": "Too many attempts"}), 429

    payload = request.get_json(silent=True) or {}
    password = payload.get("password")
    role = payload.get("role", "admin")
    config = load_config() or {}

    if not isinstance(password, str):
        return jsonify({"success": False, "error": "Invalid password"}), 401

    if role == "admin":
        stored = config.get("admin_password_hash")
        if stored and check_password_hash(stored, password):
            session.clear()
            session["admin_logged_in"] = True
            return jsonify({"success": True})
    elif role == "viewer":
        stored = config.get("viewer_password_hash")
        if stored and check_password_hash(stored, password):
            session.clear()
            session["viewer_logged_in"] = True
            return jsonify({"success": True})

    _record_failure(ip)
    return jsonify({"success": False, "error": "Invalid password"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/config", methods=["GET", "POST"])
@admin_required
def handle_config():
    config = load_config()
    if config is None:
        return jsonify({"error": "Configuration unavailable"}), 500

    if request.method == "GET":
        return jsonify(
            {
                "shared_data_mode": config.get("shared_data_mode", False),
                "instances": config.get("instances", []),
                "viewer_access_enabled": bool(config.get("viewer_password_hash")),
            }
        )

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    if "shared_data_mode" in payload:
        config["shared_data_mode"] = bool(payload["shared_data_mode"])

    if "instances" in payload:
        instances = payload["instances"]
        if not isinstance(instances, list) or len(instances) > MAX_INSTANCES:
            return jsonify({"error": "Invalid instance list"}), 400
        cleaned = []
        for entry in instances:
            if not isinstance(entry, dict):
                return jsonify({"error": "Invalid instance entry"}), 400
            name = str(entry.get("name", "")).strip()
            url = str(entry.get("url", "")).strip()
            if not name or not url:
                return jsonify({"error": "Instance name and URL are required"}), 400
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return jsonify({"error": f"Invalid instance URL: {url}"}), 400
            cleaned.append({"name": name[:100], "url": url})
        config["instances"] = cleaned

    new_admin = payload.get("new_admin_password")
    if isinstance(new_admin, str) and new_admin:
        config["admin_password_hash"] = generate_password_hash(new_admin)

    new_viewer = payload.get("new_viewer_password")
    if isinstance(new_viewer, str):
        if new_viewer:
            config["viewer_password_hash"] = generate_password_hash(new_viewer)
        else:
            config.pop("viewer_password_hash", None)

    save_config(config)
    initialize_instance_data()
    update_instance_statuses()
    return jsonify({"success": True})


@app.route("/api/init", methods=["POST"])
def init_config():
    """One-time admin setup, restricted to a request made on the host itself."""
    config = load_config()
    if _has_admin(config):
        return jsonify({"error": "Already configured"}), 403
    if request.remote_addr not in ("127.0.0.1", "::1") and os.environ.get(
        "PORTAL_ALLOW_REMOTE_INIT"
    ) != "1":
        return jsonify({"error": "Initialization must be run locally"}), 403

    payload = request.get_json(silent=True) or {}
    password = payload.get("admin_password")
    if not isinstance(password, str) or not password:
        return jsonify({"error": "Password required"}), 400

    config = config or {}
    config["admin_password_hash"] = generate_password_hash(password)
    config.setdefault("shared_data_mode", False)
    config.setdefault("instances", [])
    save_config(config)
    return jsonify({"success": True})


def _asset_version() -> str:
    newest = 0.0
    for relative in (
        "static/css/styles.css",
        "static/js/main.js",
        "templates/index.html",
    ):
        path = os.path.join(APP_DIR, relative)
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    return str(int(newest))


@app.route("/")
def home():
    config = load_config()
    shared_data_mode = bool(config.get("shared_data_mode", False)) if config else False
    return render_template(
        "index.html",
        shared_data_mode=shared_data_mode,
        is_configured=_has_admin(config),
        viewer_locked=_viewer_locked(config),
        is_admin=bool(session.get("admin_logged_in")),
        asset_version=_asset_version(),
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

if os.environ.get("PORTAL_DISABLE_SCHEDULER") != "1":
    initialize_instance_data()
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        func=update_instance_statuses,
        trigger="interval",
        seconds=REFRESH_SECONDS,
        id="update_status_job",
        max_instances=1,
        replace_existing=True,
    )
    scheduler.start()
    threading.Thread(target=update_instance_statuses, daemon=True).start()
    import atexit

    atexit.register(lambda: scheduler.shutdown())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
