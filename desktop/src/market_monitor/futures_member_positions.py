"""Normalise exchange-published futures member-position ranking tables.

The exchanges publish *ranking coverage*, not each member's full book.  This
module deliberately keeps every published long/short ranking as a separate
record.  A member absent from one direction is therefore ``null`` at API
read-time, never silently converted to a zero position.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping


COMMODITY_EXCHANGES = frozenset({"SHFE", "INE", "DCE", "CZCE", "GFEX"})
# INE publishes through the SHFE ranking adapter, but remains a separate
# exchange in every public contract, coverage record, and API filter.
ALL_EXCHANGES = ("CFFEX", "DCE", "CZCE", "SHFE", "INE", "GFEX")
_INE_PRODUCTS = frozenset({"BC", "EC", "LU", "NR", "SC"})
_MEMBER_SUMMARY_MARKERS = ("合计", "总计", "total", "subtotal")


@dataclass(frozen=True)
class MemberPositionRank:
    """One exchange-published member rank in one direction."""

    trading_day: str
    exchange: str
    contract_code: str
    product_code: str
    side: str
    rank: int
    member_key: str
    member_name: str
    position: float
    position_change: float | None
    source: str
    collected_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "trading_day": self.trading_day,
            "exchange": self.exchange,
            "contract_code": self.contract_code,
            "product_code": self.product_code,
            "side": self.side,
            "rank": self.rank,
            "member_key": self.member_key,
            "member_name": self.member_name,
            "position": self.position,
            "position_change": self.position_change,
            "source": self.source,
            "collected_at": self.collected_at,
        }


@dataclass(frozen=True)
class ExchangeRankCoverage:
    """A source-level result kept even when one exchange is unavailable."""

    exchange: str
    status: str
    contract_count: int
    record_count: int
    source: str
    error: str | None = None

    def to_dict(self, *, trading_day: str, collected_at: str) -> dict[str, Any]:
        """Return the persistent evidence shape for one source/exchange probe."""

        return {
            "trading_day": trading_day,
            "exchange": self.exchange,
            "status": self.status,
            "contract_count": self.contract_count,
            "record_count": self.record_count,
            "source": self.source,
            "error": self.error,
            "collected_at": collected_at,
        }


def collect_exchange_member_position_ranks(
    api: Any,
    *,
    day_compact: str,
    trading_day: str,
    collected_at: str,
) -> tuple[list[MemberPositionRank], list[ExchangeRankCoverage]]:
    """Read available exchange tables without treating a partial source as full market.

    AkShare's DCE batch endpoint may intermittently return a non-ZIP response;
    that failure is reported separately and does not suppress the other
    exchanges.  The public function is deliberately small enough to drive with
    deterministic frames in unit tests.
    """

    rows: list[MemberPositionRank] = []
    coverage: list[ExchangeRankCoverage] = []
    for exchange, function_name in _EXCHANGE_FUNCTIONS:
        source = f"akshare-{exchange.lower()}-member-ranking"
        coverage_exchanges = _COVERAGE_EXCHANGES[exchange]
        fn = getattr(api, function_name, None)
        if not callable(fn):
            coverage.extend(
                ExchangeRankCoverage(item, "UNSUPPORTED", 0, 0, source, f"missing {function_name}")
                for item in coverage_exchanges
            )
            continue
        try:
            frames = fn(date=day_compact)
        except Exception as error:  # provider failures must remain visible per exchange
            message = f"{type(error).__name__}: {error}"[:300]
            coverage.extend(
                ExchangeRankCoverage(item, "FAILED", 0, 0, source, message)
                for item in coverage_exchanges
            )
            continue
        if not isinstance(frames, Mapping):
            coverage.extend(
                ExchangeRankCoverage(item, "FAILED", 0, 0, source, "provider returned non-mapping result")
                for item in coverage_exchanges
            )
            continue
        normalised = normalise_exchange_member_position_ranks(
            frames,
            exchange=exchange,
            trading_day=trading_day,
            source=source,
            collected_at=collected_at,
        )
        if not normalised:
            coverage.extend(
                ExchangeRankCoverage(item, "FAILED", len(frames), 0, source, "no usable published ranking rows")
                for item in coverage_exchanges
            )
            continue
        rows.extend(normalised)
        for effective_exchange in coverage_exchanges:
            exchange_rows = [row for row in normalised if row.exchange == effective_exchange]
            contracts = {row.contract_code for row in exchange_rows}
            if exchange_rows:
                coverage.append(
                    ExchangeRankCoverage(effective_exchange, "PASS", len(contracts), len(exchange_rows), source)
                )
            else:
                coverage.append(
                    ExchangeRankCoverage(
                        effective_exchange,
                        "FAILED",
                        0,
                        0,
                        source,
                        f"provider returned no usable {effective_exchange} ranking rows",
                    )
                )
    return rows, coverage


def normalise_exchange_member_position_ranks(
    frames: Mapping[Any, Any],
    *,
    exchange: str,
    trading_day: str,
    source: str,
    collected_at: str,
) -> list[MemberPositionRank]:
    """Turn AkShare's per-contract frames into explicit long/short rank rows."""

    normalised_exchange = _normalise_exchange(exchange)
    output: list[MemberPositionRank] = []
    seen: set[tuple[str, str, str, int, str]] = set()
    for raw_contract, frame in frames.items():
        contract = _normalise_contract(raw_contract)
        if contract is None:
            continue
        product = _product_code(contract)
        effective_exchange = _exchange_for_product(normalised_exchange, product)
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
            if rank is None:
                continue
            for side, member_column, position_column, change_column in _SIDES:
                member_name = _member_name(record.get(member_column))
                position = _finite_number(record.get(position_column))
                if member_name is None or position is None:
                    continue
                member_key = _member_key(member_name)
                key = (effective_exchange, contract, side, rank, member_key)
                if key in seen:
                    continue
                seen.add(key)
                output.append(
                    MemberPositionRank(
                        trading_day=trading_day,
                        exchange=effective_exchange,
                        contract_code=contract,
                        product_code=product,
                        side=side,
                        rank=rank,
                        member_key=member_key,
                        member_name=member_name,
                        position=position,
                        position_change=_finite_number(record.get(change_column)),
                        source=source,
                        collected_at=collected_at,
                    )
                )
    return sorted(output, key=lambda row: (row.exchange, row.contract_code, row.side, row.rank, row.member_key))


