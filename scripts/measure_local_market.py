"""Measure one fixed local market-list API sample with explicit cache conditions.

Run only after starting the current product service through its controlled
launcher. The report contains timings and version IDs, never session secrets.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import platform
from pathlib import Path
import statistics
import sys
import time

import requests

from market_monitor.local_session import CONTROL_HEADER, load_control_token


BASE = "http://127.0.0.1:8765"
SAMPLE = "/api/market/instruments"


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def authenticated_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    credential = load_control_token("product", 8765, create=False)
    issued = session.post(BASE + "/api/local-session/issue", data=b"{}", timeout=3,
                          headers={CONTROL_HEADER: credential})
    issued.raise_for_status()
    capability = issued.json()["bootstrap"]
    exchanged = session.post(BASE + "/api/local-session/bootstrap", timeout=3,
                             headers={"Origin": BASE}, json={"bootstrap": capability})
    exchanged.raise_for_status()
    return session


def measure(samples: int, *, condition: str, category: str) -> dict:
    elapsed: list[float] = []
    response_bytes: list[int] = []
    versions: set[str] = set()
    snapshots: set[str] = set()
    statuses: dict[str, int] = {}
    stage_examples: list[str] = []
    session = None if condition == "NEW_HTTP_SESSION" else authenticated_session()
    try:
        for _ in range(samples):
            current = authenticated_session() if session is None else session
            try:
                start = time.perf_counter()
                response = current.get(BASE + SAMPLE, params={"categoryKey": category,
                                       "page": 1, "pageSize": 200}, timeout=30)
                elapsed.append((time.perf_counter() - start) * 1000)
                response_bytes.append(len(response.content))
                statuses[str(response.status_code)] = statuses.get(str(response.status_code), 0) + 1
                if response.status_code != 200:
                    raise RuntimeError(f"行情名单请求失败：HTTP {response.status_code}")
                payload = response.json()
                versions.add(str(payload.get("dataVersion") or ""))
                snapshots.add(str(payload.get("snapshotStatus") or "UNKNOWN"))
                stage_examples.append(response.headers.get("Server-Timing", ""))
                if len(payload.get("items", [])) > 200:
                    raise RuntimeError("固定200条样本超出预期")
            finally:
                if session is None:
                    current.close()
    finally:
        if session is not None:
            session.close()
    if len(versions) != 1:
        raise RuntimeError("测量中行情发布版本变化，请在稳定版本重新测量")
    return {"measuredAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "host": platform.node(), "platform": platform.platform(),
            "condition": condition, "category": category, "sampleSize": samples,
            "endpoint": SAMPLE, "pageSize": 200, "dataVersion": next(iter(versions)),
            "snapshotStatus": sorted(snapshots),
            "latencyMs": {"p50": round(percentile(elapsed, .5), 3),
                          "p95": round(percentile(elapsed, .95), 3),
                          "minimum": round(min(elapsed), 3), "maximum": round(max(elapsed), 3)},
            "responseBytes": {"median": int(statistics.median(response_bytes)),
                              "maximum": max(response_bytes)},
            "httpStatuses": statuses, "serverTimingSamples": stage_examples[:3]}


def main() -> int:
    parser = argparse.ArgumentParser(description="测量本机已发布行情名单API")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--condition", choices=("WARM", "NEW_HTTP_SESSION", "UPDATE_BUSY"), default="WARM")
    parser.add_argument("--category", default="cn-future-dce")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 5 <= args.samples <= 100:
        parser.error("samples must be 5..100")
    try:
        report = measure(args.samples, condition=args.condition, category=args.category)
    except (RuntimeError, KeyError, ValueError, requests.RequestException) as error:
        print(f"测量未完成：{type(error).__name__}: {error}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
