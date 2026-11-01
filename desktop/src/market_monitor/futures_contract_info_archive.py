"""Read previously captured DCE/CZCE contract facts from official raw files.

The DCE ``contractInfo`` response describes *individual listed contracts*.  Its
``startTradeDate`` is never evidence of a product's first trading day.  The CZCE
monthly report provides contract sizes, while the service report gives only
the *year* in which each futures product was first listed.  Neither source is
silently promoted to a complete contract text.
"""

from __future__ import annotations

import base64
from binascii import Error as Base64Error
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .futures_contract_info_czce_rules import (
    load_czce_business_rules,
    load_czce_historical_contract_pdfs,
)
from .futures_contract_info_czce_docx import load_current_rs_contract


_DCE_SOURCE = "DCE-45a85f5edebb.json"
_CZCE_MONTHLY_SOURCE = "czce_monthly_202507.json"
_CZCE_LISTING_SOURCE = "czce_service_report_2025.json"
_CODE = re.compile(r"^[A-Z]{1,3}(?:-F)?$")
_CZCE_LISTING_YEARS: dict[str, tuple[str, int]] = {
    "AP": ("苹果", 2017), "CF": ("棉花", 2004), "CJ": ("红枣", 2019),
    "CY": ("棉纱", 2017), "FG": ("玻璃", 2012), "JR": ("粳稻", 2013),
    "LR": ("晚籼稻", 2014), "MA": ("甲醇", 2011), "OI": ("菜籽油", 2007),
    "PF": ("短纤", 2020), "PK": ("花生", 2021), "PL": ("丙烯", 2025),
    "PM": ("普通小麦", 1993), "PR": ("瓶片", 2024), "PX": ("对二甲苯", 2023),
    "RI": ("早籼稻", 2009), "RM": ("菜籽粕", 2012), "RS": ("油菜籽", 2012),
    "SA": ("纯碱", 2019), "SF": ("硅铁", 2014), "SH": ("烧碱", 2023),
    "SM": ("锰硅", 2014), "SR": ("白糖", 2006), "TA": ("PTA", 2006),
    "UR": ("尿素", 2019), "WH": ("优质强筋小麦", 2003), "ZC": ("动力煤", 2013),
}


def _official_metadata(path: Path, exchange: str) -> dict[str, Any]:
    metadata = json.loads(path.read_text(encoding="utf-8"))
    url = str(metadata.get("sourceUrl") or "")
    host = (urlparse(url).hostname or "").lower()
    domain = "dce.com.cn" if exchange == "DCE" else "czce.com.cn"
    if host != domain and not host.endswith("." + domain):
        raise ValueError(f"unexpected {exchange} contract archive host")
    if not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("payloadSha256") or "")):
        raise ValueError(f"invalid {exchange} contract archive digest")
    datetime.fromisoformat(str(metadata["retrievedAt"]))
    if metadata.get("qualityStatus") not in (None, "PASS"):
        raise ValueError(f"unverified {exchange} contract archive")
    return metadata


def _verified_pdf(metadata_path: Path) -> tuple[dict[str, Any], PdfReader]:
    metadata = _official_metadata(metadata_path, "CZCE")
    pdf_path = metadata_path.with_suffix(".pdf")
    payload = pdf_path.read_bytes()
    if sha256(payload).hexdigest() != metadata["payloadSha256"]:
        raise ValueError(f"CZCE official PDF digest mismatch: {pdf_path.name}")
    return metadata, PdfReader(pdf_path)


def _decimal_text(raw: Any) -> str:
    try:
        value = Decimal(str(raw))
    except InvalidOperation as error:
        raise ValueError("invalid DCE contract parameter") from error
    if not value.is_finite() or value <= 0:
        raise ValueError("nonpositive DCE contract parameter")
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else str(value)


