"""Bounded, source-isolated observations from an authorized THS APP session."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import requests

URL = "https://dataq.10jqka.com.cn/fetch-data-server/fetch/v1/specific_data"
_HEADER = b"MarketListener-THS-APP-v1\x00"
_ENTROPY = b"MarketListener/THS-APP/ETF/v1"
_CODE = re.compile(r"(?:20|36):\d{6}\Z")
_ALLOWED = {"user-agent", "accept", "content-type", "x-auth-progid", "x-fuyao-auth",
            "x-auth-type", "x-auth-version", "x-auth-appname", "source-id", "platform",
            "origin", "referer", "x-requested-with", "cookie"}
_FIELDS = ("security_name", "last_price", "price_change_ratio_pct", "turnover")
_PAGE_SIZE = 40
_MAX_BYTES = 1_000_000


def credential_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not base:
        raise RuntimeError("本机会话需要 Windows 当前用户 DPAPI")
    return Path(base) / "MarketListener" / "secrets" / "ths-app-etf.dpapi"


def validate_session(value: object) -> dict:
    if not isinstance(value, dict) or value.get("url") != URL:
        raise ValueError("会话目标无效")
    headers, payload = value.get("headers"), value.get("payload")
    if not isinstance(headers, dict) or not isinstance(payload, dict):
        raise ValueError("会话结构无效")
    cleaned: dict[str, str] = {}
    for key, item in headers.items():
        if not isinstance(key, str) or key.lower() not in _ALLOWED or key.lower() in cleaned:
            raise ValueError("会话请求头无效")
        if not isinstance(item, str) or not item or len(item) > 8192 or "\r" in item or "\n" in item:
            raise ValueError("会话请求头无效")
        cleaned[key.lower()] = item
    if not {"cookie", "x-fuyao-auth", "x-auth-progid", "content-type"} <= cleaned.keys():
        raise ValueError("会话请求头不完整")
    if set(payload) != {"code_selectors", "indexes", "page_info", "sort"}:
        raise ValueError("会话请求结构无效")
    selectors = payload.get("code_selectors")
    if not isinstance(selectors, dict) or set(selectors) != {"include", "exclude", "intersection"}:
        raise ValueError("样本范围无效")
    include = selectors.get("include")
    if (not isinstance(include, list) or len(include) != 2 or selectors.get("exclude") != []
            or selectors.get("intersection") != [] or not all(isinstance(x, dict) for x in include)):
        raise ValueError("样本范围无效")
    code_lists = [x.get("values") for x in include if isinstance(x.get("values"), list) and x.get("values")]
    if len(code_lists) != 1 or not 1 <= len(code_lists[0]) <= 3000:
        raise ValueError("样本代码无效")
    codes = code_lists[0]
    if any(not isinstance(x, str) or not _CODE.fullmatch(x) for x in codes) or len(set(codes)) != len(codes):
        raise ValueError("样本代码无效")
    indexes = payload.get("indexes")
    if not isinstance(indexes, list) or not 4 <= len(indexes) <= 40:
        raise ValueError("字段列表无效")
    if any(not isinstance(x, dict) for x in indexes) or tuple(x.get("index_id") for x in indexes[:4]) != _FIELDS:
        raise ValueError("字段列表无效")
    if payload.get("sort") != [{"idx": 2, "type": "asc"}]:
        raise ValueError("样本排序无效")
    if payload.get("page_info") != {"page_size": _PAGE_SIZE, "page_begin": 0, "code_begin": 0}:
        raise ValueError("分页参数无效")
    if len(json.dumps(payload, ensure_ascii=False)) > 150_000:
        raise ValueError("请求超出大小限制")
    return {"url": URL, "headers": cleaned, "payload": deepcopy(payload)}


def save_session(value: object) -> None:
    import win32crypt

    data = json.dumps(validate_session(value), ensure_ascii=False).encode("utf-8")
    protected = win32crypt.CryptProtectData(data, "MarketListener THS ETF", _ENTROPY, None, None, 1)
    target = credential_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temp.write_bytes(_HEADER + protected)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def load_session() -> dict:
    import win32crypt

    raw = credential_path().read_bytes()
    if len(raw) > 200_000 or not raw.startswith(_HEADER):
        raise ValueError("本机会话无效")
    try:
        _, value = win32crypt.CryptUnprotectData(raw[len(_HEADER):], _ENTROPY, None, None, 1)
        return validate_session(json.loads(value))
    except Exception:
        raise ValueError("本机会话不可用") from None


def _number(value: object, *, nonnegative: bool = False) -> int | float | None:
    if value in (None, "", "--", "-"):
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or (nonnegative and value < 0):
        raise ValueError("数值字段无效")
    return value


def _atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_bytes(payload)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def capture(root: Path, config: dict, *, client: requests.Session | None = None,
            pause=time.sleep) -> dict:
    """Fetch one complete captured universe, without publishing partial pages."""
    config = validate_session(config)
    request = config["payload"]
    universe = next(x["values"] for x in request["code_selectors"]["include"] if x["values"])
    expected = set(universe)
    session = client or requests.Session()
    own = client is None
    if own:
        session.trust_env = False
    records: list[dict] = []
    pages: list[dict] = []
    seen: set[str] = set()
    try:
        for offset in range(0, len(universe), _PAGE_SIZE):
            if offset:
                pause(1.0)
            body = deepcopy(request)
            body["page_info"]["page_begin"] = offset
            response = session.post(URL, headers=config["headers"], json=body,
                                    timeout=(6, 15), allow_redirects=False, stream=True)
            try:
                if response.status_code in (401, 403):
                    return {"status": "AUTH_EXPIRED"}
                if response.status_code == 429:
                    return {"status": "RATE_LIMITED"}
                if response.status_code != 200:
                    return {"status": "HTTP_ERROR"}
                raw = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    raw.extend(chunk)
                    if len(raw) > _MAX_BYTES:
                        raise ValueError("响应超出大小限制")
                received = datetime.now(timezone.utc).isoformat()
                obj = json.loads(raw)
                if not isinstance(obj, dict) or obj.get("status_code") != 0 or not isinstance(obj.get("data"), dict):
                    return {"status": "PROVIDER_REJECTED"}
                data = obj["data"]
                indexes, rows = data.get("indexes"), data.get("data")
                if (data.get("total") != len(universe) or not isinstance(indexes, list)
                        or len(indexes) != len(request["indexes"]) or not isinstance(rows, list)
                        or len(rows) != min(_PAGE_SIZE, len(universe) - offset)):
                    raise ValueError("分页不完整")
                if any(not isinstance(field, dict) or field.get("index_id") != request["indexes"][i].get("index_id")
                       for i, field in enumerate(indexes)):
                    raise ValueError("字段顺序变化")
                for row in rows:
                    if not isinstance(row, dict) or row.get("code") not in expected or row["code"] in seen:
                        raise ValueError("样本代码重复或变化")
                    values = row.get("values")
                    if not isinstance(values, list) or len(values) != len(indexes):
                        raise ValueError("字段值缺失")
                    if any(not isinstance(x, dict) or x.get("idx") != i for i, x in enumerate(values)):
                        raise ValueError("字段索引变化")
                    name = values[0].get("value")
                    if not isinstance(name, str) or not name or len(name) > 120:
                        raise ValueError("名称无效")
                    seen.add(row["code"])
                    records.append({"code": row["code"], "name": name,
                                    "price": _number(values[1].get("value"), nonnegative=True),
                                    "changePct": _number(values[2].get("value")),
                                    "turnover": _number(values[3].get("value"), nonnegative=True),
                                    "observedAt": received})
                pages.append({"pageBegin": offset, "responseSha256": sha256(raw).hexdigest(),
                              "observedAt": received,
                              "fieldTimestamps": {name: str(indexes[i].get("timestamp", ""))
                                                  for i, name in enumerate(_FIELDS)}})
            finally:
                response.close()
        if seen != expected:
            raise ValueError("样本覆盖不完整")
        observation = {"schema": "ths-app-etf-observation-v1", "scope": "APP_TRACKING_INDEX_FILTER",
                       "quality": "UNVERIFIED", "count": len(records), "capturedPages": pages,
                       "rows": records, "order": "涨跌幅升序",
                       "limitations": ["固定的APP筛选样本，不代表全部ETF", "行情时间未核验", "分页采集非同一时刻"]}
        target = Path(root) / "bronze" / "ths_app_etf" / f"{uuid4().hex}.json"
        _atomic(target, json.dumps(observation, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        return {"status": "UNVERIFIED", "count": len(records), "captureEnd": pages[-1]["observedAt"]}
    except (requests.RequestException, ValueError, TypeError, KeyError, OverflowError):
        return {"status": "FAILED"}
    finally:
        if own:
            session.close()
