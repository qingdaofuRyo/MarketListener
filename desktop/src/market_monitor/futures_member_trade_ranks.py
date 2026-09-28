"""Normalise separately published futures member trade-volume rankings."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .futures_member_positions import (
    ALL_EXCHANGES,
    _exchange_for_product,
    _member_key,
    _member_name,
    _normalise_contract,
    _positive_rank,
    _product_code,
)


@dataclass(frozen=True)
class MemberTradeRank:
    trading_day: str
    exchange: str
    contract_code: str
    product_code: str
    rank: int
    member_key: str
    member_name: str
    volume: float
    volume_change: float | None
    source: str
    collected_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "trading_day": self.trading_day, "exchange": self.exchange,
            "contract_code": self.contract_code, "product_code": self.product_code,
            "rank": self.rank, "member_key": self.member_key,
            "member_name": self.member_name, "volume": self.volume,
            "volume_change": self.volume_change, "source": self.source,
            "collected_at": self.collected_at,
        }


def normalise_exchange_member_trade_ranks(
    frames: Mapping[Any, Any], *, exchange: str, trading_day: str,
    source: str, collected_at: str,
) -> list[MemberTradeRank]:
    venue = exchange.strip().upper()
    if venue not in ALL_EXCHANGES:
        raise ValueError(f"unsupported futures exchange: {exchange}")
    output: list[MemberTradeRank] = []
    seen: set[tuple[str, int, str]] = set()
    for raw_contract, frame in frames.items():
        contract = _normalise_contract(raw_contract)
        if contract is None:
            continue
        product = _product_code(contract)
        effective_exchange = _exchange_for_product(venue, product)
        to_records = getattr(frame, "to_dict", None)
        if not callable(to_records):
            continue
        try:
            records = to_records(orient="records")
        except (TypeError, ValueError):
            continue
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, Mapping):
                continue
            rank = _positive_rank(record.get("rank"))
            name = _member_name(record.get("vol_party_name"))
            volume = _finite(record.get("vol"))
            if rank is None or name is None or volume is None or volume < 0:
                continue
            key = (contract, rank, _member_key(name))
            if key in seen:
                continue
            seen.add(key)
            output.append(MemberTradeRank(
                trading_day=trading_day, exchange=effective_exchange,
                contract_code=contract, product_code=product, rank=rank,
                member_key=_member_key(name), member_name=name,
                volume=volume, volume_change=_finite(record.get("vol_chg")),
                source=source, collected_at=collected_at,
            ))
    return sorted(output, key=lambda row: (
        row.exchange, row.contract_code, row.rank, row.member_key,
    ))


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = ("MemberTradeRank", "normalise_exchange_member_trade_ranks")
