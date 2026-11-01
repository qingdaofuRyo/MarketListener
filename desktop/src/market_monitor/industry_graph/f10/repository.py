"""Read-only adapter from the existing F10 cache to canonical company DTOs."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import CompanyDetail, CompanySummary, MoneySnapshot, RevenueSegment, _clean_text
from .segments import revenue_classification


_MAX_PAGE_SIZE = 500
_SORT_FIELDS = {"name", "code", "market", "updatedAt", "totalMarketCap"}


@dataclass(frozen=True)
class CompanyPage:
    items: tuple[CompanySummary, ...]
    total: int
    page: int
    page_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "page": self.page,
            "pageSize": self.page_size,
        }


class CompanyRepository:
    """Canonical read view over the existing exported F10 JSONL files.

    This is intentionally an adapter, not a new company database.  The source
    records remain under ``data_control/f10`` and are exported to
    ``data_control/industry/f10`` by the existing fetch service.
    """

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self._fingerprint: tuple[tuple[str, int, int], ...] | None = None
        self._details: dict[str, CompanyDetail] = {}

    def list_companies(
        self,
        *,
        query: str = "",
        market: str | None = None,
        page: int = 1,
        page_size: int = 50,
        sort: str = "name",
        descending: bool = False,
    ) -> CompanyPage:
        if page < 1:
            raise ValueError("page must be at least 1")
        if page_size < 1 or page_size > _MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}")
        if sort not in _SORT_FIELDS:
            raise ValueError(f"sort must be one of: {', '.join(sorted(_SORT_FIELDS))}")
        market = _query_market(market) if market else None
        keyword = query.strip().casefold()
        records = list(self._load().values())
        summaries = [record.summary for record in records]
        if market:
            summaries = [item for item in summaries if item.market == market]
        if keyword:
            summaries = [
                item
                for item in summaries
                if keyword in item.name.casefold() or keyword in item.code.casefold() or keyword in item.instrument_key.casefold()
            ]
        summaries.sort(key=lambda item: _sort_value(item, sort), reverse=descending)
        total = len(summaries)
        start = (page - 1) * page_size
        return CompanyPage(tuple(summaries[start : start + page_size]), total, page, page_size)

    def company(self, instrument_key: str) -> CompanyDetail | None:
        return self._load().get(instrument_key.strip().upper())

    def _load(self) -> dict[str, CompanyDetail]:
        paths = self._source_paths()
        fingerprint = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths if path.is_file())
        if fingerprint == self._fingerprint:
            return self._details
        details: dict[str, CompanyDetail] = {}
        for path in paths:
            if not path.is_file():
                continue
            for record in _read_jsonl(path):
                detail = company_detail_from_record(record)
                if detail is not None:
                    details[detail.summary.instrument_key] = detail
        self._fingerprint = fingerprint
        self._details = details
        return details

    def _source_paths(self) -> tuple[Path, ...]:
        root = self.data_root / "industry" / "f10"
        return (root / "cn_f10.jsonl", root / "hk_f10.jsonl")


def company_detail_from_record(record: Mapping[str, Any]) -> CompanyDetail | None:
    market = _normalize_market(record.get("market"))
    code = str(record.get("code") or "").strip().upper()
    name = _clean_text(record.get("name"))
    if not code or not name:
        return None
    instrument_key = _instrument_key(market, code)
    currency = "HKD" if market == "HK" else "CNY"
    created_at = (
        _clean_text(record.get("created_at"))
        or _clean_text(record.get("detail_created_at"))
        or _clean_text(record.get("detail_fetched_at"))
        or _clean_text(record.get("fetched_at"))
    )
    updated_at = (
        _clean_text(record.get("enriched_at"))
        or _clean_text(record.get("fetched_at"))
        or _clean_text(record.get("detail_fetched_at"))
    )
    source = _clean_text(record.get("source")) or "local_f10_cache"
    quote_as_of = _clean_text(record.get("quote_as_of")) or _clean_text(record.get("quote_time")) or updated_at
    quote_source = _clean_text(record.get("quote_source")) or "tencent_quote"
    total_cap = _market_cap(record.get("total_market_cap"), currency, quote_as_of, quote_source)
    float_cap = _market_cap(record.get("float_market_cap"), currency, quote_as_of, quote_source)
    revenue_segments = tuple(_revenue_segments(record.get("revenue_breakdown")))
    products = tuple(
        product
        for product in (_clean_text(item) for item in (record.get("products") or []))
        if product is not None
    )
    summary = CompanySummary(
        instrument_key=instrument_key,
        name=name,
        code=code,
        market=market,
        company_position=_clean_text(record.get("company_position")) or _clean_text(record.get("position")),
        company_highlight=_clean_text(record.get("company_highlight")) or _clean_text(record.get("highlight")),
        company_website=_clean_text(record.get("company_website")) or _clean_text(record.get("org_web")),
        total_market_cap=total_cap,
        float_market_cap=float_cap,
        company_intro=_clean_text(record.get("profile")) or _clean_text(record.get("org_profile")),
        industry=_clean_text(record.get("industry")) or _clean_text(record.get("industry_em")),
        csrc_industry=_clean_text(record.get("csrc_industry")) or _clean_text(record.get("industry_csrc")),
        industry_tdx=_clean_text(record.get("industry_tdx")),
        industry_sw=_clean_text(record.get("industry_sw")),
        industry_em=_clean_text(record.get("industry_em")),
        industry_hs=_clean_text(record.get("industry_hs")),
        main_business=_clean_text(record.get("main_business")),
        business_scope=_clean_text(record.get("business_scope")),
        total_shares=_num(record.get("total_shares")),
        float_shares=_num(record.get("float_shares")),
        largest_revenue_segment=_top_revenue_segment(revenue_segments),
        products=products,
        source=source,
        created_at=created_at,
        updated_at=updated_at,
    )
    return CompanyDetail(
        summary=summary,
        revenue_segments=revenue_segments,
        sources=(source,),
        raw_status=_clean_text(record.get("status")),
    )


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _normalize_market(value: object) -> str:
    market = str(value or "CN").strip().upper()
    if market in {"HK", "HKG", "HKEX"}:
        return "HK"
    return "CN"


def _query_market(value: str) -> str:
    market = value.strip().upper()
    if market in {"CN", "HK"}:
        return market
    raise ValueError("market must be CN or HK")


def _instrument_key(market: str, code: str) -> str:
    if market == "HK":
        return f"HK.HKEX.STOCK.{code.zfill(5)}"
    if code.startswith(("6", "9")):
        exchange = "SSE"
    elif code.startswith(("4", "8")):
        exchange = "BSE"
    else:
        exchange = "SZSE"
    return f"CN.{exchange}.STOCK.{code}"


def _market_cap(value: object, currency: str, as_of: str | None, source: str) -> MoneySnapshot | None:
    if isinstance(value, Mapping):
        snapshot = MoneySnapshot.from_dict(value)
        if snapshot is not None:
            return snapshot
        value = value.get("value")
    try:
        yi = float(value)
    except (TypeError, ValueError):
        return None
    if yi <= 0:
        return None
    if not as_of:
        return None
    # Existing Tencent quote fields are expressed in yi (1e8) currency units.
    return MoneySnapshot(value=yi * 100_000_000, currency=currency, as_of=as_of, source=source)


def _source_timestamp(value: object) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return text if parsed.tzinfo is not None else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _revenue_segments(raw: object) -> Iterable[RevenueSegment]:
    if not isinstance(raw, list):
        return ()
    rows: list[RevenueSegment] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = _clean_text(item.get("item_name") or item.get("item") or item.get("name"))
        if not name:
            continue
        raw_currency = _clean_text(item.get("currency"))
        currency = raw_currency.upper() if raw_currency and len(raw_currency) == 3 and raw_currency.isascii() and raw_currency.isalpha() else "UNKNOWN"
        source = _clean_text(item.get("source")) or "unknown"
        resolved = revenue_classification(item)
        ratio = _finite_number(item.get("ratio"))
        share_pct = _finite_number(item.get("revenue_share_pct"))
        revenue_value = item.get("revenue", item.get("income", item.get("amount")))
        fetched_at = _source_timestamp(item.get("fetched_at"))
        source_as_of = fetched_at or _source_timestamp(item.get("as_of"))
        amount = _revenue_amount(
            revenue_value,
            currency,
            source_as_of,
            source,
        )
        amount_without_timestamp = None
        if amount is None and source_as_of is None:
            try:
                raw_value = float(revenue_value) if not isinstance(revenue_value, bool) else float("nan")
                if math.isfinite(raw_value):
                    amount_without_timestamp = {"value": raw_value, "currency": currency}
            except (TypeError, ValueError):
                pass
        cost = _revenue_amount(
            item.get("cost"),
            currency,
            source_as_of,
            source,
        )
        gross_profit = _revenue_amount(
            item.get("gross_profit"),
            currency,
            source_as_of,
            source,
        )
        gross_margin_pct = _finite_number(item.get("gross_margin_pct"))
        rows.append(
            RevenueSegment(
                name=name,
                amount=amount,
                amount_without_timestamp=amount_without_timestamp,
                ratio=ratio if ratio is not None and ratio >= 0 else None,
                segment_type=_clean_text(item.get("type")),
                revenue_share_pct=share_pct,
                classification=resolved[0] if resolved else None,
                classification_label=resolved[1] if resolved else None,
                period=_clean_text(item.get("period")),
                source=_clean_text(item.get("source")),
                fetched_at=fetched_at,
                cost=cost,
                gross_profit=gross_profit,
                gross_margin_pct=gross_margin_pct,
            )
        )
    return tuple(rows)


def _revenue_amount(value: object, currency: str, as_of: str | None, source: str) -> MoneySnapshot | None:
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount) or not as_of:
        return None
    return MoneySnapshot(value=amount, currency=currency, as_of=as_of, source=source)


def _top_revenue_segment(segments: Iterable[RevenueSegment]) -> RevenueSegment | None:
    valid: list[tuple[date, RevenueSegment]] = []
    for item in segments:
        if not item.period or _segment_amount(item) <= 0 or item.classification == "region":
            continue
        try:
            period = date.fromisoformat(item.period[:10])
        except ValueError:
            continue
        valid.append((period, item))
    if not valid:
        return None
    latest_period = max(period for period, _ in valid)
    same_period = [item for period, item in valid if period == latest_period]
    preference = ("product", "business", "industry", "project", "other")
    for classification in preference:
        group = [item for item in same_period if (item.classification or "").strip().lower() == classification]
        if group:
            return _max_comparable_segment(group)
    untyped = [item for item in same_period if not (item.classification or "").strip()]
    if untyped:
        return _max_comparable_segment(untyped)
    return None


def _segment_amount(item: RevenueSegment) -> float:
    if item.amount is not None:
        return item.amount.value
    return float(item.amount_without_timestamp["value"]) if item.amount_without_timestamp else -1.0


def _max_comparable_segment(items: list[RevenueSegment]) -> RevenueSegment | None:
    currencies = {item.amount.currency if item.amount is not None else item.amount_without_timestamp["currency"]
                  for item in items if item.amount is not None or item.amount_without_timestamp}
    if len(currencies) != 1 or "UNKNOWN" in currencies:
        return None
    return max(items, key=_segment_amount)


def _sort_value(item: CompanySummary, sort: str) -> tuple[object, ...]:
    if sort == "totalMarketCap":
        return (item.total_market_cap.value if item.total_market_cap else -1.0, item.name)
    if sort == "updatedAt":
        return (item.updated_at or "", item.name)
    if sort == "market":
        return (item.market, item.name)
    return (getattr(item, sort), item.instrument_key)


def _num(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


__all__ = ("CompanyPage", "CompanyRepository", "company_detail_from_record")