def _dce_code(contract_id: str, variety_order: str) -> str | None:
    contract_id = contract_id.upper()
    code = variety_order.upper()
    if not _CODE.fullmatch(code):
        return None
    if code.endswith("-F"):
        prefix = code[:-2]
        match = re.fullmatch(r"([A-Z]{1,3})(\d{4})F", contract_id)
    else:
        prefix = code
        match = re.fullmatch(r"([A-Z]{1,3})(\d{4})", contract_id)
    return code if match and match.group(1) == prefix else None


def _dce_contract_archive(data_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, str]]]]:
    path = data_root / "bronze" / "official_futures_contract_info" / _DCE_SOURCE
    metadata = _official_metadata(path, "DCE")
    payload = base64.b64decode(metadata["rawBase64"], validate=True)
    if sha256(payload).hexdigest() != metadata["payloadSha256"]:
        raise ValueError("DCE contractInfo archive digest mismatch")
    response = json.loads(payload)
    if response.get("success") is not True or response.get("code") != 200 or not isinstance(response.get("data"), list):
        raise ValueError("DCE contractInfo archive is not a successful response")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in response["data"]:
        if not isinstance(row, dict) or str(row.get("tradeType")) != "1":
            continue  # Options and non-futures instruments are outside this directory.
        code = _dce_code(str(row.get("contractId") or ""), str(row.get("varietyOrder") or ""))
        if code is not None:
            groups[code].append(row)
    observations: dict[str, dict[str, Any]] = {}
    month_contracts: dict[str, list[dict[str, str]]] = {}
    for code, rows in groups.items():
        names = {str(row.get("variety") or "").strip() for row in rows}
        multipliers = {_decimal_text(row.get("unit")) for row in rows}
        ticks = {_decimal_text(row.get("tick")) for row in rows}
        if len(names) != 1 or not next(iter(names)) or len(multipliers) != 1 or len(ticks) != 1:
            continue  # Do not pick an arbitrary conflicting official contract value.
        key = f"DCE.{code}"
        multiplier = next(iter(multipliers))
        tick = next(iter(ticks))
        observations[key] = {
            "url": metadata["sourceUrl"],
            "sha": metadata["payloadSha256"],
            "retrievedAt": metadata["retrievedAt"],
            "fields": {
                "交易品种": next(iter(names)),
                "交易代码": code,
                "合约乘数（官网接口原值）": multiplier,
                "最小变动价位（官网接口原值）": tick,
            },
            "sourceKind": "OFFICIAL_DCE_CONTRACT_INFO_API",
            "qualityStatus": "PARTIAL",
        }
        listings: list[dict[str, str]] = []
        for row in rows:
            try:
                listed = datetime.strptime(str(row["startTradeDate"]), "%Y%m%d").date().isoformat()
                last_trade = datetime.strptime(str(row["endTradeDate"]), "%Y%m%d").date().isoformat()
                last_delivery = datetime.strptime(str(row["endDeliveryDate"]), "%Y%m%d").date().isoformat()
            except (KeyError, ValueError):
                continue
            listings.append({
                "contractCode": str(row["contractId"]).upper(),
                "listedOn": listed,
                "lastTradingDay": last_trade,
                "lastDeliveryDay": last_delivery,
                "sourceUrl": metadata["sourceUrl"],
                "payloadSha256": metadata["payloadSha256"],
                "retrievedAt": metadata["retrievedAt"],
            })
        month_contracts[key] = sorted(listings, key=lambda item: item["contractCode"])
    return observations, month_contracts


