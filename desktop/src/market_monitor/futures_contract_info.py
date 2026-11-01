"""Source-isolated collection of exchange-published futures product terms."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from bs4 import BeautifulSoup

from .futures_official_multipliers import REGISTRY_PATH
from .futures_contract_info_archive import load_archived_contract_information
from .futures_contract_info_chrome import load_chrome_contract_tables
from .futures_contract_listing_seed import verified_listing_evidence
from .futures_contract_info_ine import (
    ine_official_contract_urls,
    validate_ine_official_contract_fields,
    validate_ine_official_contract_url,
)
from .atomic_files import replace_with_retry
from .storage import MarketStore


PARSER_VERSION = "official-futures-contract-info-v4"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
_SEEDS = {
    "SHFE": "https://www.shfe.com.cn/products/futures/metal/nonferrousmetal/cu_f/",
    "INE": "https://www.ine.cn/products/futures/energyandchemical/lu_f/",
    "CFFEX": "http://www.cffex.com.cn/cn/hs300.html",
    "GFEX": "http://www.gfex.com.cn/gfex/djg/sspz.shtml",
}
_CFFEX_PAGES = {
    "IF": "hs300.html", "IC": "zz500.html", "IM": "zz1000.html",
    "IH": "sz50gzqh.html", "TS": "2ts.html", "TF": "5tf.html",
    "T": "10t.html", "TL": "30tl.html",
}
_GFEX_PAGES = {"PT": "sspzb", "PD": "sspzp", "PS": "djg", "LC": "tsl", "SI": "gyeg"}
_DIRECT_SAMPLES = {
    "DCE.Y": "http://www.dce.com.cn/dce/channel/list/2233.html",
    "CZCE.SR": "https://www.czce.com.cn/cn/sspz/bt/H077002004index_1.htm",
}
_SHFE_FIELD_NAMES = {
    "Product": "交易品种", "Underlying": "合约标的物",
    "ContractMultiplier": "合约乘数", "ContractSize": "交易单位",
    "PriceQuotation": "报价单位", "MinimumPriceFluctuation": "最小变动价位",
    "RangeofPriceLimit": "涨跌停板幅度", "ListedContracts": "合约月份",
    "ListedContracts1": "合约交割月份", "TradingHours": "交易时间",
    "LastTradingDay": "最后交易日", "DeliveryPeriod": "交割日期",
    "GradeandQualitySpecifications1": "交割品级",
    "GradesandQualitySpecifications": "交割品质",
    "DeliveryVenue": "交割地点", "MinimumTradeMargin": "最低交易保证金",
    "SettlementType": "交割方式", "DeliveryUnit": "交割单位",
    "ExpirationDate": "到期日", "ContractSymbol": "交易代码",
    "ListingExchange": "上市交易所", "ListingExchange1": "上市机构",
    "note": "原文注释",
}
_DOMAINS = {
    "SHFE": "shfe.com.cn", "INE": "ine.cn", "CFFEX": "cffex.com.cn",
    "GFEX": "gfex.com.cn", "DCE": "dce.com.cn", "CZCE": "czce.com.cn",
}
_CURATED_LISTINGS = {
    "DCE.L-F": ("2025-10-29", "DCE_MONTH_AVERAGE_LISTING_20251020"),
    "DCE.PP-F": ("2025-10-29", "DCE_MONTH_AVERAGE_LISTING_20251020"),
    "DCE.V-F": ("2025-10-29", "DCE_MONTH_AVERAGE_LISTING_20251020"),
    "DCE.LG": ("2024-11-18", "DCE_LG_LISTING_20241028"),
    "DCE.BZ": ("2025-07-08", "DCE_BZ_LISTING_20250627"),
    "DCE.EB": ("2019-09-26", "DCE_EB_UNIT_202201"),
    "CZCE.PX": ("2023-09-15", "CZCE_202309_NEW_PRODUCT_UNITS"),
    "CZCE.SH": ("2023-09-15", "CZCE_202309_NEW_PRODUCT_UNITS"),
    "CZCE.PR": ("2024-08-30", "CZCE_202408_PR_UNITS"),
    "CZCE.PL": ("2025-07-22", "CZCE_2025_SERVICE_PL_LISTING"),
}


def _fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/pdf,*/*"})
    with urlopen(request, timeout=15) as response:
        content = response.read(5_000_001)
        if len(content) > 5_000_000:
            raise ValueError("official contract source exceeds five megabytes")
        return content


def _decode(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("gb18030", errors="replace")


def _extract_js_array(html: str, name: str) -> list[dict[str, Any]]:
    marker = re.search(rf"\blet\s+{re.escape(name)}\s*=\s*\[", html)
    if marker is None:
        return []
    start = marker.end() - 1
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                raw = html[start:index + 1].replace("\xa0", " ")
                raw = re.sub(r",\s*([}\]])", r"\1", raw)
                parsed = json.loads(raw)
                return [item for item in parsed if isinstance(item, dict)]
    return []


def _parse_fields(exchange: str, html: str) -> tuple[str | None, str | None, dict[str, str]]:
    if exchange in {"SHFE", "INE"}:
        versions = _extract_js_array(html, "pageList")
        if not versions:
            return None, None, {}
        current = versions[0]
        fields = {
            label: str(current[key]).strip()
            for key, label in _SHFE_FIELD_NAMES.items()
            if current.get(key) not in (None, "")
        }
        return str(current.get("ContractSymbol") or "").upper() or None, str(current.get("data_standard") or "") or None, fields
    soup = BeautifulSoup(html, "html.parser")
    if exchange == "GFEX":
        tables = soup.select("table.table_cphy")
        table = tables[0] if tables else None
    elif exchange == "CFFEX":
        table = soup.select_one("table")
    elif exchange == "CZCE":
        table = next(
            (item for item in soup.select("table")
             if "交易单位" in item.get_text(" ", strip=True)
             and "交易代码" in item.get_text(" ", strip=True)),
            None,
        )
    else:
        table = soup.select_one("table")
    if table is None:
        return None, None, {}
    fields: dict[str, str] = {}
    for row in table.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.select("th,td")]
        for index in range(0, len(cells) - 1, 2):
            label, value = cells[index:index + 2]
            if label and value:
                fields[label.strip().replace(" ", "")] = value.strip()
    code = fields.get("交易代码", "").upper().replace(" ", "")
    if exchange == "DCE" and "合约月份" in code and code.endswith("F"):
        code = code.split("合约月份", 1)[0] + "-F"
    return code or None, None, fields


def _source_pages(product_keys: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    urls: dict[str, str] = ine_official_contract_urls(product_keys)
    failures: dict[str, str] = {}
    for exchange, url in _SEEDS.items():
        if exchange == "INE":
            continue
        try:
            html = _decode(_fetch(url))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            failures[exchange] = str(error)[:220]
            continue
        soup = BeautifulSoup(html, "html.parser")
        if exchange in {"SHFE", "INE"}:
            for anchor in soup.select("a[href]"):
                candidate = urljoin(url, anchor["href"])
                match = re.search(r"/products/futures/(?:[^/]+/)+([a-z]{1,3})_f/?$", candidate, re.I)
                if match is None:
                    continue
                key = f"{exchange}.{match.group(1).upper()}"
                if key in product_keys and urlparse(candidate).hostname == urlparse(url).hostname:
                    urls.setdefault(key, candidate)
        elif exchange == "CFFEX":
            urls.update({f"CFFEX.{code}": urljoin(url, path) for code, path in _CFFEX_PAGES.items()
                         if f"CFFEX.{code}" in product_keys})
        elif exchange == "GFEX":
            urls.update({f"GFEX.{code}": urljoin(url, f"/gfex/{folder}/sspz.shtml")
                         for code, folder in _GFEX_PAGES.items() if f"GFEX.{code}" in product_keys})
    urls.update({key: url for key, url in _DIRECT_SAMPLES.items() if key in product_keys})
    return urls, failures


def _inventory(store: MarketStore, registry: dict[str, Any]) -> tuple[set[str], set[str]]:
    names_path = Path(__file__).with_name("config") / "futures_product_names.json"
    names = json.loads(names_path.read_text(encoding="utf-8"))
    known = set(registry.get("products", {}))
    known.update(
        f"{exchange}.{code}" for exchange, code in store.connection.execute(
            "SELECT DISTINCT exchange,product_code FROM futures_contract_daily_official "
            "WHERE quality_status='PASS'"
        ).fetchall()
    )
    known = {key for key in known if key.split(".", 1)[0] in _DOMAINS}
    ine_codes = {key.split(".", 1)[1] for key in known if key.startswith("INE.")}
    known = {key for key in known if not (key.startswith("SHFE.") and key.split(".", 1)[1] in ine_codes)}
    return known, known | set(names)


def _registry_fallback(product_key: str, registry: dict[str, Any]) -> dict[str, Any] | None:
    records = registry.get("products", {}).get(product_key)
    if records is None:
        return None
    if isinstance(records, dict):
        records = [records]
    current = max(records, key=lambda record: str(record.get("effectiveFrom") or ""))
    proof = registry.get("evidence", {}).get(current.get("evidenceId"), {})
    if proof.get("qualityStatus") != "PASS":
        return None
    return {
        "url": proof.get("sourceUrl"), "sha": proof.get("payloadSha256"),
        "retrievedAt": proof.get("retrievedAt"),
        "fields": {"合约乘数": str(current.get("contractMultiplier")),
                   "报价单位": str(current.get("quoteUnit") or "")},
    }


def _browser_archive(root: Path, product_key: str) -> dict[str, Any] | None:
    exchange, code = product_key.split(".", 1)
    if exchange not in {"DCE", "CZCE"}:
        return None
    directory = root / "bronze" / "futures_contract_info" / exchange / code
    if not directory.is_dir():
        return None
    for metadata_path in sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            url = str(metadata["sourceUrl"])
            digest = str(metadata["payloadSha256"])
            adapters = ({"LDPLAYER_WEBVIEW_CDP"} if exchange == "DCE" else
                        {"BLUESTACKS_CHROME_CDP", "LDPLAYER_WEBVIEW_CDP",
                         "BLUESTACKS_CHROME_DOM", "LDPLAYER_WEBVIEW_DOM"})
            host = "www.dce.com.cn" if exchange == "DCE" else "www.czce.com.cn"
            path_pattern = (r"/dce/channel/list/\d+\.html" if exchange == "DCE"
                            else r"/cn/sspz/(?:[^/]+/)+H077002\d{3}(?:\d{6})?index_1\.htm")
            if (metadata.get("productKey") != product_key
                    or metadata.get("adapter") not in adapters
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or (urlparse(url).hostname or "").lower() != host
                    or not re.fullmatch(path_pattern, urlparse(url).path)):
                continue
            payload = metadata_path.with_suffix(".html").read_bytes()
            if sha256(payload).hexdigest() != digest:
                continue
            source_code, published, fields = _parse_fields(exchange, _decode(payload))
            if source_code != code or len(fields) < 8:
                continue
            datetime.fromisoformat(str(metadata["retrievedAt"]))
            if metadata.get("adapter") in {"BLUESTACKS_CHROME_DOM", "LDPLAYER_WEBVIEW_DOM"} and (
                metadata.get("captureKind") != "RENDERED_DOM_SNAPSHOT"
                or metadata.get("navigationResponseStatus") != 200
            ):
                continue
            source_kind = ("OFFICIAL_BROWSER_DOM_CAPTURE" if metadata.get("adapter") in
                           {"BLUESTACKS_CHROME_DOM", "LDPLAYER_WEBVIEW_DOM"}
                           else "OFFICIAL_BROWSER_CAPTURE")
            return {"url": url, "sha": digest, "retrievedAt": metadata["retrievedAt"],
                    "publishedOn": published, "fields": fields,
                    "sourceKind": source_kind, "qualityStatus": "PASS"}
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return None


def _save_raw(root: Path, product_key: str, digest: str, payload: bytes) -> None:
    exchange, code = product_key.split(".", 1)
    path = root / "bronze" / "futures_contract_info" / exchange / code / f"{digest}.html"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _collect_page(product_key: str, url: str) -> dict[str, Any]:
    exchange, expected_code = product_key.split(".", 1)
    host = (urlparse(url).hostname or "").lower()
    if exchange == "INE":
        validate_ine_official_contract_url(product_key, url)
    elif host != _DOMAINS[exchange] and not host.endswith("." + _DOMAINS[exchange]):
        raise ValueError(f"unexpected official host for {product_key}")
    payload = _fetch(url)
    if not payload.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("official response is not an HTML contract page")
    code, published, fields = _parse_fields(exchange, _decode(payload))
    if code != expected_code or len(fields) < 4:
        raise ValueError(f"contract page has no matching futures terms for {product_key}")
    if exchange == "INE":
        validate_ine_official_contract_fields(product_key, fields)
    return {
        "url": url, "sha": sha256(payload).hexdigest(), "payload": payload,
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
        "publishedOn": published, "fields": fields,
    }


def _listing_evidence(product_key: str, registry: dict[str, Any]) -> dict[str, str] | None:
    curated = _CURATED_LISTINGS.get(product_key)
    if curated is None:
        return None
    day, evidence_id = curated
    proof = registry.get("evidence", {}).get(evidence_id)
    if not isinstance(proof, dict) or proof.get("qualityStatus") != "PASS":
        return None
    return {
        "day": day, "url": str(proof["sourceUrl"]), "sha": str(proof["payloadSha256"]),
        "retrievedAt": str(proof["retrievedAt"]),
    }


def collect_official_contract_info(data_root: Path) -> dict[str, Any]:
    """Refresh official pages, retaining precise failures and prior observations."""
    root = Path(data_root)
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    store = MarketStore(root)
    try:
        known, candidates = _inventory(store, registry)
        urls, menu_failures = _source_pages(candidates)
        keys = known | set(urls)
        archive = load_archived_contract_information(root)
        chrome_error: str | None = None
        try:
            chrome_tables = load_chrome_contract_tables(root)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            chrome_tables = {}
            chrome_error = str(error)[:220]
        additional_listings = verified_listing_evidence(root)
        fetched: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_collect_page, key, url): key for key, url in urls.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    fetched[key] = future.result()
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
                    errors[key] = str(error)[:220]
        now = datetime.now(timezone.utc).isoformat()
        counts = {"PASS": 0, "PARTIAL": 0, "FAILED": 0}
        if keys:
            placeholders = ",".join("?" for _ in keys)
            store.connection.execute(
                f"DELETE FROM futures_product_contract_info_coverage WHERE product_key NOT IN ({placeholders})",
                sorted(keys),
            )
        for key in sorted(keys):
            exchange = key.split(".", 1)[0]
            page = fetched.get(key)
            browser_page = None if page else _browser_archive(root, key)
            chrome_page = None if page or browser_page else chrome_tables.get(key)
            archive_terms = archive["observations"].get(key)
            fallback = _registry_fallback(key, registry)
            selected = page or browser_page or chrome_page or archive_terms or fallback
            status = (
                "PASS" if (
                    (selected and selected.get("qualityStatus") == "PASS")
                    or page or browser_page or chrome_page
                )
                else "PARTIAL" if selected else "FAILED"
            )
            counts[status] += 1
            if page:
                _save_raw(root, key, page["sha"], page["payload"])
            if selected:
                store.connection.execute(
                    """INSERT OR REPLACE INTO futures_product_contract_spec_observation
                    (product_key,source_url,payload_sha256,retrieved_at,published_on,effective_from,
                     fields_json,source_kind,parser_version,quality_status)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [key, selected["url"], selected["sha"], selected["retrievedAt"],
                     selected.get("publishedOn"), selected.get("effectiveFrom"),
                     json.dumps(selected["fields"], ensure_ascii=False),
                     selected.get("sourceKind") or ("OFFICIAL_CONTRACT_PAGE" if page else "OFFICIAL_MULTIPLIER_EVIDENCE"),
                     PARSER_VERSION, status],
                )
            listing = additional_listings.get(key) or _listing_evidence(key, registry)
            year_listing = archive["productListings"].get(key)
            if listing:
                store.connection.execute(
                    """INSERT OR IGNORE INTO futures_product_listing_evidence
                    (product_key,source_url,payload_sha256,retrieved_at,first_trading_day,
                     date_precision,evidence_status,note) VALUES (?,?,?,?,?,?,?,?)""",
                    [key, listing["url"], listing["sha"], listing["retrievedAt"],
                     listing["day"], listing.get("precision", "DAY"), "PASS",
                     listing.get("note") or "交易所上市通知或含明确日期的官方报告"],
                )
            if year_listing:
                store.connection.execute(
                    """INSERT OR IGNORE INTO futures_product_listing_evidence
                    (product_key,source_url,payload_sha256,retrieved_at,first_trading_day,
                     date_precision,evidence_status,note) VALUES (?,?,?,?,?,?,?,?)""",
                    [key, year_listing["url"], year_listing["sha"], year_listing["retrievedAt"],
                     year_listing["day"], year_listing["precision"], "PASS", year_listing["note"]],
                )
            for month in archive["monthContractListings"].get(key, []):
                store.connection.execute(
                    """INSERT OR IGNORE INTO futures_month_contract_listing_evidence
                    (product_key,contract_code,listed_on,last_trading_day,last_delivery_day,
                     source_url,payload_sha256,retrieved_at) VALUES (?,?,?,?,?,?,?,?)""",
                    [key, month["contractCode"], month["listedOn"], month["lastTradingDay"],
                     month["lastDeliveryDay"], month["sourceUrl"], month["payloadSha256"],
                     month["retrievedAt"]],
                )
            # A verified browser response satisfies this product even when the
            # separate direct HTTP probe was refused.  Keep the probe failure
            # in the run report, not on an otherwise complete product row.
            error = None if status == "PASS" else errors.get(key) or menu_failures.get(exchange)
            store.connection.execute(
                """INSERT OR REPLACE INTO futures_product_contract_info_coverage
                (product_key,exchange,status,source_url,last_checked_at,error)
                VALUES (?,?,?,?,?,?)""",
                [key, exchange, status, (selected or {}).get("url"), now, error],
            )
        report = {"products": len(keys), "quality": counts, "menuFailures": menu_failures,
                  "pageFailures": errors, "archiveErrors": archive["errors"],
                  "chromeArchiveError": chrome_error, "checkedAt": now}
        state = root / "state" / "futures_contract_info_last_checked.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        temporary = state.with_name(f"{state.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        replace_with_retry(temporary, state)
        return report
    finally:
        store.close()


def read_official_contract_info(data_root: Path) -> dict[str, Any]:
    """Return one current audited view plus all observed source versions."""
    root = Path(data_root)
    if not (root / "catalog.duckdb").is_file():
        return {"products": [], "coverage": {}, "limitations": ["尚无官网期货合约信息。"]}
    store = MarketStore(root)
    try:
        coverage_rows = store.connection.execute(
            "SELECT product_key,exchange,status,source_url,last_checked_at,error "
            "FROM futures_product_contract_info_coverage ORDER BY product_key"
        ).fetchall()
        spec_rows = store.connection.execute(
            """SELECT product_key,source_url,payload_sha256,retrieved_at,published_on,
                      effective_from,fields_json,source_kind,parser_version,quality_status
               FROM futures_product_contract_spec_observation
               ORDER BY product_key,
                 CASE WHEN source_kind IN ('OFFICIAL_CONTRACT_PAGE','OFFICIAL_BROWSER_CAPTURE',
                                           'OFFICIAL_BROWSER_DOM_CAPTURE',
                                           'OFFICIAL_CHROME_DOM_FIELDS',
                                           'OFFICIAL_CZCE_CONTRACT_DOCX') THEN 0 ELSE 1 END,
                 CASE quality_status WHEN 'PASS' THEN 0 ELSE 1 END,
                 published_on DESC NULLS LAST,retrieved_at DESC"""
        ).fetchall()
        listing_rows = store.connection.execute(
            """SELECT product_key,source_url,payload_sha256,retrieved_at,first_trading_day,
                      date_precision,evidence_status,note
               FROM futures_product_listing_evidence
               ORDER BY product_key,
                 CASE evidence_status WHEN 'PASS' THEN 0 ELSE 1 END,
                 CASE date_precision WHEN 'DAY' THEN 0 WHEN 'MONTH' THEN 1 ELSE 2 END,
                 retrieved_at DESC"""
        ).fetchall()
        month_rows = store.connection.execute(
            """SELECT product_key,contract_code,listed_on,last_trading_day,
                      last_delivery_day,source_url,payload_sha256,retrieved_at
               FROM futures_month_contract_listing_evidence
               ORDER BY product_key,contract_code"""
        ).fetchall()
        first_seen = dict(store.connection.execute(
            "SELECT exchange || '.' || product_code,min(trade_date) "
            "FROM futures_contract_daily_official WHERE quality_status='PASS' GROUP BY 1"
        ).fetchall())
    finally:
        store.close()
    names_path = Path(__file__).with_name("config") / "futures_product_names.json"
    names = json.loads(names_path.read_text(encoding="utf-8"))
    specs: dict[str, list[dict[str, Any]]] = {}
    for (key, url, digest, retrieved, published, effective, fields_json,
         kind, parser, status) in spec_rows:
        specs.setdefault(key, []).append({
            "sourceUrl": url, "payloadSha256": digest, "retrievedAt": retrieved,
            "publishedOn": published, "effectiveFrom": effective,
            "fields": json.loads(fields_json), "sourceKind": kind,
            "parserVersion": parser, "qualityStatus": status,
        })
    listings: dict[str, dict[str, Any]] = {}
    for key, url, digest, retrieved, day, precision, status, note in listing_rows:
        listings.setdefault(key, {
            "date": day, "precision": precision, "status": status,
            "sourceUrl": url, "payloadSha256": digest,
            "retrievedAt": retrieved, "note": note,
        })
    month_listings: dict[str, list[dict[str, Any]]] = {}
    for key, code, listed, last_trade, last_delivery, url, digest, retrieved in month_rows:
        month_listings.setdefault(key, []).append({
            "contractCode": code, "listedOn": listed,
            "lastTradingDay": last_trade, "lastDeliveryDay": last_delivery,
            "sourceUrl": url, "payloadSha256": digest, "retrievedAt": retrieved,
        })
    products = []
    counts: dict[str, int] = {"PASS": 0, "PARTIAL": 0, "FAILED": 0}
    for key, exchange, status, url, checked, error in coverage_rows:
        counts[status] = counts.get(status, 0) + 1
        history = specs.get(key, [])
        products.append({
            "key": key, "exchange": exchange, "productCode": key.split(".", 1)[1],
            "name": names.get(key) or (history[0]["fields"].get("交易品种") if history else None) or key,
            "qualityStatus": status, "lastCheckedAt": checked,
            "error": error, "sourceUrl": url,
            "listing": listings.get(key), "firstObservedDay": first_seen.get(key),
            "monthContractListings": month_listings.get(key, []),
            "currentSpec": history[0] if history else None, "versions": history,
        })
    return {"products": products, "coverage": counts,
            "limitations": ["上市交易日无官网证据时保持为空；合约文本发布日期与品种上市日不同。"]}


__all__ = ("collect_official_contract_info", "read_official_contract_info", "PARSER_VERSION")
