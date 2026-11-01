"""Sanitized, deterministic diagnostics for the DCE browser ZIP capture."""

from __future__ import annotations

import json
from pathlib import Path
import time

import pytest


@pytest.fixture
def capture_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import capture_dce_android_batch_ranks

    return capture_dce_android_batch_ranks


def _request(path: str, *, request_id: str = "batch-1") -> dict:
    return {"method": "Network.requestWillBeSent", "params": {
        "requestId": request_id,
        "request": {"method": "POST",
                    "url": f"http://www.dce.com.cn{path}?token=synthetic-secret",
                    "headers": {"Cookie": "synthetic-secret"},
                    "postData": "synthetic-secret"},
    }}


@pytest.mark.parametrize(
    ("additional", "expected_code", "event", "status", "network_error"),
    [
        ([], "DCE_BATCH_RESPONSE_TIMEOUT", "REQUEST_OBSERVED", None, None),
        ([{"method": "Network.loadingFailed", "params": {
            "requestId": "batch-1", "errorText": "net::ERR_TIMED_OUT"}}],
         "DCE_BATCH_NETWORK_FAILED", "LOADING_FAILED", None, "net::ERR_TIMED_OUT"),
        ([{"method": "Network.responseReceived", "params": {
            "requestId": "batch-1", "response": {
                "status": 412, "headers": {"Set-Cookie": "synthetic-secret"}}}}],
         "DCE_BATCH_RESPONSE_NOT_PAUSED", "RESPONSE_RECEIVED", 412, None),
    ],
)
def test_batch_wait_reports_network_stage_without_sensitive_data(
    capture_module, additional: list[dict], expected_code: str, event: str,
    status: int | None, network_error: str | None,
) -> None:
    error = capture_module._batch_wait_error([
        _request(capture_module.BATCH_PATH), *additional,
    ])
    assert error.code == expected_code
    assert error.status == status
    assert error.network_diagnostic == {
        "path": capture_module.BATCH_PATH,
        "event": event,
        **({"httpStatus": status} if status is not None else {}),
        **({"networkError": network_error} if network_error is not None else {}),
    }
    assert "synthetic-secret" not in json.dumps(error.network_diagnostic)


def test_batch_wait_distinguishes_no_request_and_sanitizes_unknown_failure(capture_module) -> None:
    unseen = capture_module._batch_wait_error([
        _request("/another/path"),
    ])
    assert unseen.code == "DCE_BATCH_REQUEST_NOT_OBSERVED"
    assert unseen.network_diagnostic == {
        "path": capture_module.BATCH_PATH, "event": "REQUEST_NOT_OBSERVED",
    }

    failure = capture_module._batch_wait_error([
        _request(capture_module.BATCH_PATH),
        {"method": "Network.loadingFailed", "params": {
            "requestId": "batch-1", "errorText": "private error synthetic-secret"}},
    ])
    assert failure.network_diagnostic["networkError"] == "NETWORK_ERROR"
    assert "synthetic-secret" not in json.dumps(failure.network_diagnostic)


def test_paused_wait_uses_existing_events_without_a_live_socket(capture_module) -> None:
    class Socket:
        timeout = 40.0

        def gettimeout(self) -> float:
            return self.timeout

        def settimeout(self, value: float) -> None:
            self.timeout = value

        def recv(self) -> str:
            raise AssertionError("No live CDP socket read is needed")

    page = capture_module.BrowserPage.__new__(capture_module.BrowserPage)
    page.sock = Socket()
    page.events = [_request(capture_module.BATCH_PATH)]
    with pytest.raises(capture_module.CaptureError) as caught:
        page._next_paused(time.monotonic() - 1)
    assert caught.value.code == "DCE_BATCH_RESPONSE_TIMEOUT"
    assert page.sock.timeout == 40.0

    page.events = [_request(capture_module.BATCH_PATH), {
        "method": "Network.loadingFailed", "params": {
            "requestId": "batch-1", "errorText": "net::ERR_CONNECTION_TIMED_OUT",
        },
    }]
    with pytest.raises(capture_module.CaptureError) as caught:
        page._next_paused(time.monotonic() + 1)
    assert caught.value.code == "DCE_BATCH_NETWORK_FAILED"
    assert page.sock.timeout == 40.0

    page.events = [{"method": "Fetch.requestPaused", "params": {
        "requestId": "fetch-1", "responseStatusCode": 200,
    }}]
    assert page._next_paused(time.monotonic() + 1)["requestId"] == "fetch-1"
    assert page.sock.timeout == 40.0