def _czce_contract_sizes(data_root: Path) -> dict[str, dict[str, Any]]:
    path = data_root / "bronze" / "official_multiplier_history_czce" / _CZCE_MONTHLY_SOURCE
    metadata, pdf = _verified_pdf(path)
    text = "".join((pdf.pages[page].extract_text() or "") for page in range(min(4, len(pdf.pages))))
    match = re.search(r"合约大小\s*[：:]\s*(.+?)\s*含期转现", text, re.S)
    if match is None:
        raise ValueError("CZCE official monthly PDF has no contract-size note")
    sizes: dict[str, int] = {}
    for segment, size_text in re.findall(r"(?:^|[；;])([^；;]+?)(\d+)\s*吨\s*/\s*手", match.group(1), re.S):
        size = int(size_text)
        for code in re.findall(r"[A-Z]{1,3}", segment):
            if code not in _CZCE_LISTING_YEARS:
                continue
            if code in sizes and sizes[code] != size:
                raise ValueError(f"conflicting CZCE contract size for {code}")
            sizes[code] = size
    if set(sizes) != set(_CZCE_LISTING_YEARS):
        raise ValueError("CZCE monthly PDF contract-size coverage does not match the futures directory")
    return {
        f"CZCE.{code}": {
            "url": metadata["sourceUrl"],
            "sha": metadata["payloadSha256"],
            "retrievedAt": metadata["retrievedAt"],
            "fields": {"交易代码": code, "交易单位": f"{size}吨/手"},
            "sourceKind": "OFFICIAL_CZCE_MONTHLY_CONTRACT_SIZE",
            "qualityStatus": "PARTIAL",
        }
        for code, size in sizes.items()
    }


def _czce_year_listings(data_root: Path) -> dict[str, dict[str, str]]:
    path = data_root / "bronze" / "official_multiplier_history_czce" / _CZCE_LISTING_SOURCE
    metadata, pdf = _verified_pdf(path)
    if len(pdf.pages) < 2:
        raise ValueError("CZCE official service report has no listing table")
    text = re.sub(r"\s+", "", pdf.pages[1].extract_text() or "")
    start = text.find("期货品种及上市时间")
    end = text.find("资料来源：郑商所", start)
    if start < 0 or end <= start:
        raise ValueError("CZCE official service report listing table not found")
    table = text[start:end]
    listings: dict[str, dict[str, str]] = {}
    for code, (name, year) in _CZCE_LISTING_YEARS.items():
        if f"{name}（{year}）" not in table:
            continue
        listings[f"CZCE.{code}"] = {
            "day": str(year),
            "precision": "YEAR",
            "url": metadata["sourceUrl"],
            "sha": metadata["payloadSha256"],
            "retrievedAt": metadata["retrievedAt"],
            "note": "郑商所官方白皮书只注明品种上市年份，未给出月、日。",
        }
    return listings


def load_archived_contract_information(data_root: Path) -> dict[str, Any]:
    """Return source-audited DCE/CZCE terms and distinct listing facts.

    ``observations`` uses the same ``url``/``sha``/``retrievedAt``/``fields``
    keys as the collector's fallback records.  Its ``PARTIAL`` status means the
    source is not a full exchange contract text.  ``productListings`` contains
    only CZCE year-precision facts; ``monthContractListings`` contains DCE
    concrete-contract dates and must never populate product listing dates.
    """
    root = Path(data_root)
    observations: dict[str, dict[str, Any]] = {}
    month_contracts: dict[str, list[dict[str, str]]] = {}
    product_listings: dict[str, dict[str, str]] = {}
    errors: dict[str, str] = {}
    try:
        dce, month_contracts = _dce_contract_archive(root)
        observations.update(dce)
    except (OSError, ValueError, KeyError, TypeError, Base64Error) as error:
        errors["DCE"] = str(error)[:220]
    try:
        observations.update(_czce_contract_sizes(root))
    except (OSError, ValueError, KeyError, TypeError, PdfReadError) as error:
        errors["CZCE_MONTHLY"] = str(error)[:220]
    observations.update(load_czce_historical_contract_pdfs(root))
    observations.update(load_czce_business_rules(root))
    current_rs = load_current_rs_contract(root)
    if current_rs is not None:
        observations["CZCE.RS"] = current_rs
    try:
        product_listings = _czce_year_listings(root)
    except (OSError, ValueError, KeyError, TypeError, PdfReadError) as error:
        errors["CZCE_LISTINGS"] = str(error)[:220]
    return {
        "observations": observations,
        "productListings": product_listings,
        "monthContractListings": month_contracts,
        "errors": errors,
    }


__all__ = ("load_archived_contract_information",)
