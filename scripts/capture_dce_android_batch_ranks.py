"""Capture DCE daily ranking ZIPs from the already open LDPlayer browser.

This is a Bronze-only collector. It uses the browser's own page context and
intercepts the response with CDP because Android Download Manager does not
persist this endpoint's ZIP. Browser session data never enter the manifest.
"""

from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

import websocket

from market_monitor.czce_html_rank_backfill import _read_json, _write_bytes, _write_json
from market_monitor.dce_batch_zip import DceBatchZipResult, parse_dce_batch_zip
from market_monitor.futures_calendar import load_futures_trading_calendar
from market_monitor.futures_official_backfill import _process_file_lock


PAGE = "http://www.dce.com.cn/dce/channel/list/176.html"
BATCH_PATH = "/dcereport/publicweb/dailystat/memberDealPosi/batchDownload"
DAILY_PATH = "/dcereport/publicweb/dailystat/dayQuotes"
SCHEMA = "dce-android-batch-capture/v1"
ADAPTER = "official-dce-android-browser-batch-raw-v1"
BRONZE = Path("bronze") / "dce_dom_capture"
STATE = Path("state") / "dce_android_batch_capture"
_SOCKET = re.compile(r"\b(chrome_devtools_remote|webview_devtools_remote_\d+)\b")
_MAX_BODY_CHARS = 12 * 1024 * 1024
_NETWORK_ERROR = re.compile(r"net::ERR_[A-Z0-9_]{1,80}\Z")