def is_commodity_exchange(exchange: str) -> bool:
    return exchange.upper() in COMMODITY_EXCHANGES


_EXCHANGE_FUNCTIONS: tuple[tuple[str, str], ...] = (
    ("CFFEX", "get_cffex_rank_table"),
    ("DCE", "futures_dce_position_rank"),
    ("CZCE", "get_rank_table_czce"),
    ("SHFE", "get_shfe_rank_table"),
    ("GFEX", "futures_gfex_position_rank"),
)
_COVERAGE_EXCHANGES: Mapping[str, tuple[str, ...]] = {
    "CFFEX": ("CFFEX",),
    "DCE": ("DCE",),
    "CZCE": ("CZCE",),
    "SHFE": ("SHFE", "INE"),
    "GFEX": ("GFEX",),
}
_SIDES = (
    ("LONG", "long_party_name", "long_open_interest", "long_open_interest_chg"),
    ("SHORT", "short_party_name", "short_open_interest", "short_open_interest_chg"),
)


def _normalise_exchange(value: str) -> str:
    exchange = value.strip().upper()
    if exchange not in ALL_EXCHANGES:
        raise ValueError(f"unsupported futures exchange: {value}")
    return exchange


def _normalise_contract(value: Any) -> str | None:
    contract = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    if not contract or not re.search(r"[A-Z]", contract):
        return None
    return contract


def _product_code(contract: str) -> str:
    match = re.match(r"[A-Z]+", contract)
    if match is None:  # guarded by _normalise_contract
        raise ValueError(f"contract has no product code: {contract}")
    return match.group(0)


def _exchange_for_product(exchange: str, product: str) -> str:
    # SHFE's official ranking endpoint also returns International Energy
    # Exchange contracts.  These are distinct standard exchange keys.
    return "INE" if exchange == "SHFE" and product in _INE_PRODUCTS else exchange


def _member_name(value: Any) -> str | None:
    if value is None:
        return None
    member = " ".join(str(value).split()).strip()
    if not member or member.casefold() in {"nan", "none", "-", "--"}:
        return None
    if any(marker in member.casefold() for marker in _MEMBER_SUMMARY_MARKERS):
        return None
    return member


def _member_key(member_name: str) -> str:
    return re.sub(r"\s+", "", member_name).casefold()


def _positive_rank(value: Any) -> int | None:
    number = _finite_number(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
