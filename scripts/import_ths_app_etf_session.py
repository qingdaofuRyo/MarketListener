"""Import one user-authorized Reqable ETF session into current-user DPAPI.

Reads only the matching saved request and the business body tied to a verified
local observation. Requires the optional lmdb package on PYTHONPATH.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path

from market_monitor.ths_app_etf_live import URL, _ALLOWED, save_session, validate_session


def import_session(root: Path, reqable: Path) -> None:
    import lmdb

    snapshots = sorted((root / "bronze" / "ths_app_etf").glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not snapshots:
        raise ValueError("缺少已保存的 ETF 观察")
    snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
    if snapshot.get("schema") != "ths-app-etf-observation-v1" or snapshot.get("scope") != "APP_TRACKING_INDEX_FILTER":
        raise ValueError("ETF 观察范围无效")
    page_hash = snapshot["capturedPages"][0]["responseSha256"]
    payload = None
    for path in (reqable / "capture").glob("*res-extract-body.reqable"):
        if path.stat().st_size > 1_000_000:
            continue
        if sha256(path.read_bytes()).hexdigest() == page_hash:
            request_path = path.with_name(path.name.replace("-res-extract-body.reqable", "-req_raw-body.reqable"))
            if request_path.stat().st_size > 150_000:
                raise ValueError("业务请求过大")
            payload = json.loads(request_path.read_bytes())
            break
    if payload is None:
        raise ValueError("未找到匹配的业务请求")
    expected = {row["code"] for row in snapshot["rows"]}
    selected = next((item.get("values") for item in payload["code_selectors"]["include"] if item.get("values")), [])
    if len(expected) != snapshot["count"] or set(selected) != expected:
        raise ValueError("业务请求与观察样本不一致")
    env = lmdb.open(str(reqable / "box"), readonly=True, lock=False, max_dbs=128)
    candidates = []
    try:
        with env.begin() as transaction:
            for _, value in transaction.cursor():
                if b"specific_data" not in value:
                    continue
                try:
                    text = value.decode("utf-8", errors="ignore")
                    decoded, _ = json.JSONDecoder().raw_decode(text[text.find("{"):])
                    api = decoded["api"]
                    if api.get("method") != "POST" or api.get("url", {}).get("base") != URL:
                        continue
                    headers = {item["key"]: item["value"] for item in api["headers"]
                               if isinstance(item, dict) and isinstance(item.get("key"), str)
                               and item["key"].lower() in _ALLOWED and isinstance(item.get("value"), str)
                               and item["value"]}
                    candidates.append(validate_session({"url": URL, "headers": headers, "payload": payload}))
                except (ValueError, KeyError, TypeError):
                    continue
    finally:
        env.close()
    if len(candidates) != 1:
        raise ValueError("未找到唯一的同花顺 APP 会话")
    save_session(candidates[0])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--reqable", type=Path, default=Path(os.environ.get("APPDATA", "")) / "Reqable")
    args = parser.parse_args()
    try:
        import_session(args.data_root, args.reqable)
    except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError):
        raise SystemExit("导入未完成：请检查本机 Reqable 抓包和会话配置。") from None
    print("同花顺 ETF 会话已加密保存；未输出凭据。")


if __name__ == "__main__":
    main()
