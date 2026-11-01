"""Probe the official THS ETF snapshot with a locally supplied API key.

This deliberately does not persist or publish the result. Credentials are
read only from the environment and are never included in diagnostic output.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import sys

import requests

URL = "https://fuyao.aicubes.cn/api/fund/market/snapshot"
MAX_RESPONSE = 256_000


def main() -> int:
    parser = argparse.ArgumentParser(description="核验同花顺单只ETF快照权限，不保存行情")
    parser.add_argument("--symbol", default="510300.SH", help="带交易所后缀的ETF代码")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ)", args.symbol):
        parser.error("代码须为6位数字加 .SH 或 .SZ")
    key = os.environ.get("HITHINK_FINANCE_API_KEY", "").strip()
    if not key:
        print(json.dumps({"status": "MISSING_API_KEY", "keySource": "HITHINK_FINANCE_API_KEY"}))
        return 2
    session = requests.Session()
    session.trust_env = False
    response = None
    try:
        response = session.get(URL, params={"thscode": args.symbol}, headers={"X-api-key": key},
                               timeout=(5, 10), allow_redirects=False, stream=True)
        if response.status_code != 200:
            print(json.dumps({"status": "HTTP_ERROR", "httpStatus": response.status_code}))
            return 2
        body = bytearray()
        for chunk in response.iter_content(16384):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE:
                raise ValueError("response too large")
        payload = json.loads(body)
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise ValueError("invalid envelope")
        if payload["code"] != 0:
            print(json.dumps({"status": "PROVIDER_ERROR", "providerCode": payload["code"]}))
            return 2
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("item"), list):
            raise ValueError("invalid data")
        rows = data["item"]
        if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("thscode") != args.symbol:
            raise ValueError("unexpected instrument")
        print(json.dumps({"status": "UNVERIFIED", "symbol": args.symbol, "count": len(rows),
                          "checkedAt": datetime.now(timezone.utc).isoformat(),
                          "responseTimestamp": data.get("timestamp"),
                          "hasLastPrice": rows[0].get("last_price") is not None}))
        return 0
    except (requests.RequestException, OSError, ValueError, TypeError):
        print(json.dumps({"status": "REQUEST_OR_RESPONSE_ERROR"}))
        return 2
    finally:
        if response is not None:
            response.close()
        session.close()


if __name__ == "__main__":
    sys.exit(main())
