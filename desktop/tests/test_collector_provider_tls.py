"""Transport assurance for the unauthenticated USD index and VIX providers."""

from __future__ import annotations

import requests
import pytest

from market_monitor import collector


class _Response:
    def __init__(self, status: int, *, text: str = "", location: str | None = None) -> None:
        self.status_code = status
        self.text = text
        self.headers = {"Location": location} if location else {}
        self.closed = False
        self.encoding = "utf-8"

    def iter_content(self, chunk_size: int = 65536):
        yield self.text.encode("utf-8")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, responses: list[_Response | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.trust_env = True

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_public_fetch_rejects_unverified_tls_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Session([requests.exceptions.SSLError("synthetic certificate failure")])
    monkeypatch.setattr(requests, "Session", lambda: fake)

    with pytest.raises(collector.ProviderTransportBlockedError, match="TLS_VERIFICATION_FAILED"):
        collector._fetch_url_text("https://cdn.cboe.com/feed")

    assert fake.trust_env is False
    assert len(fake.calls) == 1
    assert fake.calls[0][1]["verify"] is True
    assert fake.calls[0][1]["allow_redirects"] is False


def test_public_fetch_rejects_https_to_http_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    redirect = _Response(302, location="http://cdn.cboe.com/feed")
    fake = _Session([redirect])
    monkeypatch.setattr(requests, "Session", lambda: fake)

    with pytest.raises(collector.ProviderTransportBlockedError, match="HTTPS_REQUIRED"):
        collector._fetch_url_text("https://cdn.cboe.com/feed")

    assert len(fake.calls) == 1
    assert redirect.closed is True


def test_public_fetch_follows_only_verified_https_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Session([_Response(302, location="/new"), _Response(200, text="verified")])
    monkeypatch.setattr(requests, "Session", lambda: fake)

    assert collector._fetch_url_text("https://cdn.cboe.com/feed") == "verified"
    assert [url for url, _ in fake.calls] == [
        "https://cdn.cboe.com/feed",
        "https://cdn.cboe.com/new",
    ]
    assert all(options["verify"] is True for _, options in fake.calls)


def test_public_fetch_rejects_cross_host_https_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Session([_Response(302, location="https://push2.eastmoney.com/new")])
    monkeypatch.setattr(requests, "Session", lambda: fake)
    with pytest.raises(collector.ProviderTransportBlockedError, match="HTTPS_REDIRECT_HOST_BLOCKED"):
        collector._fetch_url_text("https://cdn.cboe.com/feed")


def test_public_fetch_rejects_oversize_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Session([_Response(200, text="x" * (2 * 1024 * 1024 + 1))])
    monkeypatch.setattr(requests, "Session", lambda: fake)
    with pytest.raises(collector.ProviderTransportBlockedError, match="PROVIDER_RESPONSE_TOO_LARGE"):
        collector._fetch_url_text("https://cdn.cboe.com/feed")


def test_vix_tls_block_on_primary_can_use_verified_secondary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(url: str, timeout: int = 20) -> str:
        if "cboe.com" in url:
            raise collector.ProviderTransportBlockedError("TLS_VERIFICATION_FAILED")
        return '{"data":{"usVIX":{"day":[["2026-10-09","0","18.5"]]}}}'

    monkeypatch.setattr(collector, "_fetch_url_text", fake_fetch)
    errors: list[str] = []
    assert collector._fetch_vix_points(errors) == [("2026-10-09", 18.5)]
    assert errors == ["cdn.cboe.com:ProviderTransportBlockedError"]


def test_tls_failure_blocks_series_and_never_reports_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        collector,
        "_fetch_usd_index_points",
        lambda _errors: [("2026-10-09", 100.0)],
    )
    monkeypatch.setattr(
        collector,
        "_fetch_vix_points",
        lambda _errors: (_ for _ in ()).throw(
            collector.ProviderTransportBlockedError("TLS_VERIFICATION_FAILED")
        ),
    )

    result = collector._collect_usd_vix()

    assert result.status == "PARTIAL_FAILURE"
    assert result.rows == 1
    assert "VIX:ProviderTransportBlockedError:TLS_VERIFICATION_FAILED" in (result.error or "")


def test_successful_https_fallback_remains_visible_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        collector,
        "_fetch_usd_index_points",
        lambda _errors: [("2026-10-09", 100.0)],
    )

    def fake_fetch(url: str, timeout: int = 20) -> str:
        if "cboe.com" in url:
            return "invalid CSV"
        return '{"data":{"usVIX":{"day":[["2026-10-09","0","18.5"]]}}}'

    monkeypatch.setattr(collector, "_fetch_url_text", fake_fetch)
    result = collector._collect_usd_vix()

    assert result.status == "PARTIAL_FAILURE"
    assert result.rows == 2
    assert "VIX:cdn.cboe.com:ValueError" in (result.error or "")