class CaptureError(RuntimeError):
    """A bounded, sanitized browser or exchange capture failure."""

    def __init__(self, code: str, *, status: int | None = None,
                 network_diagnostic: dict[str, str | int] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.network_diagnostic = network_diagnostic


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _batch_network_diagnostic(events: list[dict[str, Any]]) -> dict[str, str | int]:
    """Summarize only CDP event names, a fixed path, status, and safe error code."""

    diagnostic: dict[str, str | int] = {"path": BATCH_PATH, "event": "REQUEST_NOT_OBSERVED"}
    request_ids: set[str] = set()
    for event in events:
        if event.get("method") != "Network.requestWillBeSent":
            continue
        params = event.get("params") or {}
        request = params.get("request") or {}
        try:
            url = urlsplit(str(request.get("url") or ""))
        except ValueError:
            continue
        if (url.scheme not in {"http", "https"} or url.hostname != "www.dce.com.cn"
                or url.path != BATCH_PATH or request.get("method") != "POST"):
            continue
        request_id = params.get("requestId")
        if isinstance(request_id, str):
            request_ids.add(request_id)
            diagnostic["event"] = "REQUEST_OBSERVED"
    for event in events:
        params = event.get("params") or {}
        if params.get("requestId") not in request_ids:
            continue
        if event.get("method") == "Network.responseReceived":
            status = (params.get("response") or {}).get("status")
            if type(status) is int and 100 <= status <= 599:
                diagnostic["httpStatus"] = status
            diagnostic["event"] = "RESPONSE_RECEIVED"
        elif event.get("method") == "Network.loadingFailed":
            error = str(params.get("errorText") or "")
            diagnostic["event"] = "LOADING_FAILED"
            diagnostic["networkError"] = error if _NETWORK_ERROR.fullmatch(error) else "NETWORK_ERROR"
    return diagnostic


def _batch_wait_error(events: list[dict[str, Any]]) -> CaptureError:
    diagnostic = _batch_network_diagnostic(events)
    event = diagnostic["event"]
    if event == "REQUEST_NOT_OBSERVED":
        code = "DCE_BATCH_REQUEST_NOT_OBSERVED"
    elif event == "LOADING_FAILED":
        code = "DCE_BATCH_NETWORK_FAILED"
    elif event == "RESPONSE_RECEIVED":
        code = "DCE_BATCH_RESPONSE_NOT_PAUSED"
    else:
        code = "DCE_BATCH_RESPONSE_TIMEOUT"
    status = diagnostic.get("httpStatus")
    return CaptureError(code, status=status if type(status) is int else None,
                        network_diagnostic=diagnostic)


def _adb_command(adb: Path, port: int, *args: str, timeout: int = 15) -> str:
    try:
        result = subprocess.run(
            [str(adb), "-P", str(port), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CaptureError("ADB_UNAVAILABLE") from error
    if result.returncode:
        raise CaptureError("ADB_COMMAND_FAILED")
    return result.stdout.strip()


def _target_for_port(port: int) -> dict[str, Any] | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as response:
            pages = json.load(response)
    except (OSError, ValueError):
        return None
    if not isinstance(pages, list):
        return None
    for page in pages:
        if not isinstance(page, dict) or page.get("type") != "page" or page.get("url") != PAGE:
            continue
        ws_url = str(page.get("webSocketDebuggerUrl") or "")
        parts = urlsplit(ws_url)
        if parts.scheme == "ws" and parts.hostname in {"127.0.0.1", "localhost"} and parts.port == port:
            return page
    return None


def _discover_page(adb: Path, adb_port: int, device: str) -> tuple[int, dict[str, Any]]:
    _adb_command(adb, adb_port, "connect", device)
    devices = _adb_command(adb, adb_port, "devices")
    if not any(line.split()[:2] == [device, "device"] for line in devices.splitlines()):
        raise CaptureError("ANDROID_DEVICE_NOT_READY")
    sockets_text = _adb_command(adb, adb_port, "-s", device, "shell", "cat", "/proc/net/unix")
    sockets = sorted(set(_SOCKET.findall(sockets_text)))
    if not sockets:
        raise CaptureError("ANDROID_BROWSER_CDP_NOT_FOUND")
    for socket in sockets:
        output = _adb_command(adb, adb_port, "-s", device, "forward", "tcp:0", f"localabstract:{socket}")
        try:
            port = int(output)
        except ValueError as error:
            raise CaptureError("ADB_FORWARD_INVALID") from error
        target = _target_for_port(port)
        if target is not None:
            return port, target
        _remove_forward(adb, adb_port, device, port)
    raise CaptureError("DCE_PAGE_NOT_OPEN_IN_ANDROID_BROWSER")


def _remove_forward(adb: Path, adb_port: int, device: str, port: int) -> None:
    try:
        _adb_command(adb, adb_port, "-s", device, "forward", "--remove", f"tcp:{port}")
    except CaptureError:
        pass


class BrowserPage:
    def __init__(self, target: dict[str, Any]) -> None:
        try:
            self.sock = websocket.create_connection(
                str(target["webSocketDebuggerUrl"]), timeout=40, suppress_origin=True,
            )
        except (KeyError, OSError, websocket.WebSocketException) as error:
            raise CaptureError("ANDROID_BROWSER_CDP_CONNECT_FAILED") from error
        self.sequence = 0
        self.events: list[dict[str, Any]] = []

    def close(self) -> None:
        self.sock.close()

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sequence += 1
        identifier = self.sequence
        try:
            self.sock.send(json.dumps({"id": identifier, "method": method, "params": params or {}}))
            while True:
                event = json.loads(self.sock.recv())
                if event.get("id") == identifier:
                    if "error" in event:
                        raise CaptureError("ANDROID_BROWSER_CDP_COMMAND_FAILED")
                    return event.get("result", {})
                if event.get("method"):
                    self.events.append(event)
        except (OSError, ValueError, websocket.WebSocketException) as error:
            raise CaptureError("ANDROID_BROWSER_CDP_IO_FAILED") from error

    def evaluate(self, expression: str) -> Any:
        result = self.command("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if "exceptionDetails" in result:
            raise CaptureError("ANDROID_BROWSER_PAGE_EVALUATION_FAILED")
        return result.get("result", {}).get("value")

    def preflight(self) -> dict[str, Any]:
        evidence = self.evaluate("""(()=>{
          const frame=[...document.querySelectorAll('iframe')]
            .find(x=>x.src.includes('/frontend/dcereport/'));
          const doc=frame?.contentDocument;
          const buttons=[...(doc?.querySelectorAll('span')||[])]
            .filter(x=>x.textContent.trim()==='批量下载' && x.getClientRects().length);
          const dates=[...(doc?.querySelectorAll('input')||[])]
            .filter(x=>/^20\\d{2}-\\d{2}-\\d{2}$/.test(x.value)).map(x=>x.value);
          const report=(doc?.body?.innerText||'').match(/查询日期：\\s*(20\\d{6})/);
          return {url:location.href.split(/[?#]/)[0],frame:!!doc,buttons:buttons.length,
            dates,reportDay:report?.[1]||null,
            heading:(doc?.body?.innerText||'').includes('日成交持仓排名')};
        })()""")
        if not isinstance(evidence, dict) or evidence.get("url") != PAGE:
            raise CaptureError("DCE_BROWSER_PAGE_MISMATCH")
        dates = evidence.get("dates")
        report_day = evidence.get("reportDay")
        if (evidence.get("frame") is not True or evidence.get("buttons") != 1
                or evidence.get("heading") is not True or not isinstance(dates, list)
                or len(dates) != 1 or dates[0].replace("-", "") != report_day):
            raise CaptureError("DCE_RANK_PAGE_NOT_READY")
        return {"page": PAGE, "displayedDay": dates[0], "batchButtons": 1}

    def _next_paused(self, deadline: float) -> dict[str, Any]:
        old_timeout = self.sock.gettimeout()
        try:
            while time.monotonic() < deadline:
                for index, event in enumerate(self.events):
                    if event.get("method") == "Fetch.requestPaused":
                        return self.events.pop(index)["params"]
                diagnostic = _batch_network_diagnostic(self.events)
                if diagnostic["event"] == "LOADING_FAILED":
                    raise _batch_wait_error(self.events)
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.sock.settimeout(min(1.0, remaining))
                    event = json.loads(self.sock.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                except (OSError, ValueError, websocket.WebSocketException) as error:
                    raise CaptureError("ANDROID_BROWSER_CDP_IO_FAILED") from error
                if event.get("method"):
                    self.events.append(event)
            raise _batch_wait_error(self.events)
        finally:
            self.sock.settimeout(old_timeout)

    def capture_day(self, compact_day: str) -> tuple[bytes, str | None]:
        self.command("Network.enable")
        self.command("Fetch.enable", {"patterns": [
            {"urlPattern": "*memberDealPosi/batchDownload*", "requestStage": "Response"},
        ]})
        try:
            self.events.clear()
            payload = {"tradeDate": compact_day, "varietyId": "a", "contractId": "a2611",
                       "tradeType": "1", "lang": "zh"}
            expression = """(()=>{const payload=""" + json.dumps(payload) + """;
              window.__dceBatchCapture=fetch('""" + BATCH_PATH + """',
                {method:'POST',headers:{'Content-Type':'application/json'},
                 credentials:'include',body:JSON.stringify(payload)})
                .then(r=>({status:r.status})).catch(()=>({error:'fetch'}));
              return true;})()"""
            if self.evaluate(expression) is not True:
                raise CaptureError("DCE_BATCH_REQUEST_NOT_STARTED")
            paused = self._next_paused(time.monotonic() + 45)
            request_id = paused.get("requestId")
            try:
                request = paused.get("request", {})
                url = urlsplit(str(request.get("url") or ""))
                if (url.scheme != "http" or url.hostname != "www.dce.com.cn"
                        or url.path != BATCH_PATH or request.get("method") != "POST"):
                    raise CaptureError("DCE_BATCH_REQUEST_MISMATCH")
                post_data = request.get("postData")
                if not post_data:
                    network_id = paused.get("networkId")
                    observed = next((event for event in self.events
                                     if event.get("method") == "Network.requestWillBeSent"
                                     and event.get("params", {}).get("requestId") == network_id), None)
                    post_data = (observed or {}).get("params", {}).get("request", {}).get("postData")
                    if not post_data and network_id:
                        post_data = self.command("Network.getRequestPostData", {"requestId": network_id}).get("postData")
                try:
                    submitted = json.loads(post_data or "")
                except ValueError as error:
                    raise CaptureError("DCE_BATCH_POST_BODY_MISSING") from error
                if submitted != payload:
                    raise CaptureError("DCE_BATCH_POST_BODY_MISMATCH")
                status = paused.get("responseStatusCode")
                if status != 200:
                    raise CaptureError("DCE_BATCH_HTTP_FAILED", status=status if isinstance(status, int) else None)
                headers = paused.get("responseHeaders") or []
                content_type = next((str(item.get("value")) for item in headers
                                     if str(item.get("name", "")).lower() == "content-type"), None)
                if content_type is None or "zip" not in content_type.lower():
                    raise CaptureError("DCE_BATCH_NOT_ZIP")
                result = self.command("Fetch.getResponseBody", {"requestId": request_id})
                encoded = result.get("body")
                if not isinstance(encoded, str) or not result.get("base64Encoded") or len(encoded) > _MAX_BODY_CHARS:
                    raise CaptureError("DCE_BATCH_BODY_INVALID")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError as error:
                    raise CaptureError("DCE_BATCH_BODY_INVALID") from error
                if not raw.startswith(b"PK"):
                    raise CaptureError("DCE_BATCH_NOT_ZIP")
                return raw, content_type
            finally:
                if request_id:
                    try:
                        self.command("Fetch.continueResponse", {"requestId": request_id})
                    except CaptureError:
                        self.command("Fetch.continueRequest", {"requestId": request_id})
        finally:
            self.command("Fetch.disable")
            self.command("Network.disable")
            self.events.clear()

    def capture_daily_quotes(self, compact_day: str) -> tuple[bytes, str]:
        """Read a dated official dayQuotes JSON from this verified page session."""

        if re.fullmatch(r"20\d{6}", compact_day) is None:
            raise ValueError("DCE daily trading day must be YYYYMMDD")
        payload = {
            "contractId": "", "lang": "zh", "optionSeries": "",
            "statisticsType": "0", "tradeDate": compact_day,
            "tradeType": "1", "varietyId": "all",
        }
        expression = """(async()=>{const payload=""" + json.dumps(payload) + """;
          const response=await fetch('""" + DAILY_PATH + """',
            {method:'POST',headers:{'Content-Type':'application/json'},
             credentials:'include',body:JSON.stringify(payload)});
          return {status:response.status,contentType:response.headers.get('content-type'),
            body:await response.text()};})()"""
        result = self.command("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
        })
        if "exceptionDetails" in result:
            raise CaptureError("DCE_DAILY_PAGE_EVALUATION_FAILED")
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise CaptureError("DCE_DAILY_RESPONSE_INVALID")
        status = value.get("status")
        if status != 200:
            raise CaptureError("DCE_DAILY_HTTP_FAILED", status=status if isinstance(status, int) else None)
        content_type = str(value.get("contentType") or "")
        body = value.get("body")
        if "html" in content_type.lower() or not isinstance(body, str) or len(body) > _MAX_BODY_CHARS:
            raise CaptureError("DCE_DAILY_NOT_JSON")
        raw = body.encode("utf-8")
        try:
            decoded = json.loads(raw.decode("utf-8-sig"))
        except ValueError as error:
            raise CaptureError("DCE_DAILY_BODY_INVALID") from error
        if (not isinstance(decoded, dict) or decoded.get("code") != 200
                or decoded.get("success") is not True or not isinstance(decoded.get("data"), list)):
            raise CaptureError("DCE_DAILY_BUSINESS_FAILED")
        return raw, content_type


def _existing(root: Path, day: str) -> dict[str, Any] | None:
    compact = day.replace("-", "")
    path = root / BRONZE / f"{compact}.zip"
    manifest = _read_json(root / BRONZE / f"{compact}.json")
    if (not path.is_file() or manifest is None or manifest.get("status") != "PASS_RAW"
            or manifest.get("tradeDate") != day or manifest.get("source") != PAGE
            or manifest.get("batchPath") != BATCH_PATH or manifest.get("httpStatus") != 200
            or manifest.get("authoritativeDataWritten") is not False):
        return None
    try:
        parsed = parse_dce_batch_zip(path.read_bytes(), trading_day=compact)
    except (OSError, ValueError):
        return None
    digest = manifest.get("payloadSha256") or manifest.get("sha256")
    if digest != parsed.archive_sha256 or manifest.get("bytes") != path.stat().st_size:
        return None
    return parsed.summary


def _store(root: Path, day: str, raw: bytes, content_type: str | None,
           parsed: DceBatchZipResult) -> dict[str, Any]:
    compact = day.replace("-", "")
    path = root / BRONZE / f"{compact}.zip"
    manifest = {"schema": SCHEMA, "publisher": "DCE", "adapter": ADAPTER,
                "source": PAGE, "batchPath": BATCH_PATH, "tradeDate": day,
                "method": "LDPlayer page fetch + CDP Fetch", "httpStatus": 200,
                "contentType": content_type, "bytes": len(raw),
                "payloadSha256": parsed.archive_sha256, "summary": parsed.summary,
                "capturedAtUtc": _utc(), "status": "PASS_RAW", "qualityStatus": "PASS_RAW",
                "authoritativeDataWritten": False}
    _write_bytes(path, raw)
    _write_json(path.with_suffix(".json"), manifest)
    return manifest


def capture(root: Path, *, start_day: str, end_day: str, max_days: int | None,
            min_interval_seconds: float, adb: Path, adb_port: int, device: str,
            dry_run: bool = False) -> dict[str, Any]:
    if date.fromisoformat(start_day) > date.fromisoformat(end_day):
        raise ValueError("start_day must not exceed end_day")
    if max_days is not None and max_days < 1:
        raise ValueError("max_days must be positive")
    if min_interval_seconds < 2 or adb_port < 1024 or adb_port > 65535:
        raise ValueError("minimum interval is two seconds and adb port must be valid")
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified futures trading calendar is missing")
    days = calendar.between(start_day, end_day)
    if not days:
        raise ValueError("verified futures trading calendar has no days in range")
    if dry_run:
        existing = sum(_existing(root, day) is not None for day in days)
        return {"status": "DRY_RUN", "calendarDays": len(days), "verifiedExistingDays": existing,
                "pendingDays": len(days) - existing, "maxDays": max_days,
                "authoritativeDataWritten": False}

    state = root / STATE
    progress = {"schema": SCHEMA, "publisher": "DCE", "adapter": ADAPTER,
                "status": "RUNNING", "startDay": start_day, "endDay": end_day,
                "calendarDays": len(days), "completedDays": 0, "newDaysThisRun": 0,
                "authoritativeDataWritten": False, "updatedAt": _utc()}
    with _process_file_lock(state / "capture.lock"):
        try:
            pending = [day for day in days if _existing(root, day) is None]
            progress["completedDays"] = len(days) - len(pending)
            _write_json(state / "progress.json", progress)
            if not pending:
                progress["status"] = "PASS_RAW"
            elif not adb.is_file():
                raise CaptureError("ADB_UNAVAILABLE")
            else:
                port, target = _discover_page(adb, adb_port, device)
                try:
                    page = BrowserPage(target)
                    try:
                        progress["browserPreflight"] = page.preflight()
                        _write_json(state / "progress.json", progress)
                        last_request_at: float | None = None
                        for day in pending:
                            if max_days is not None and progress["newDaysThisRun"] >= max_days:
                                progress["status"] = "BATCH_LIMIT"
                                break
                            progress.update(currentDay=day, updatedAt=_utc())
                            _write_json(state / "progress.json", progress)
                            if last_request_at is not None:
                                time.sleep(max(0.0, min_interval_seconds - (time.monotonic() - last_request_at)))
                            last_request_at = time.monotonic()
                            raw, content_type = page.capture_day(day.replace("-", ""))
                            parsed = parse_dce_batch_zip(raw, trading_day=day.replace("-", ""))
                            manifest = _store(root, day, raw, content_type, parsed)
                            _write_json(state / f"{day}.json", manifest)
                            progress["newDaysThisRun"] += 1
                            progress["completedDays"] += 1
                            _write_json(state / "progress.json", progress)
                            print(json.dumps({"day": day, "status": "PASS_RAW",
                                              "entries": parsed.entry_count,
                                              "rankedContracts": len(parsed.frames)}, ensure_ascii=False), flush=True)
                        else:
                            progress["status"] = "PASS_RAW"
                    finally:
                        page.close()
                finally:
                    _remove_forward(adb, adb_port, device, port)
        except (CaptureError, ValueError, OSError) as error:
            progress.update(status="FAILED", errorDay=progress.get("currentDay"),
                            errorType=type(error).__name__,
                            errorCode=error.code if isinstance(error, CaptureError) else "VALIDATION_OR_IO_FAILED")
            if isinstance(error, CaptureError) and error.status is not None:
                progress["httpStatus"] = error.status
            if isinstance(error, CaptureError) and error.network_diagnostic is not None:
                progress["networkDiagnostic"] = error.network_diagnostic
        finally:
            progress["updatedAt"] = _utc()
            _write_json(state / "progress.json", progress)
    return progress


def main() -> int:
    parser = argparse.ArgumentParser(description="大商所Android浏览器每日排名ZIP原件断点采集")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2025-01-01")
    parser.add_argument("--end-day", default="2026-09-30")
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--min-interval-seconds", type=float, default=2.0)
    parser.add_argument("--adb", type=Path, default=Path(r"C:\leidian\LDPlayer14\adb.exe")
                        if Path(r"C:\leidian\LDPlayer14\adb.exe").is_file()
                        else Path(shutil.which("adb") or "adb"))
    parser.add_argument("--adb-port", type=int, default=5038)
    parser.add_argument("--device", default="127.0.0.1:5555")
    parser.add_argument("--dry-run", action="store_true", help="只核对日历和已有原件，不请求官网")
    args = parser.parse_args()
    result = capture(args.data_root, start_day=args.start_day, end_day=args.end_day,
                     max_days=args.max_days, min_interval_seconds=args.min_interval_seconds,
                     adb=args.adb, adb_port=args.adb_port, device=args.device, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result["status"] in {"DRY_RUN", "PASS_RAW", "BATCH_LIMIT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
