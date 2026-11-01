"""Date-effective, exchange-published futures trading multipliers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlparse


REGISTRY_PATH = Path(__file__).with_name("config") / "futures_official_multipliers.json"
_KEY = re.compile(r"^(SHFE|INE|DCE|CZCE|GFEX|CFFEX)\.([A-Z]+(?:-F)?)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOMAINS = {
    "SHFE": {"shfe.com.cn"}, "INE": {"shfe.com.cn", "ine.cn"},
    "DCE": {"dce.com.cn"}, "CZCE": {"czce.com.cn"},
    "GFEX": {"gfex.com.cn"}, "CFFEX": {"cffex.com.cn", "e-cffex.com.cn"},
}


@dataclass(frozen=True)
class OfficialMultiplier:
    exchange: str
    product_code: str
    effective_from: date
    effective_to: date | None
    contract_multiplier: float
    quote_unit: str
    source_url: str
    retrieved_at: str
    payload_sha256: str
    last_trading_day_rule: str | None = None


class OfficialMultiplierRegistry:
    def __init__(
        self,
        products: Mapping[str, tuple[OfficialMultiplier, ...]],
        exchange_coverage: Mapping[str, str],
    ) -> None:
        self.products = dict(products)
        self.exchange_coverage = dict(exchange_coverage)

    def resolve(
        self, trading_day: date | str, exchange: str, product_code: str,
    ) -> OfficialMultiplier | None:
        day = trading_day if isinstance(trading_day, date) else date.fromisoformat(str(trading_day))
        key = f"{exchange.upper()}.{product_code.upper()}"
        matches = [
            item for item in self.products.get(key, ())
            if item.effective_from <= day
            and (item.effective_to is None or day <= item.effective_to)
        ]
        if len(matches) > 1:
            raise ValueError(f"overlapping official multiplier records: {key} on {day}")
        return matches[0] if matches else None


def load_official_multiplier_registry(path: Path | None = None) -> OfficialMultiplierRegistry:
    source = path or REGISTRY_PATH
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schemaVersion") != 1:
        raise ValueError("unsupported official multiplier registry schema")
    evidence = payload.get("evidence")
    products = payload.get("products")
    coverage = payload.get("exchangeCoverage")
    if not isinstance(evidence, Mapping) or not isinstance(products, Mapping) or not isinstance(coverage, Mapping):
        raise ValueError("official multiplier registry is missing evidence, products or coverage")
    parsed: dict[str, tuple[OfficialMultiplier, ...]] = {}
    for key, raw_values in products.items():
        match = _KEY.fullmatch(str(key))
        if match is None:
            raise ValueError(f"invalid official multiplier product key: {key}")
        exchange, product_code = match.groups()
        records = raw_values if isinstance(raw_values, list) else [raw_values]
        output: list[OfficialMultiplier] = []
        for raw in records:
            if not isinstance(raw, Mapping):
                raise ValueError(f"invalid official multiplier record: {key}")
            evidence_id = str(raw.get("evidenceId") or "")
            proof = evidence.get(evidence_id)
            if not isinstance(proof, Mapping) or proof.get("qualityStatus") != "PASS":
                raise ValueError(f"missing verified official multiplier evidence: {key}")
            url = str(proof.get("sourceUrl") or "")
            host = (urlparse(url).hostname or "").lower()
            if not any(host == domain or host.endswith("." + domain) for domain in _DOMAINS[exchange]):
                raise ValueError(f"unexpected official multiplier source domain: {key}")
            digest = str(proof.get("payloadSha256") or "")
            if not _SHA256.fullmatch(digest):
                raise ValueError(f"invalid source SHA-256 for {key}")
            retrieved_at = str(proof.get("retrievedAt") or "")
            datetime.fromisoformat(retrieved_at)
            effective_from = date.fromisoformat(str(raw["effectiveFrom"]))
            effective_to = date.fromisoformat(str(raw["effectiveTo"])) if raw.get("effectiveTo") else None
            if effective_to is not None and effective_to < effective_from:
                raise ValueError(f"invalid official multiplier effective interval: {key}")
            multiplier = float(raw["contractMultiplier"])
            if not math.isfinite(multiplier) or multiplier <= 0:
                raise ValueError(f"invalid official multiplier value: {key}")
            quote_unit = str(raw.get("quoteUnit") or "")
            if not quote_unit:
                raise ValueError(f"missing official quote unit: {key}")
            last_trading_day_rule = raw.get("lastTradingDayRule")
            if last_trading_day_rule not in (None, "LAST_TRADING_DAY_OF_PREVIOUS_MONTH"):
                raise ValueError(f"unsupported official last trading day rule: {key}")
            output.append(OfficialMultiplier(
                exchange, product_code, effective_from, effective_to, multiplier,
                quote_unit, url, retrieved_at, digest, last_trading_day_rule,
            ))
        ordered = sorted(output, key=lambda item: item.effective_from)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.effective_to is None or previous.effective_to >= current.effective_from:
                raise ValueError(f"overlapping official multiplier intervals: {key}")
        parsed[key] = tuple(ordered)
    return OfficialMultiplierRegistry(parsed, {str(key): str(value) for key, value in coverage.items()})


__all__ = ("OfficialMultiplier", "OfficialMultiplierRegistry", "load_official_multiplier_registry", "REGISTRY_PATH")
