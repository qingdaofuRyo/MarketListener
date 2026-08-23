"""Configuration-driven market classification for canonical instruments.

The classifier deliberately treats ``code`` without its market/exchange as
incomplete identity.  Explicit canonical fields win, ordered code rules are
used second, and small allowlists resolve only documented ambiguous symbols.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any


_CONFIG_PATH = Path(__file__).with_name("config") / "market_classification.json"
_LEGACY_CATEGORIES = {
    "cn-index": {"a-index", "tdx-board-index", "tdx-industry-index"},
    "cn-stock": {"a-sh", "a-sz", "a-bse", "a-chinext", "a-star"},
    "cn-etf": {"a-etf"},
    "hk-index": {"hk-index"},
    "hk-stock": {"hk-stock"},
    "commodity-index": {"cn-future-index"},
}


@lru_cache(maxsize=1)
def market_classification_spec() -> dict[str, Any]:
    """Load the versioned external classification specification once."""
    with _CONFIG_PATH.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise ValueError("unsupported market classification specification")
    return document


def market_category_options() -> list[dict[str, str]]:
    """Return the ordered public market-filter options."""
    return [dict(item) for item in market_classification_spec()["categories"]]


def _value(item: dict[str, Any], *names: str) -> str:
    for name in names:
        value = item.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _canonical_parts(item: dict[str, Any]) -> list[str]:
    instrument_id = _value(item, "instrumentId", "instrument_id", "canonicalInstrumentId")
    return [part.upper() for part in instrument_id.split(".") if part]


def _normal_exchange(raw: str, spec: dict[str, Any]) -> str:
    value = raw.upper()
    for canonical, aliases in spec["exchangeAliases"].items():
        if value in aliases:
            return str(canonical)
    return value


def _prefix(symbol: str, prefixes: list[str]) -> bool:
    return any(symbol.startswith(prefix) for prefix in prefixes)


def _future_product(item: dict[str, Any], parts: list[str]) -> str:
    candidates = [
        _value(item, "productCode", "product_code"),
        _value(item, "symbol", "sourceSymbol", "source_symbol", "code"),
    ]
    if "FUTURE" in parts:
        index = parts.index("FUTURE")
        if index + 1 < len(parts):
            candidates.append(parts[index + 1])
    for candidate in candidates:
        match = re.match(r"^([A-Za-z]+)", candidate)
        if match:
            return match.group(1).upper()
    return ""


def _symbol(item: dict[str, Any], parts: list[str]) -> str:
    value = _value(item, "symbol", "sourceSymbol", "source_symbol", "code").upper()
    if value:
        return value
    if len(parts) >= 4:
        return parts[-1]
    return ""


def _a_share_category(symbol: str, exchange: str, asset_type: str, spec: dict[str, Any]) -> str | None:
    rules = spec["aShare"]
    for prefix, category in rules["tdxBoardPrefixes"].items():
        if symbol.startswith(prefix):
            return str(category)
    if asset_type == "ETF" or (
        asset_type not in {"INDEX", "FUTURE"}
        and exchange in rules["etfPrefixes"]
        and _prefix(symbol, rules["etfPrefixes"][exchange])
    ):
        return "a-etf"
    if asset_type == "INDEX":
        return "a-index"
    if asset_type in {"CONVERTIBLE_BOND", "EXCHANGEABLE_BOND", "PLEDGED_REPO", "REPO", "LOF", "REIT"}:
        return {
            "CONVERTIBLE_BOND": "a-convertible",
            "EXCHANGEABLE_BOND": "a-exchangeable",
            "PLEDGED_REPO": "a-pledged-repo",
            "REPO": "a-repo",
            "LOF": "a-lof",
            "REIT": "a-reit",
        }[asset_type]
    if asset_type == "STOCK":
        # Explicit stock type is the second check: it must prevent an SSE
        # index-prefix code (000xxx.SH) from being promoted to an index.
        if _prefix(symbol, rules["chinextPrefixes"]):
            return "a-chinext"
        if _prefix(symbol, rules["starPrefixes"]):
            return "a-star"
        if exchange == "BSE" or (not exchange and _prefix(symbol, rules["mainBoardPrefixes"]["BSE"])):
            return "a-bse"
        if exchange == "SSE" or (not exchange and _prefix(symbol, rules["mainBoardPrefixes"]["SSE"])):
            return "a-sh"
        if exchange == "SZSE" or (not exchange and _prefix(symbol, rules["mainBoardPrefixes"]["SZSE"])):
            return "a-sz"
        if not exchange and symbol.startswith("000"):
            return "a-sz"
        return None
    if exchange and exchange in rules["indexPrefixes"] and _prefix(symbol, rules["indexPrefixes"][exchange]):
        return "a-index"
    if not exchange and symbol in rules["indexAllowlist"]:
        return "a-index"
    for prefix_key, category in (
        ("convertibleBondPrefixes", "a-convertible"),
        ("exchangeableBondPrefixes", "a-exchangeable"),
        ("pledgedRepoPrefixes", "a-pledged-repo"),
        ("repoPrefixes", "a-repo"),
        ("lofPrefixes", "a-lof"),
        ("reitPrefixes", "a-reit"),
    ):
        if exchange and exchange in rules[prefix_key] and _prefix(symbol, rules[prefix_key][exchange]):
            return category
    if _prefix(symbol, rules["chinextPrefixes"]):
        return "a-chinext"
    if _prefix(symbol, rules["starPrefixes"]):
        return "a-star"
    if exchange == "BSE" or (exchange == "" and _prefix(symbol, rules["mainBoardPrefixes"]["BSE"])):
        return "a-bse"
    if exchange == "SSE" and _prefix(symbol, rules["mainBoardPrefixes"]["SSE"]):
        return "a-sh"
    if exchange == "SZSE" and _prefix(symbol, rules["mainBoardPrefixes"]["SZSE"]):
        return "a-sz"
    if not exchange:
        if _prefix(symbol, rules["mainBoardPrefixes"]["SSE"]):
            return "a-sh"
        # 000xxx is intentionally not guessed without an exchange: it can be
        # either an SSE index or an SZSE stock.
        safe_sz = [prefix for prefix in rules["mainBoardPrefixes"]["SZSE"] if prefix != "000"]
        if _prefix(symbol, safe_sz):
            return "a-sz"
    return None


def _future_category(
    item: dict[str, Any], symbol: str, exchange: str, series_kind: str, parts: list[str], spec: dict[str, Any]
) -> str:
    rules = spec["futures"]
    product = _future_product(item, parts)
    suffix = symbol[len(product) :] if product and symbol.startswith(product) else ""
    if series_kind == "COMMODITY_INDEX" or symbol in rules["indexAllowlist"] or suffix in rules["indexSuffixes"]:
        return "cn-future-index"
    if exchange == "CFFEX" or product in rules["cffexProducts"]:
        return "cn-future-cffex"
    if exchange in rules["commodityProducts"] or any(
        product in products for products in rules["commodityProducts"].values()
    ):
        return "cn-future-commodity"
    return "other"


def classify_market(item: dict[str, Any]) -> str:
    """Return the primary category for one instrument."""
    spec = market_classification_spec()
    parts = _canonical_parts(item)
    market = _value(item, "market", "country_or_market").upper() or (parts[0] if parts else "")
    raw_exchange = _value(item, "exchange") or (parts[1] if len(parts) > 1 else "")
    exchange = _normal_exchange(raw_exchange, spec)
    asset_type = _value(item, "assetType", "asset_type", "secType", "sec_type").upper()
    if not asset_type and len(parts) > 2:
        asset_type = parts[2]
    series_kind = _value(item, "seriesKind", "series_kind").upper()
    symbol = _symbol(item, parts)

    if market == "HK" or exchange == "HKEX":
        if asset_type == "INDEX" or not symbol.isdigit():
            return "hk-index"
        if asset_type in {"", "STOCK"} and symbol.isdigit() and 1 <= len(symbol) <= 5:
            return "hk-stock"
        return "other"

    is_future = asset_type == "FUTURE" or (
        not asset_type and (exchange in spec["futures"]["commodityProducts"] or exchange == "CFFEX")
    )
    if market == "CN" and (is_future or series_kind == "COMMODITY_INDEX"):
        return _future_category(item, symbol, exchange, series_kind, parts, spec)

    if market == "CN" or exchange in {"SSE", "SZSE", "BSE"} or re.fullmatch(r"\d{6}", symbol):
        return _a_share_category(symbol, exchange, asset_type, spec) or "other"
    return "other"


def night_session(item: dict[str, Any]) -> str | None:
    """Return the configured night-session window for an eligible commodity."""
    if classify_market(item) != "cn-future-commodity":
        return None
    product = _future_product(item, _canonical_parts(item))
    for window, products in market_classification_spec()["futures"]["nightSessions"].items():
        if product in products:
            return str(window)
    return None


def matches_market_category(item: dict[str, Any], category_key: str) -> bool:
    """Match new categories and retained R2 compatibility keys."""
    key = category_key.strip().casefold()
    if not key or key == "all":
        return True
    primary = classify_market(item)
    if key == "cn-future-night":
        return night_session(item) is not None
    if key in _LEGACY_CATEGORIES:
        return primary in _LEGACY_CATEGORIES[key]
    series_kind = _value(item, "seriesKind", "series_kind").upper()
    if key == "cn-future-main":
        return primary in {"cn-future-cffex", "cn-future-commodity"} and series_kind == "MAIN"
    if key == "cn-future-secondary":
        return primary in {"cn-future-cffex", "cn-future-commodity"} and series_kind == "SECONDARY"
    if key == "cn-future-weighted":
        return primary in {"cn-future-cffex", "cn-future-commodity"} and series_kind == "WEIGHTED"
    return primary == key


def split_zero_prefix_symbols(items: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split ambiguous 000-prefixed instruments by exchange and type.

    ``000xxx.SH`` is an SSE index while ``000xxx.SZ`` is an SZSE stock.  A
    ``type`` field (or any of the project's asset-type aliases) acts as the
    second check so a malformed exchange suffix cannot promote a stock to an
    index, and vice versa.
    """
    spec = market_classification_spec()
    indexes: list[dict[str, Any]] = []
    stocks: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        parts = _canonical_parts(item)
        symbol = _symbol(item, parts)
        if not symbol.startswith("000"):
            continue
        raw_exchange = _value(item, "exchange") or (parts[1] if len(parts) > 1 else "")
        exchange = _normal_exchange(raw_exchange, spec)
        asset_type = _value(item, "type", "assetType", "asset_type", "secType", "sec_type").upper()
        if exchange == "SSE" and asset_type != "STOCK":
            indexes.append(item)
        elif exchange == "SZSE" and asset_type != "INDEX":
            stocks.append(item)
    return {"indexes": indexes, "stocks": stocks}
