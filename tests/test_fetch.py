"""Tests for the HTTP layer, especially the WAF-403 handling.

Both dfas.mil and travel.dod.mil sit behind a WAF that rejected the project's
original custom User-Agent with HTTP 403 on every URL across both hosts. These
tests pin the browser-header default and the diagnostics that led to it.
"""

import httpx
import pytest

from mcp_militarypay import fetch


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda _seconds: None)


def mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        fetch, "_client",
        lambda **kwargs: httpx.Client(transport=transport, follow_redirects=True),
    )


class TestHeaders:
    def test_default_user_agent_looks_like_a_browser(self):
        """A custom User-Agent gets a 403 from the DoD WAF on every URL."""
        agent = fetch.default_headers()["User-Agent"]
        assert agent.startswith("Mozilla/5.0")
        assert "mcp-militarypay" not in agent

    def test_sends_the_ordinary_browser_header_set(self):
        headers = fetch.default_headers()
        for header in ("Accept", "Accept-Language", "Sec-Fetch-Mode", "sec-ch-ua"):
            assert header in headers

    def test_does_not_claim_an_encoding_it_cannot_decode(self):
        """httpx sets Accept-Encoding from installed codecs; overriding breaks it."""
        assert "Accept-Encoding" not in fetch.default_headers()

    def test_user_agent_is_overridable_by_environment(self, monkeypatch):
        monkeypatch.setenv(fetch.USER_AGENT_ENV_VAR, "something-else/2.0")
        assert fetch.default_headers()["User-Agent"] == "something-else/2.0"


class TestFetchBytes:
    def test_returns_content_and_caches_it(self, monkeypatch, tmp_path):
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(200, content=b"payload")

        mock_client(monkeypatch, handler)
        monkeypatch.setattr(fetch, "cache_dir", lambda: tmp_path)

        assert fetch.fetch_bytes("https://example.test/a") == b"payload"
        assert fetch.fetch_bytes("https://example.test/a") == b"payload"
        assert len(calls) == 1  # second call served from cache

    def test_refresh_bypasses_the_cache(self, monkeypatch, tmp_path):
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(200, content=b"payload")

        mock_client(monkeypatch, handler)
        monkeypatch.setattr(fetch, "cache_dir", lambda: tmp_path)

        fetch.fetch_bytes("https://example.test/a")
        fetch.fetch_bytes("https://example.test/a", refresh=True)
        assert len(calls) == 2

    def test_403_is_retried_once_then_reported_with_the_probe_hint(
        self, monkeypatch, tmp_path
    ):
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(403, text="Access Denied")

        mock_client(monkeypatch, handler)
        monkeypatch.setattr(fetch, "cache_dir", lambda: tmp_path)

        with pytest.raises(fetch.FetchError, match="probe") as exc:
            fetch.fetch_bytes("https://example.test/a", retries=3)
        assert "403" in str(exc.value)
        assert len(calls) == 2  # one retry, not an endless loop

    def test_403_that_clears_on_retry_succeeds(self, monkeypatch, tmp_path):
        responses = [httpx.Response(403, text="denied"), httpx.Response(200, content=b"ok")]
        mock_client(monkeypatch, lambda request: responses.pop(0))
        monkeypatch.setattr(fetch, "cache_dir", lambda: tmp_path)

        assert fetch.fetch_bytes("https://example.test/a", retries=3) == b"ok"

    def test_404_fails_fast_without_retrying(self, monkeypatch, tmp_path):
        """For these sources a 404 means the URL pattern is wrong."""
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(404)

        mock_client(monkeypatch, handler)
        monkeypatch.setattr(fetch, "cache_dir", lambda: tmp_path)

        with pytest.raises(fetch.FetchError, match="404"):
            fetch.fetch_bytes("https://example.test/missing", retries=4)
        assert len(calls) == 1

    def test_500_is_retried_then_gives_up(self, monkeypatch, tmp_path):
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(503)

        mock_client(monkeypatch, handler)
        monkeypatch.setattr(fetch, "cache_dir", lambda: tmp_path)

        with pytest.raises(fetch.FetchError):
            fetch.fetch_bytes("https://example.test/a", retries=3)
        assert len(calls) == 3

    def test_nothing_is_cached_on_failure(self, monkeypatch, tmp_path):
        mock_client(monkeypatch, lambda request: httpx.Response(404))
        monkeypatch.setattr(fetch, "cache_dir", lambda: tmp_path)

        with pytest.raises(fetch.FetchError):
            fetch.fetch_bytes("https://example.test/a")
        assert list(tmp_path.glob("*")) == []


class TestProbe:
    def test_profiles_cover_the_403_hypotheses(self):
        assert set(fetch.PROBE_PROFILES) == {
            "project-ua", "httpx-default", "browser", "browser-http2",
        }
        for config in fetch.PROBE_PROFILES.values():
            assert config["description"]

    def test_probe_reports_status_rather_than_raising(self, monkeypatch):
        real_client = httpx.Client  # capture before patching, or this recurses
        transport = httpx.MockTransport(
            lambda request: httpx.Response(403, text="Access Denied Reference #1")
        )
        monkeypatch.setattr(
            httpx, "Client", lambda **kwargs: real_client(transport=transport)
        )
        result = fetch.probe_url("https://example.test/a", "browser")
        assert result["status"] == 403
        assert "access denied" in result["waf_markers"]
        assert "reference #" in result["waf_markers"]

    def test_probe_reports_transport_errors_rather_than_raising(self, monkeypatch):
        def boom(**kwargs):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(httpx, "Client", boom)
        result = fetch.probe_url("https://example.test/a", "browser")
        assert result["status"] is None
        assert "ConnectError" in result["error"]
