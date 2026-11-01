"""Import DCE daily quotes through the already open LDPlayer browser page."""

from __future__ import annotations

import argparse
import json
from json import dumps as json_dumps
from pathlib import Path
import shutil

import requests

from capture_dce_android_batch_ranks import (
    DAILY_PATH, BrowserPage, CaptureError, _discover_page, _remove_forward,
)
from market_monitor.futures_official_backfill import (
    HostPaused, _process_file_lock, run_official_futures_backfill,
)


class BrowserDailySession:
    """Provide the existing official importer with browser-session response bytes."""

    trust_env = False

    def __init__(self, page: BrowserPage) -> None:
        self.page = page
        self.responses_captured = 0

    def request(
        self, method: str, url: str, *, json: dict | None = None,
        data: object = None, headers: object = None, timeout: float = 30,
    ) -> requests.Response:
        if (method != "POST" or not url.endswith(DAILY_PATH) or not isinstance(json, dict)
                or data is not None or json.get("tradeType") != "1"
                or json.get("varietyId") != "all"):
            raise ValueError("DCE browser daily request differs from official specification")
        day = json.get("tradeDate")
        if not isinstance(day, str):
            raise ValueError("DCE browser daily request is missing a trading day")
        try:
            raw, content_type = self.page.capture_daily_quotes(day)
        except CaptureError as error:
            status = f" HTTP {error.status}" if error.status is not None else ""
            raise HostPaused(f"DCE browser daily capture {error.code}{status}") from error
        self.responses_captured += 1
        if self.responses_captured % 20 == 0:
            print(json_dumps({"status": "CAPTURING", "responsesCaptured": self.responses_captured}), flush=True)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = content_type
        response._content = raw
        return response


def main() -> int:
    parser = argparse.ArgumentParser(description="通过雷电浏览器采集大商所官网逐合约日行情")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", required=True)
    parser.add_argument("--end-day", required=True)
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--min-interval-seconds", type=float, default=1.0)
    parser.add_argument("--adb", type=Path, default=Path(r"C:\leidian\LDPlayer14\adb.exe")
                        if Path(r"C:\leidian\LDPlayer14\adb.exe").is_file()
                        else Path(shutil.which("adb") or "adb"))
    parser.add_argument("--adb-port", type=int, default=5038)
    parser.add_argument("--device", default="127.0.0.1:5555")
    args = parser.parse_args()
    root = args.data_root.resolve()
    port: int | None = None
    page: BrowserPage | None = None
    try:
        with _process_file_lock(root / "state" / "dce_android_batch_capture" / "capture.lock"):
            port, target = _discover_page(args.adb, args.adb_port, args.device)
            page = BrowserPage(target)
            page.preflight()
            report = run_official_futures_backfill(
                root, start_day=args.start_day, end_day=args.end_day,
                exchanges=("DCE",), max_days=args.max_days,
                min_interval_seconds=args.min_interval_seconds,
                retry_failed=True, session=BrowserDailySession(page),
            )
    except CaptureError as error:
        print(json.dumps({"status": "BLOCKED", "reason": error.code}, ensure_ascii=False))
        return 2
    finally:
        if page is not None:
            page.close()
        if port is not None:
            _remove_forward(args.adb, args.adb_port, args.device, port)
    failed = [item for item in report["outcomes"] if item["status"] not in {"PASS", "CACHED"}]
    print(json.dumps({
        "status": report["status"], "startDay": report["startDay"], "endDay": report["endDay"],
        "selectedTradingDays": report["selectedTradingDays"], "attempts": report["attempts"],
        "passed": report["passed"], "failed": len(failed),
        "firstFailure": failed[0] if failed else None,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
