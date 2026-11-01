"""Shared F10 domain models used by APIs and every company presentation.

The models deliberately carry money value, currency, timestamp and source as
one object.  A consumer must therefore never render a market-cap number
without also knowing what it means and when it was observed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _omit_empty(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if value not in (None, "", [], {})}


@dataclass(frozen=True)
class MoneySnapshot:
    """One monetary fact in base currency units, with provenance."""

    value: float
    currency: str
    as_of: str
    source: str
    derived: bool = False
    calculation_method: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any] | None) -> "MoneySnapshot | None":
        if not document:
            return None
        value = document.get("value")
        try:
            value = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        currency = str(document.get("currency") or "").strip()
        as_of = str(document.get("asOf") or document.get("as_of") or "").strip()
        source = str(document.get("source") or "").strip()
        if value <= 0 or not currency or not as_of or not source:
            return None
        return cls(
            value=value,
            currency=currency,
            as_of=as_of,
            source=source,
            derived=bool(document.get("derived")),
            calculation_method=str(document.get("calculationMethod") or document.get("calculation_method") or "").strip() or None,
            inputs=dict(document.get("inputs") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "value": self.value,
            "currency": self.currency,
            "asOf": self.as_of,
            "source": self.source,
        }
        if self.derived:
            document["derived"] = True
        if self.calculation_method:
            document["calculationMethod"] = self.calculation_method
        if self.inputs:
            document["inputs"] = self.inputs
        return document


@dataclass(frozen=True)
class RevenueSegment:
    """One structured revenue composition item; no inferred business rank."""

    name: str
    amount: MoneySnapshot | None = None
    amount_without_timestamp: dict[str, Any] | None = None
    ratio: float | None = None
    segment_type: str | None = None
    revenue_share_pct: float | None = None
    classification: str | None = None
    classification_label: str | None = None
    period: str | None = None
    source: str | None = None
    fetched_at: str | None = None
    cost: MoneySnapshot | None = None
    gross_profit: MoneySnapshot | None = None
    gross_margin_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return _omit_empty(
            {
                "name": self.name,
                "amount": self.amount.to_dict() if self.amount is not None else None,
                "amountWithoutTimestamp": self.amount_without_timestamp,
                "ratio": self.ratio,
                "segmentType": self.segment_type,
                "revenueSharePct": self.revenue_share_pct,
                "classification": self.classification,
                "classificationLabel": self.classification_label,
                "period": self.period,
                "source": self.source,
                "fetchedAt": self.fetched_at,
                "cost": self.cost.to_dict() if self.cost is not None else None,
                "grossProfit": self.gross_profit.to_dict() if self.gross_profit is not None else None,
                "grossMarginPct": self.gross_margin_pct,
            }
        )


@dataclass(frozen=True)
class CompanySummary:
    """The one compact company view embedded by list and chain responses."""

    instrument_key: str
    name: str
    code: str
    market: str
    company_position: str | None = None
    company_highlight: str | None = None
    company_website: str | None = None
    total_market_cap: MoneySnapshot | None = None
    float_market_cap: MoneySnapshot | None = None
    company_intro: str | None = None
    industry: str | None = None
    csrc_industry: str | None = None
    industry_tdx: str | None = None
    industry_sw: str | None = None
    industry_em: str | None = None
    industry_hs: str | None = None
    main_business: str | None = None
    business_scope: str | None = None
    total_shares: float | None = None
    float_shares: float | None = None
    largest_revenue_segment: RevenueSegment | None = None
    products: tuple[str, ...] = ()
    source: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def top_revenue_segment(self) -> RevenueSegment | None:
        """Backward-compatible alias for the largest-revenue segment."""
        return self.largest_revenue_segment

    def to_dict(self) -> dict[str, Any]:
        largest = self.largest_revenue_segment.to_dict() if self.largest_revenue_segment else None
        return _omit_empty(
            {
                "instrumentKey": self.instrument_key,
                "name": self.name,
                "code": self.code,
                "market": self.market,
                "companyPosition": self.company_position,
                "companyHighlight": self.company_highlight,
                "companyWebsite": self.company_website,
                "totalMarketCap": self.total_market_cap.to_dict() if self.total_market_cap else None,
                "floatMarketCap": self.float_market_cap.to_dict() if self.float_market_cap else None,
                "companyIntro": self.company_intro,
                "industry": self.industry,
                "csrcIndustry": self.csrc_industry,
                "industryTdx": self.industry_tdx,
                "industrySw": self.industry_sw,
                "industryEm": self.industry_em,
                "industryHs": self.industry_hs,
                "mainBusiness": self.main_business,
                "businessScope": self.business_scope,
                "totalShares": self.total_shares,
                "floatShares": self.float_shares,
                "largestRevenueSegment": largest,
                "topRevenueSegment": largest,
                "products": list(self.products),
                "source": self.source,
                "createdAt": self.created_at,
                "updatedAt": self.updated_at,
            }
        )


@dataclass(frozen=True)
class CompanyDetail:
    """The complete company view.  It extends, rather than duplicates, Summary."""

    summary: CompanySummary
    revenue_segments: tuple[RevenueSegment, ...] = ()
    chain_locations: tuple[dict[str, str], ...] = ()
    sources: tuple[str, ...] = ()
    raw_status: str | None = None
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        document = self.summary.to_dict()
        document.update(
            _omit_empty(
                {
                    "revenueSegments": [segment.to_dict() for segment in self.revenue_segments],
                    "chainLocations": list(self.chain_locations),
                    "sources": list(self.sources),
                    "status": self.raw_status,
                }
            )
        )
        return document


__all__ = ("CompanyDetail", "CompanySummary", "MoneySnapshot", "RevenueSegment", "_clean_text")
