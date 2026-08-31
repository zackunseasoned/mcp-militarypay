"""HTTP fetching with retries and an on-disk cache.

Kept separate from parsing so the parsers can be tested against fixtures with no
network at all, and so a refresh can be re-run from cache while iterating.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx

USER_AGENT = (
    "mcp-militarypay/0.1 (+https://github.com/zackunseasoned/mcp-militarypay) "
    "public rate table reader"
)

DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 4


class FetchError(RuntimeError):
    """Raised when a source could not be retrieved after retries."""


def cache_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "cache"


def _cache_path(url: str, suffix: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return cache_dir() / f"{digest}{suffix}"


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

    Retries only transport errors and 5xx/429 responses; a 404 fails fast,
    because for these sources it means the URL pattern is wrong rather than the
    server being unhappy.
    """
    path = _cache_path(url, suffix)
    if use_cache and not refresh and path.exists():
        return path.read_bytes()

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = httpx.get(
                url,
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            if response.status_code >= 500 or response.status_code == 429:
                raise httpx.HTTPStatusError(
                    f"{response.status_code} from {url}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            content = response.content
            if use_cache:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            return content
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status < 500 and status != 429:
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
