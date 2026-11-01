"""Repair dated A-share breadth gaps from quality-gated local TDX daily bars."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from .collector import _metric
from .a_share_calendar import is_verified_a_share_trading_day
from .storage import MarketStore


_FIELDS = {
    "TOTAL_AMOUNT_YI": ("当日成交额(亿)", "amount_yi"),
    "ADVANCES": ("上涨家数", "advances"),
    "UNCHANGED": ("平盘家数", "unchanged"),
    "DECLINES": ("下跌家数", "declines"),
}


def _beijing_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _trusted_existing(day: str, timestamp: str, method: str) -> bool:
    if method.startswith("source=tdx-cn-v2; historical_silver_recovery;"):
        return True
    try:
        observed = datetime.fromisoformat(timestamp)
        if observed.tzinfo is None:
            return False
        observed = observed.astimezone(ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return False
    return observed.date().isoformat() == day and (observed.hour, observed.minute) >= (15, 30)


def _silver_rows(data_root: Path, lookback_days: int) -> list[tuple]:
    today = _beijing_today()
    earliest = today - timedelta(days=lookback_days + 12)
    paths = [str(path) for year in range(earliest.year, today.year + 1)
             for path in (data_root / "silver" / "market=CN" / "asset_type=STOCK" /
                          "period=1d" / f"year={year}").glob("*.parquet")]
    if not paths:
        return []
    connection = duckdb.connect(database=":memory:")
    try:
        return connection.execute("""
            WITH raw AS (
                SELECT json_extract_string(bar_json, '$.canonical_instrument_id') AS id,
                       substr(bar_open_time, 1, 10) AS day,
                       try_cast(json_extract_string(bar_json, '$.close') AS DOUBLE) AS close_price,
                       try_cast(json_extract_string(bar_json, '$.amount') AS DOUBLE) AS amount,
                       json_extract_string(bar_json, '$.quality_status') AS quality,
                       json_extract_string(bar_json, '$.normalization_status') AS normalization,
                       json_extract_string(bar_json, '$.normalization_version') AS version,
                       try_cast(json_extract_string(bar_json, '$.price_scale') AS DOUBLE) AS price_scale,
                       try_cast(json_extract_string(bar_json, '$.volume_multiplier') AS DOUBLE) AS volume_multiplier,
                       json_extract_string(bar_json, '$.volume_unit') AS volume_unit
                FROM read_parquet(?)
                WHERE substr(bar_open_time, 1, 10) BETWEEN ? AND ?
            ), valid AS (
                SELECT * FROM raw WHERE id IS NOT NULL AND quality = 'PASS'
                  AND normalization = 'PASS' AND version = 'tdx-cn-v2'
                  AND price_scale > 0 AND volume_multiplier > 0 AND volume_unit = 'SHARE'
                  AND isfinite(close_price) AND close_price > 0
                  AND isfinite(amount) AND amount >= 0
                QUALIFY row_number() OVER (PARTITION BY id, day ORDER BY amount DESC) = 1
            ), paired AS (
                SELECT *, lag(close_price) OVER (PARTITION BY id ORDER BY day) AS previous_close
                FROM valid
            )
            SELECT day, count(*) AS coverage, count(previous_close) AS paired_coverage,
                   sum(amount) / 100000000 AS amount_yi,
                   count(*) FILTER (WHERE close_price > previous_close) AS advances,
                   count(*) FILTER (WHERE close_price = previous_close) AS unchanged,
                   count(*) FILTER (WHERE close_price < previous_close) AS declines
            FROM paired GROUP BY day ORDER BY day
        """, [paths, earliest.isoformat(), (today - timedelta(days=1)).isoformat()]).fetchall()
    finally:
        connection.close()


def recover_recent_a_share_breadth(
    data_root: Path, *, lookback_days: int = 30, min_coverage: int = 5000,
) -> dict[str, object]:
    """Fill only missing or pre-close fields; retain trustworthy Tencent snapshots."""

    data_root = Path(data_root)
    today = _beijing_today()
    threshold = (today - timedelta(days=lookback_days)).isoformat()
    candidates = [row for row in _silver_rows(data_root, lookback_days) if row[0] >= threshold]
    if not candidates:
        return {"recoveredDays": [], "metricCount": 0}
    store = MarketStore(data_root)
    try:
        existing = store.connection.execute("""
            SELECT metric_id, trading_date, timestamp, calculation_method FROM gold_metrics
            WHERE metric_id LIKE 'A_SHARE_BREADTH:%' AND trading_date >= ?
        """, [threshold]).fetchall()
        trusted = {(str(day), str(metric_id).rsplit(":", 1)[-1]) for metric_id, day, stamp, method in existing
                   if _trusted_existing(str(day), str(stamp), str(method))}
        metrics = []
        recovered_days = []
        for day, coverage, paired, amount_yi, advances, unchanged, declines in candidates:
            if not is_verified_a_share_trading_day(date.fromisoformat(day)):
                continue
            if coverage < min_coverage or paired < coverage * .98 or amount_yi is None or amount_yi <= 0:
                continue
            values = {"amount_yi": amount_yi, "advances": advances,
                      "unchanged": unchanged, "declines": declines}
            missing = [key for key in _FIELDS if (day, key) not in trusted]
            if not missing:
                continue
            method = ("source=tdx-cn-v2; historical_silver_recovery; "
                      f"coverage={coverage}; paired={paired}; amount_unit=CNY")
            for key in missing:
                name, source_key = _FIELDS[key]
                metrics.append(_metric(
                    "A_SHARE_BREADTH", "CN.A_SHARE.BREADTH", day, "1d", name,
                    values[source_key], "已通过 tdx-cn-v2 质量门的本机日线；涨跌与前一有效收盘价比较",
                    method, metric_key=key,
                ))
            recovered_days.append(day)
        if metrics:
            run_id = store.begin_run("collector:tdx-cn-v2-breadth-recovery")
            try:
                store.upsert_gold_metrics(metrics)
                store.finish_run(run_id, "COMPLETE", f"恢复 {len(recovered_days)} 日、{len(metrics)} 项")
            except Exception:
                store.finish_run(run_id, "FAILED", "A 股历史宽度补算失败")
                raise
        return {"recoveredDays": recovered_days, "metricCount": len(metrics)}
    finally:
        store.close()
