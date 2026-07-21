import argparse
import json
import re
import ssl
import urllib.parse
import urllib.request
from html.parser import HTMLParser


class JoinPageParser(HTMLParser):
    """Extract the title and visible text from a Foundry join page."""

    def __init__(self):
        super().__init__()
        self.title = None
        self._in_title = False
        self._text_parts = []

    @property
    def text(self):
        return " ".join(self._text_parts)

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        self._text_parts.append(text)
        if self._in_title and self.title is None:
            self.title = text


def fetch_url(url, timeout=10):
    """Fetch a URL while allowing Foundry instances with self-signed TLS."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "FoundryPortal/1.0"},
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=context,
        ) as response:
            return response.read().decode("utf-8")
    except Exception:
        return None


def normalize_base_url(url):
    """Normalize a configured, join-page, or status URL to its instance base."""
    parsed = urllib.parse.urlsplit(url.strip())
    path = parsed.path.rstrip("/")

    if path.endswith("/api/status"):
        path = path[:-len("/api/status")]
    elif path.endswith("/join") or path.endswith("/auth"):
        path = path.rsplit("/", 1)[0]

    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path.rstrip("/"), "", "")
    )


def _background_url(base_url, background):
    if not isinstance(background, str) or not background:
        return None
    if background.startswith(("http://", "https://")):
        return background
    return base_url + "/" + background.lstrip("/")


def debug_scraper(url, timeout=10):
    """Probe and print Foundry status using its HTTP endpoints."""
    base_url = normalize_base_url(url)
    print(f"Probing Foundry instance: {base_url}")

    status_url = base_url + "/api/status"
    status_response = fetch_url(status_url, timeout=timeout)
    if status_response is None:
        print("State: offline")
        print(f"Unable to fetch: {status_url}")
        return 1

    try:
        status_data = json.loads(status_response)
    except (json.JSONDecodeError, TypeError):
        print("State: offline")
        print("The status endpoint did not return valid JSON.")
        return 1

    if not isinstance(status_data, dict):
        print("State: offline")
        print("The status endpoint returned an unexpected JSON value.")
        return 1

    print("Status response:")
    print(json.dumps(status_data, indent=2, sort_keys=True))

    background = _background_url(base_url, status_data.get("background"))
    if status_data.get("active") and status_data.get("world"):
        world_name = status_data["world"]
        players = "Unknown / Unknown"
        join_url = base_url + "/join"
        join_html = fetch_url(join_url, timeout=timeout)

        if join_html:
            parser = JoinPageParser()
            try:
                parser.feed(join_html)
            except (TypeError, ValueError):
                pass
            if parser.title:
                world_name = parser.title

            player_match = re.search(
                r"Current\s+Players\s*(\d+)\s*/\s*(\d+)",
                parser.text,
                flags=re.IGNORECASE,
            )
            if player_match:
                players = f"{player_match.group(1)} / {player_match.group(2)}"

        print("State: active")
        print(f"World: {world_name}")
        print(f"Players: {players}")
        print(f"Background: {background or 'not provided'}")
        return 0

    print("State: online")
    print("World: none active")
    print(f"Background: {background or 'not provided'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Probe a Foundry instance through its HTTP status endpoint."
    )
    parser.add_argument(
        "url",
        help="Foundry instance base URL, join URL, or status URL",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10,
        help="request timeout in seconds (default: 10)",
    )
    args = parser.parse_args(argv)
    return debug_scraper(args.url, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
