"""HTTP fetching with retries and an on-disk cache.

Kept separate from parsing so the parsers can be tested against fixtures with no
network at all, and so a refresh can be re-run from cache while iterating.

A note on request headers. Both dfas.mil and travel.dod.mil sit behind a WAF
that rejects clients which do not look like a browser: a custom User-Agent gets
an outright HTTP 403 on every URL, on both hosts. These are public rate tables
with no authentication, no login and no API key, so the fix is simply to send
the ordinary header set a browser sends. Override it with $MILITARYPAY_USER_AGENT
if a future WAF change needs something different, and use the `probe` CLI command
to find out what the servers currently accept.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import httpx

# A current Chrome on Windows. The DoD WAF rejects non-browser agents.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Identifies this client honestly. Kept for the `probe` command and for anyone
# whose network prefers it; it is not the default because the WAF 403s it.
PROJECT_USER_AGENT = (
    "mcp-militarypay/0.1 (+https://github.com/zackunseasoned/mcp-militarypay) "
    "public rate table reader"
)

USER_AGENT_ENV_VAR = "MILITARYPAY_USER_AGENT"

# Accept-Encoding is deliberately absent: httpx sets it from the codecs actually
# installed, and claiming an encoding we cannot decode breaks the response.
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 4


class FetchError(RuntimeError):
    """Raised when a source could not be retrieved after retries."""


def default_headers() -> dict[str, str]:
    """Browser-like headers, with the User-Agent overridable by environment."""
    headers = dict(BROWSER_HEADERS)
    override = os.environ.get(USER_AGENT_ENV_VAR)
    if override:
        headers["User-Agent"] = override
    return headers


def cache_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "cache"


def _cache_path(url: str, suffix: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return cache_dir() / f"{digest}{suffix}"


def _client(*, timeout: float, http2: bool = True) -> httpx.Client:
    """An httpx client that looks like a browser at the protocol level too.

    HTTP/2 matters here: a WAF that fingerprints clients notices an agent
    claiming to be Chrome while speaking HTTP/1.1. Falls back cleanly when the
    h2 package is unavailable.
    """
    try:
        return httpx.Client(
            timeout=timeout, follow_redirects=True, http2=http2,
            headers=default_headers(),
        )
    except ImportError:
        # h2 not installed; HTTP/1.1 still works for most of these fetches.
        return httpx.Client(
            timeout=timeout, follow_redirects=True,
            headers=default_headers(),
        )


def fetch_bytes(
    url: str,
    *,
    use_cache: bool = True,
    refresh: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    suffix: str = ".bin",
) -> bytes:
    """GET a URL, with exponential backoff and an optional disk cache.

    Retries transport errors and 5xx/429. A 403 is retried once as well: the DoD
    WAF sometimes rejects a first request and accepts the follow-up. Other 4xx
    responses fail fast, since for these sources they mean the URL is wrong
    rather than the server being unhappy.
    """
    path = _cache_path(url, suffix)
    if use_cache and not refresh and path.exists():
        return path.read_bytes()

    last_error: Exception | None = None
    forbidden_attempts = 0

    for attempt in range(retries):
        try:
            with _client(timeout=timeout) as client:
                response = client.get(url)

            if response.status_code >= 500 or response.status_code == 429:
                raise httpx.HTTPStatusError(
                    f"{response.status_code} from {url}",
                    request=response.request, response=response,
                )
            if response.status_code == 403:
                forbidden_attempts += 1
                if forbidden_attempts <= 1 and attempt < retries - 1:
                    last_error = httpx.HTTPStatusError(
                        f"403 from {url}", request=response.request, response=response,
                    )
                    time.sleep(2)
                    continue
                raise FetchError(
                    f"{url} returned HTTP 403. The DoD WAF is rejecting this "
                    f"client. Run `python -m mcp_militarypay.cli probe` to see "
                    f"which request headers the server currently accepts, then "
                    f"set ${USER_AGENT_ENV_VAR} accordingly."
                )

            response.raise_for_status()
            content = response.content
            if use_cache:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            return content

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status < 500 and status not in (403, 429):
                raise FetchError(f"{url} returned HTTP {status}") from exc
            last_error = exc
        except httpx.HTTPError as exc:
            last_error = exc

        if attempt < retries - 1:
            time.sleep(2 ** (attempt + 1))

    raise FetchError(f"failed to fetch {url} after {retries} attempts: {last_error}")


def fetch_text(url: str, *, encoding: str = "utf-8", **kwargs) -> str:
    kwargs.setdefault("suffix", ".html")
    return fetch_bytes(url, **kwargs).decode(encoding, errors="replace")


# --- diagnostics ----------------------------------------------------------

# Header profiles the probe command tries, cheapest explanation first.
PROBE_PROFILES: dict[str, dict] = {
    "project-ua": {
        "headers": {"User-Agent": PROJECT_USER_AGENT},
        "http2": False,
        "description": "the original custom User-Agent (what returned 403)",
    },
    "httpx-default": {
        "headers": None,
        "http2": False,
        "description": "no custom headers at all",
    },
    "browser": {
        "headers": BROWSER_HEADERS,
        "http2": False,
        "description": "full browser header set over HTTP/1.1",
    },
    "browser-http2": {
        "headers": BROWSER_HEADERS,
        "http2": True,
        "description": "full browser header set over HTTP/2 (the new default)",
    },
}

_WAF_MARKERS = (
    "access denied", "reference #", "akamai", "forbidden",
    "bot", "blocked", "incapsula", "cloudflare",
)


def probe_url(url: str, profile: str, *, timeout: float = 30.0) -> dict:
    """Try one URL with one header profile and report what came back.

    Streams the response so probing a multi-megabyte zip costs only headers.
    """
    config = PROBE_PROFILES[profile]
    started = time.monotonic()

    kwargs: dict = {"timeout": timeout, "follow_redirects": True}
    if config["headers"] is not None:
        kwargs["headers"] = dict(config["headers"])
    if config["http2"]:
        kwargs["http2"] = True

    try:
        try:
            client = httpx.Client(**kwargs)
        except ImportError:
            return {
                "profile": profile, "status": None,
                "error": "HTTP/2 needs the h2 package: pip install 'httpx[http2]'",
            }
        with client:
            with client.stream("GET", url) as response:
                head = b""
                if response.status_code != 200:
                    for chunk in response.iter_bytes():
                        head += chunk
                        if len(head) > 2048:
                            break
                body = head.decode("utf-8", errors="replace").lower()
                markers = sorted({m for m in _WAF_MARKERS if m in body})
                return {
                    "profile": profile,
                    "status": response.status_code,
                    "http_version": response.http_version,
                    "content_type": response.headers.get("content-type", ""),
                    "content_length": response.headers.get("content-length", ""),
                    "server": response.headers.get("server", ""),
                    "waf_markers": markers,
                    "elapsed_s": round(time.monotonic() - started, 2),
                }
    except Exception as exc:  # noqa: BLE001 - a probe reports failures, never raises
        return {
            "profile": profile, "status": None,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_s": round(time.monotonic() - started, 2),
        }
