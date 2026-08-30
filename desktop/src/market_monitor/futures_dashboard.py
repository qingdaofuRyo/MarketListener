"""期货市场看板：每日涨跌家数与持仓龙虎榜汇总。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class FuturesBreadthSnapshot:
    """国内期货主力/加权合约每日上涨、下跌、平盘个数。"""

    trading_day: str
    advances: int
    declines: int
    unchanged: int
    series_kind: str = "MAIN"
    metric_definition: str = "上涨=close>前收盘；下跌=close<前收盘；平盘=close==前收盘"
    calculation_method: str = "按 trading_day 分组，以 1d bar 的前收盘环比计算"
    timestamp: str = ""
    source: str = "local-computed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trading_day": self.trading_day,
            "advances": self.advances,
            "declines": self.declines,
            "unchanged": self.unchanged,
            "series_kind": self.series_kind,
            "metric_definition": self.metric_definition,
            "calculation_method": self.calculation_method,
            "timestamp": self.timestamp,
            "source": self.source,
        }


def compute_futures_breadth(
    bars_by_day: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    series_kind: str = "MAIN",
    now: datetime | None = None,
) -> list[FuturesBreadthSnapshot]:
    """按交易日计算期货涨跌家数（一个交易日一条）。

    bars 使用统一 bar 字段：instrument_id / trading_day / close。
    首日没有前收盘的品种不计入任何分类。
    """

    if series_kind not in {"MAIN", "WEIGHTED", "CONTRACT", "INDEX"}:
        raise ValueError("series_kind must be MAIN/WEIGHTED/CONTRACT/INDEX")
    timestamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    previous_by_code: dict[str, float] = {}
    snapshots: list[FuturesBreadthSnapshot] = []
    for trading_day in sorted(bars_by_day):
        advances = declines = unchanged = 0
        for bar in bars_by_day[trading_day]:
            code = _instrument_id(bar)
            close = float(bar["close"])
            previous = previous_by_code.get(code)
            if previous is None or previous == 0:
                pass
            elif close > previous:
                advances += 1
            elif close < previous:
                declines += 1
            else:
                unchanged += 1
            previous_by_code[code] = close
        snapshots.append(
            FuturesBreadthSnapshot(
                trading_day=str(trading_day),
                advances=advances,
                declines=declines,
                unchanged=unchanged,
                series_kind=series_kind,
                timestamp=timestamp,
            )
        )
    return snapshots


@dataclass(frozen=True)
class MemberPositionRow:
    """一条期货公司会员持仓记录（来源：交易所会员持仓排名/同花顺龙虎榜）。"""

    member: str
    instrument_id: str
    trading_day: str
    long_position: float
    long_position_change: float
    short_position: float
    short_position_change: float
    source: str = "exchange-member-ranking"

    def to_dict(self) -> dict[str, Any]:
        return {
            "member": self.member,
            "instrument_id": self.instrument_id,
            "trading_day": self.trading_day,
            "long_position": self.long_position,
            "long_position_change": self.long_position_change,
            "short_position": self.short_position,
            "short_position_change": self.short_position_change,
            "net_position": self.net_position,
            "net_position_change": self.net_position_change,
            "source": self.source,
        }

    @property
    def net_position(self) -> float:
        return self.long_position - self.short_position

    @property
    def net_position_change(self) -> float:
        return self.long_position_change - self.short_position_change


@dataclass(frozen=True)
class OpenInterestLeaderRow:
    """一个品种（加权合约汇总）的持仓龙虎榜聚合行。"""

    instrument_id: str
    trading_day: str
    long_position: float
    long_position_change: float
    short_position: float
    short_position_change: float
    net_position: float
    net_position_change: float
    member_count: int
    source: str
    metric_definition: str = "多头=会员多头持仓合计；空头=会员空头持仓合计；净=多-空"
    calculation_method: str = "按 instrument_id+trading_day 汇总会员持仓，按净持仓降序"

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "trading_day": self.trading_day,
            "long_position": self.long_position,
            "long_position_change": self.long_position_change,
            "short_position": self.short_position,
            "short_position_change": self.short_position_change,
            "net_position": self.net_position,
            "net_position_change": self.net_position_change,
            "member_count": self.member_count,
            "source": self.source,
            "metric_definition": self.metric_definition,
            "calculation_method": self.calculation_method,
        }


def build_open_interest_leaderboard(
    rows: Sequence[MemberPositionRow],
    *,
    trading_day: str | None = None,
    top_n: int | None = None,
) -> list[OpenInterestLeaderRow]:
    """把会员持仓明细聚合为品种级龙虎榜；默认只取最新交易日，可显式指定。"""

    if not rows:
        return []
    if trading_day is None:
        trading_day = max(row.trading_day for row in rows)
    if top_n is not None and top_n <= 0:
        raise ValueError("top_n must be positive")
    groups: dict[str, list[MemberPositionRow]] = {}
    for row in rows:
        if row.trading_day != trading_day:
            continue
        groups.setdefault(row.instrument_id, []).append(row)
    leaderboard = [
        OpenInterestLeaderRow(
            instrument_id=instrument_id,
            trading_day=trading_day,
            long_position=sum(row.long_position for row in group),
            long_position_change=sum(row.long_position_change for row in group),
            short_position=sum(row.short_position for row in group),
            short_position_change=sum(row.short_position_change for row in group),
            net_position=sum(row.net_position for row in group),
            net_position_change=sum(row.net_position_change for row in group),
            member_count=len({row.member for row in group}),
            source=";".join(sorted({row.source for row in group})),
        )
        for instrument_id, group in groups.items()
    ]
    leaderboard.sort(key=lambda row: row.net_position, reverse=True)
    if top_n is not None:
        leaderboard = leaderboard[:top_n]
    return leaderboard


def _instrument_id(bar: Mapping[str, Any]) -> str:
    instrument_key = bar.get("instrument_key")
    if isinstance(instrument_key, Mapping):
        return str(instrument_key.get("code") or bar.get("instrument_id") or "")
    return str(bar.get("instrument_id") or "")
