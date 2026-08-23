"""Local multi-account analysis storage and deterministic performance metrics.

The account snapshot is authoritative.  Fills and cash flows are preserved for
attribution, FIFO close matching and reconciliation; they never silently
replace a user-entered end-of-day account equity.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
ANNUAL_TRADING_DAYS = 250


class AccountError(ValueError):
    """A stable user-facing error for local account data."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value: Any) -> Any:
    """Small standalone JSON-safe cleaner; avoid a web router import cycle."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def _database_path(data_root: Path) -> Path:
    return data_root / "personal" / "accounts.sqlite"


@contextmanager
def _connect(data_root: Path) -> Iterator[sqlite3.Connection]:
    path = _database_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        _schema(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            start_date TEXT NOT NULL, initial_equity REAL NOT NULL,
            risk_free_rate REAL NOT NULL DEFAULT 0.02,
            benchmark_instrument_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            deleted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
            day TEXT NOT NULL, equity REAL NOT NULL, cash REAL, market_value REAL,
            margin_used REAL, note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(account_id, day)
        );
        CREATE TABLE IF NOT EXISTS account_cashflows (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
            occurred_at TEXT NOT NULL, kind TEXT NOT NULL, amount REAL NOT NULL,
            note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS account_fills (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
            instrument_id TEXT NOT NULL, instrument_name TEXT, direction TEXT NOT NULL,
            position_effect TEXT NOT NULL, occurred_at TEXT NOT NULL, quantity REAL NOT NULL,
            price REAL NOT NULL, contract_multiplier REAL NOT NULL DEFAULT 1,
            fee REAL NOT NULL DEFAULT 0, strategy_id TEXT, note TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS account_strategy_uses (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
            strategy_id TEXT NOT NULL, strategy_name TEXT, start_date TEXT NOT NULL,
            end_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snapshot_account_day ON account_snapshots(account_id, day);
        CREATE INDEX IF NOT EXISTS idx_cashflow_account_time ON account_cashflows(account_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_fill_account_time ON account_fills(account_id, occurred_at);
        """
    )


def _as_iso_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise AccountError("时间格式必须为 ISO 日期或日期时间") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _day(value: Any) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except ValueError as error:
        raise AccountError("日期必须为 YYYY-MM-DD") from error


def _number(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise AccountError(f"{field} 必须为数值")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise AccountError(f"{field} 必须为数值") from error
    if not math.isfinite(parsed) or (positive and parsed <= 0) or (nonnegative and parsed < 0):
        qualifier = "正数" if positive else "非负数" if nonnegative else "有限数值"
        raise AccountError(f"{field} 必须为{qualifier}")
    return parsed


def _account_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "startDate": row["start_date"],
        "initialEquity": row["initial_equity"], "riskFreeRate": row["risk_free_rate"],
        "benchmarkInstrumentId": row["benchmark_instrument_id"], "createdAt": row["created_at"],
        "updatedAt": row["updated_at"], "deletedAt": row["deleted_at"],
    }


def list_accounts(data_root: Path, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    with _connect(data_root) as connection:
        condition = "" if include_deleted else "WHERE deleted_at IS NULL"
        rows = connection.execute(f"SELECT * FROM accounts {condition} ORDER BY updated_at DESC, name COLLATE NOCASE").fetchall()
    return clean([_account_row(row) for row in rows])


def get_account(data_root: Path, account_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
    with _connect(data_root) as connection:
        row = connection.execute("SELECT * FROM accounts WHERE id = ?", [account_id]).fetchone()
    if row is None or (row["deleted_at"] and not include_deleted):
        raise AccountError("账户不存在")
    return clean(_account_row(row))


def create_account(data_root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 80:
        raise AccountError("账户名称长度必须为 1 至 80")
    start_date = _day(payload.get("startDate"))
    initial_equity = _number(payload.get("initialEquity"), "期初资金", positive=True)
    risk_free_rate = _number(payload.get("riskFreeRate", 0.02), "无风险利率")
    if not -1 < risk_free_rate < 1:
        raise AccountError("无风险利率必须在 -100% 至 100% 之间")
    account_id = f"acct_{uuid.uuid4().hex[:16]}"
    timestamp = now_iso()
    benchmark = str(payload.get("benchmarkInstrumentId") or "").strip() or None
    with _connect(data_root) as connection:
        try:
            connection.execute(
                "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                [account_id, name, start_date, initial_equity, risk_free_rate, benchmark, timestamp, timestamp],
            )
            connection.execute(
                "INSERT INTO account_snapshots VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)",
                [f"snap_{uuid.uuid4().hex[:16]}", account_id, start_date, initial_equity, "期初资金", timestamp, timestamp],
            )
        except sqlite3.IntegrityError as error:
            raise AccountError("账户名称已存在") from error
    return get_account(data_root, account_id)


def update_account(data_root: Path, account_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    current = get_account(data_root, account_id)
    name = str(payload.get("name", current["name"]) or "").strip()
    if not name or len(name) > 80:
        raise AccountError("账户名称长度必须为 1 至 80")
    start_date = _day(payload.get("startDate", current["startDate"]))
    initial = _number(payload.get("initialEquity", current["initialEquity"]), "期初资金", positive=True)
    risk_free = _number(payload.get("riskFreeRate", current["riskFreeRate"]), "无风险利率")
    if not -1 < risk_free < 1:
        raise AccountError("无风险利率必须在 -100% 至 100% 之间")
    benchmark = str(payload.get("benchmarkInstrumentId", current["benchmarkInstrumentId"]) or "").strip() or None
    with _connect(data_root) as connection:
        try:
            connection.execute(
                "UPDATE accounts SET name=?, start_date=?, initial_equity=?, risk_free_rate=?, benchmark_instrument_id=?, updated_at=? WHERE id=? AND deleted_at IS NULL",
                [name, start_date, initial, risk_free, benchmark, now_iso(), account_id],
            )
            changed = connection.execute(
                "UPDATE account_snapshots SET equity=?, note=?, updated_at=? WHERE account_id=? AND day=?",
                [initial, "期初资金", now_iso(), account_id, start_date],
            ).rowcount
            if not changed:
                timestamp = now_iso()
                connection.execute(
                    "INSERT INTO account_snapshots VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)",
                    [f"snap_{uuid.uuid4().hex[:16]}", account_id, start_date, initial, "期初资金", timestamp, timestamp],
                )
        except sqlite3.IntegrityError as error:
            raise AccountError("账户名称已存在") from error
    return get_account(data_root, account_id)


def soft_delete_account(data_root: Path, account_id: str) -> None:
    get_account(data_root, account_id)
    with _connect(data_root) as connection:
        connection.execute("UPDATE accounts SET deleted_at=?, updated_at=? WHERE id=?", [now_iso(), now_iso(), account_id])


def restore_account(data_root: Path, account_id: str) -> dict[str, Any]:
    with _connect(data_root) as connection:
        changed = connection.execute("UPDATE accounts SET deleted_at=NULL, updated_at=? WHERE id=? AND deleted_at IS NOT NULL", [now_iso(), account_id]).rowcount
    if not changed:
        raise AccountError("回收区中没有该账户")
    return get_account(data_root, account_id)


def purge_deleted(data_root: Path, *, now: datetime | None = None) -> int:
    cutoff = ((now or datetime.now(timezone.utc)).timestamp() - 30 * 86400)
    deleted: list[str] = []
    with _connect(data_root) as connection:
        rows = connection.execute("SELECT id, deleted_at FROM accounts WHERE deleted_at IS NOT NULL").fetchall()
        deleted = [row["id"] for row in rows if _parse_time(row["deleted_at"]).timestamp() < cutoff]
        for account_id in deleted:
            connection.execute("DELETE FROM accounts WHERE id=?", [account_id])
    return len(deleted)


def _parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _upsert(
    data_root: Path, table: str, account_id: str, payload: Mapping[str, Any], row_id: str | None = None,
) -> dict[str, Any]:
    get_account(data_root, account_id)
    timestamp = now_iso()
    row_id = row_id or f"{table[8:-1] if table.startswith('account_') else table}_{uuid.uuid4().hex[:16]}"
    with _connect(data_root) as connection:
        owner = connection.execute(f"SELECT account_id FROM {table} WHERE id=?", [row_id]).fetchone()
        if owner is not None and owner["account_id"] != account_id:
            raise AccountError("记录不属于当前账户")
        if table == "account_snapshots":
            day = _day(payload.get("day"))
            equity = _number(payload.get("equity"), "总资产", positive=True)
            cash = _number(payload["cash"], "现金") if payload.get("cash") is not None else None
            market_value = _number(payload["marketValue"], "持仓市值") if payload.get("marketValue") is not None else None
            margin_used = _number(payload["marginUsed"], "占用保证金", nonnegative=True) if payload.get("marginUsed") is not None else None
            note = str(payload.get("note") or "")[:1000] or None
            existing = connection.execute("SELECT id, created_at FROM account_snapshots WHERE account_id=? AND day=?", [account_id, day]).fetchone()
            chosen_id = existing["id"] if existing else row_id
            row_id = chosen_id
            created = existing["created_at"] if existing else timestamp
            connection.execute(
                "INSERT OR REPLACE INTO account_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [chosen_id, account_id, day, equity, cash, market_value, margin_used, note, created, timestamp],
            )
        elif table == "account_cashflows":
            occurred = _as_iso_datetime(payload.get("occurredAt"))
            kind = str(payload.get("kind") or "").upper()
            if kind not in {"DEPOSIT", "WITHDRAWAL"}:
                raise AccountError("资金流水类型必须为 DEPOSIT 或 WITHDRAWAL")
            amount = _number(payload.get("amount"), "金额", positive=True)
            connection.execute(
                "INSERT OR REPLACE INTO account_cashflows VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [row_id, account_id, occurred, kind, amount, str(payload.get("note") or "")[:1000] or None, timestamp, timestamp],
            )
        elif table == "account_fills":
            direction = str(payload.get("direction") or "").upper()
            effect = str(payload.get("positionEffect") or "").upper()
            if direction not in {"LONG", "SHORT"} or effect not in {"OPEN", "CLOSE"}:
                raise AccountError("成交方向必须为 LONG/SHORT，开平必须为 OPEN/CLOSE")
            instrument_id = str(payload.get("instrumentId") or "").strip()
            if not instrument_id:
                raise AccountError("成交必须指定标的")
            connection.execute(
                "INSERT OR REPLACE INTO account_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [row_id, account_id, instrument_id, str(payload.get("instrumentName") or "")[:160] or None, direction, effect,
                 _as_iso_datetime(payload.get("occurredAt")), _number(payload.get("quantity"), "数量/手数", positive=True),
                 _number(payload.get("price"), "成交价", positive=True), _number(payload.get("contractMultiplier", 1), "合约乘数", positive=True),
                 _number(payload.get("fee", 0), "手续费", nonnegative=True), str(payload.get("strategyId") or "")[:128] or None,
                 str(payload.get("note") or "")[:1000] or None, timestamp, timestamp],
            )
        elif table == "account_strategy_uses":
            strategy_id = str(payload.get("strategyId") or "").strip()
            if not strategy_id:
                raise AccountError("策略使用记录必须指定策略")
            start = _day(payload.get("startDate"))
            end = _day(payload["endDate"]) if payload.get("endDate") else None
            if end and end < start:
                raise AccountError("策略结束日期不能早于开始日期")
            connection.execute(
                "INSERT OR REPLACE INTO account_strategy_uses VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [row_id, account_id, strategy_id, str(payload.get("strategyName") or "")[:160] or None, start, end, timestamp, timestamp],
            )
        else:
            raise AssertionError(table)
        connection.execute("UPDATE accounts SET updated_at=? WHERE id=?", [timestamp, account_id])
    records = list_records(data_root, account_id, table, include_all=True)
    for record in records:
        if record.get("id") == row_id:
            return record
    raise AccountError("保存后的记录无法读取")


def upsert_snapshot(data_root: Path, account_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return _upsert(data_root, "account_snapshots", account_id, payload, str(payload.get("id") or "") or None)


def upsert_cashflow(data_root: Path, account_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return _upsert(data_root, "account_cashflows", account_id, payload, str(payload.get("id") or "") or None)


def upsert_fill(data_root: Path, account_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return _upsert(data_root, "account_fills", account_id, payload, str(payload.get("id") or "") or None)


def upsert_strategy_use(data_root: Path, account_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return _upsert(data_root, "account_strategy_uses", account_id, payload, str(payload.get("id") or "") or None)


def list_records(data_root: Path, account_id: str, table: str, *, include_all: bool = False) -> list[dict[str, Any]]:
    get_account(data_root, account_id)
    allowed = {"account_snapshots", "account_cashflows", "account_fills", "account_strategy_uses"}
    if table not in allowed:
        raise AccountError("未知账户记录类型")
    sort = {"account_snapshots": "day", "account_cashflows": "occurred_at", "account_fills": "occurred_at", "account_strategy_uses": "start_date"}[table]
    with _connect(data_root) as connection:
        rows = connection.execute(f"SELECT * FROM {table} WHERE account_id=? ORDER BY {sort}, id", [account_id]).fetchall()
    mappings: dict[str, dict[str, str]] = {
        "account_snapshots": {"account_id": "accountId", "market_value": "marketValue", "margin_used": "marginUsed", "created_at": "createdAt", "updated_at": "updatedAt"},
        "account_cashflows": {"account_id": "accountId", "occurred_at": "occurredAt", "created_at": "createdAt", "updated_at": "updatedAt"},
        "account_fills": {"account_id": "accountId", "instrument_id": "instrumentId", "instrument_name": "instrumentName", "position_effect": "positionEffect", "occurred_at": "occurredAt", "contract_multiplier": "contractMultiplier", "strategy_id": "strategyId", "created_at": "createdAt", "updated_at": "updatedAt"},
        "account_strategy_uses": {"account_id": "accountId", "strategy_id": "strategyId", "strategy_name": "strategyName", "start_date": "startDate", "end_date": "endDate", "created_at": "createdAt", "updated_at": "updatedAt"},
    }
    result = []
    for row in rows:
        item = dict(row)
        for source, target in mappings[table].items():
            item[target] = item.pop(source)
        result.append(item)
    return clean(result)


def delete_record(data_root: Path, account_id: str, table: str, row_id: str) -> None:
    get_account(data_root, account_id)
    if table not in {"account_snapshots", "account_cashflows", "account_fills", "account_strategy_uses"}:
        raise AccountError("未知账户记录类型")
    with _connect(data_root) as connection:
        changed = connection.execute(f"DELETE FROM {table} WHERE id=? AND account_id=?", [row_id, account_id]).rowcount
        connection.execute("UPDATE accounts SET updated_at=? WHERE id=?", [now_iso(), account_id])
    if not changed:
        raise AccountError("记录不存在")


def _cashflows_by_day(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        occurred = _parse_time(row["occurredAt"]).astimezone(BEIJING)
        amount = float(row["amount"])
        grouped[occurred.date().isoformat()].append({"amount": amount if row["kind"] == "DEPOSIT" else -amount, "time": occurred})
    return grouped


def _benchmark_closes(data_root: Path, instrument_id: str | None) -> dict[str, float]:
    if not instrument_id:
        return {}
    # Imported lazily to keep ``web_api.stats`` -> this module acyclic.
    from market_monitor.web_api.common import read_bars

    bars = read_bars(data_root, instrument_id, period="1d", limit=5_000)
    output: dict[str, float] = {}
    for bar in bars:
        close = bar.get("close")
        try:
            price = float(close)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            output[str(bar.get("trading_day") or bar.get("trading_date") or str(bar.get("bar_open_time") or "")[:10])] = price
    return output


def _last_not_after(values: Mapping[str, float], day: str) -> float | None:
    available = [key for key in values if key <= day]
    return values[max(available)] if available else None


def _closed_fill_pnl(fills: Sequence[Mapping[str, Any]]) -> list[float]:
    lots: dict[tuple[str, str], deque[dict[str, float]]] = defaultdict(deque)
    closed: list[float] = []
    for row in sorted(fills, key=lambda item: (str(item["occurredAt"]), str(item["id"]))):
        key = (str(row["instrumentId"]), str(row["direction"]))
        quantity = float(row["quantity"])
        price = float(row["price"])
        multiplier = float(row["contractMultiplier"])
        fee = float(row["fee"])
        if row["positionEffect"] == "OPEN":
            lots[key].append({"quantity": quantity, "price": price, "multiplier": multiplier, "fee": fee})
            continue
        remaining = quantity
        while remaining > 1e-12 and lots[key]:
            lot = lots[key][0]
            used = min(remaining, lot["quantity"])
            entry_fee = lot["fee"] * used / lot["quantity"]
            exit_fee = fee * used / quantity
            raw = (price - lot["price"]) * used * multiplier if row["direction"] == "LONG" else (lot["price"] - price) * used * multiplier
            closed.append(raw - entry_fee - exit_fee)
            lot["quantity"] -= used
            remaining -= used
            if lot["quantity"] <= 1e-12:
                lots[key].popleft()
    return closed


def _metric(value: float | None, reason: str | None = None) -> dict[str, Any]:
    return {"value": value, "reason": reason if value is None else None}


def analyze_account(data_root: Path, account_id: str, *, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    account = get_account(data_root, account_id)
    snapshots = list_records(data_root, account_id, "account_snapshots")
    if not snapshots:
        return {"available": False, "reason": "缺少每日账户快照", "account": account, "metrics": {}, "series": {}}
    start_day = _day(start or account["startDate"])
    end_day = _day(end or snapshots[-1]["day"])
    rows = [item for item in snapshots if start_day <= item["day"] <= end_day]
    if not rows:
        return {"available": False, "reason": "所选日期范围没有账户快照", "account": account, "metrics": {}, "series": {}}
    cashflows = list_records(data_root, account_id, "account_cashflows")
    fills = list_records(data_root, account_id, "account_fills")
    flows = _cashflows_by_day(cashflows)
    nav = 1.0
    daily_returns: list[float] = []
    curve: list[dict[str, Any]] = []
    benchmark = _benchmark_closes(data_root, account.get("benchmarkInstrumentId"))
    benchmark_start = _last_not_after(benchmark, rows[0]["day"])
    peak = nav
    max_drawdown = 0.0
    cumulative_deposit = 0.0
    cumulative_withdrawal = 0.0
    cumulative_fee = 0.0
    fees_by_day: dict[str, float] = defaultdict(float)
    for fill in fills:
        fees_by_day[_parse_time(fill["occurredAt"]).astimezone(BEIJING).date().isoformat()] += float(fill["fee"])
    for index, row in enumerate(rows):
        equity = float(row["equity"])
        # 开始日的出入金已经包含在用户给出的期初总资产，不能重复扣除。
        day_flows = [] if index == 0 else flows.get(row["day"], [])
        if index:
            previous = float(rows[index - 1]["equity"])
            end_of_day = datetime.combine(date.fromisoformat(row["day"]), time.max, tzinfo=BEIJING)
            weights = [max(0.0, min(1.0, (end_of_day - item["time"]).total_seconds() / 86_400)) for item in day_flows]
            net = sum(item["amount"] for item in day_flows)
            denominator = previous + sum(weight * item["amount"] for weight, item in zip(weights, day_flows))
            daily = (equity - previous - net) / denominator if denominator > 0 else None
            if daily is not None and math.isfinite(daily):
                nav *= 1 + daily
                daily_returns.append(daily)
        for item in day_flows:
            if item["amount"] >= 0:
                cumulative_deposit += item["amount"]
            else:
                cumulative_withdrawal += -item["amount"]
        cumulative_fee += fees_by_day.get(row["day"], 0.0)
        peak = max(peak, nav)
        max_drawdown = min(max_drawdown, nav / peak - 1 if peak else 0.0)
        benchmark_price = _last_not_after(benchmark, row["day"])
        benchmark_return = benchmark_price / benchmark_start - 1 if benchmark_price and benchmark_start else None
        curve.append({
            "t": row["day"], "equity": equity, "nav": nav, "dailyReturn": daily_returns[-1] if index and daily_returns else None,
            "cumulativePnl": equity - float(rows[0]["equity"]) - (cumulative_deposit - cumulative_withdrawal),
            "deposits": cumulative_deposit, "withdrawals": cumulative_withdrawal, "fees": cumulative_fee,
            "return": nav - 1, "benchmarkReturn": benchmark_return,
            "excessReturn": nav - 1 - benchmark_return if benchmark_return is not None else None,
            "drawdown": max_drawdown, "capitalUtilization": ((abs(float(row["marketValue"] or 0)) + float(row["marginUsed"] or 0)) / equity) if equity else None,
            "riskFreeRate": float(account["riskFreeRate"]),
        })
    trading_days = len(rows)
    start_equity, end_equity = float(rows[0]["equity"]), float(rows[-1]["equity"])
    cumulative_pnl = end_equity - start_equity - (cumulative_deposit - cumulative_withdrawal)
    annual_return = nav ** (ANNUAL_TRADING_DAYS / trading_days) - 1 if nav > 0 and trading_days else None
    annual_volatility = math.sqrt(ANNUAL_TRADING_DAYS) * statistics.stdev(daily_returns) if len(daily_returns) >= 2 else None
    risk_free = float(account["riskFreeRate"])
    benchmark_end = _last_not_after(benchmark, rows[-1]["day"])
    benchmark_return = benchmark_end / benchmark_start - 1 if benchmark_end and benchmark_start else None
    benchmark_daily: list[float] = []
    for previous, current in zip(rows, rows[1:]):
        first, second = _last_not_after(benchmark, previous["day"]), _last_not_after(benchmark, current["day"])
        if first and second and first > 0:
            benchmark_daily.append(second / first - 1)
    beta: float | None = None
    if len(benchmark_daily) == len(daily_returns) and len(daily_returns) >= 2:
        variance = statistics.variance(benchmark_daily)
        if variance > 0:
            beta = statistics.covariance(daily_returns, benchmark_daily) / variance
    benchmark_annual = (1 + benchmark_return) ** (ANNUAL_TRADING_DAYS / trading_days) - 1 if benchmark_return is not None and benchmark_return > -1 else None
    closed = _closed_fill_pnl(fills)
    winners = [value for value in closed if value > 0]
    losers = [value for value in closed if value < 0]
    profit_ratio = (statistics.mean(winners) / abs(statistics.mean(losers))) if winners and losers else None
    downside = [min(value - risk_free / ANNUAL_TRADING_DAYS, 0.0) for value in daily_returns]
    downside_risk = math.sqrt(ANNUAL_TRADING_DAYS) * statistics.stdev(downside) if len(downside) >= 2 else None
    active = [left - right for left, right in zip(daily_returns, benchmark_daily)] if len(benchmark_daily) == len(daily_returns) else []
    tracking = math.sqrt(ANNUAL_TRADING_DAYS) * statistics.stdev(active) if len(active) >= 2 else None
    metrics = {
        "initialFunds": _metric(start_equity), "endingFunds": _metric(end_equity), "cumulativePnl": _metric(cumulative_pnl),
        "cumulativeDeposits": _metric(cumulative_deposit), "cumulativeWithdrawals": _metric(cumulative_withdrawal), "cumulativeFees": _metric(cumulative_fee),
        "cumulativeReturn": _metric(nav - 1), "benchmarkReturn": _metric(benchmark_return, "未选择基准或缺少本地基准行情"),
        "excessReturn": _metric((nav - 1 - benchmark_return) if benchmark_return is not None else None, "缺少基准收益率"),
        "annualReturn": _metric(annual_return, "净值或交易天数不足"), "annualVolatility": _metric(annual_volatility, "至少需要两期日收益率"),
        "maxDrawdown": _metric(abs(max_drawdown)), "winRate": _metric(len(winners) / (len(winners) + len(losers)) if winners or losers else None, "缺少已平仓盈亏"),
        "profitLossRatio": _metric(profit_ratio, "需要同时存在盈利和平亏损平仓"),
        "alpha": _metric(annual_return - risk_free - beta * (benchmark_annual - risk_free) if annual_return is not None and beta is not None and benchmark_annual is not None else None, "缺少基准或 Beta"),
        "beta": _metric(beta, "至少需要两期对齐且基准有波动的日收益率"),
        "sharpe": _metric((annual_return - risk_free) / annual_volatility if annual_return is not None and annual_volatility else None, "年化波动率不可计算或为零"),
        "calmar": _metric(annual_return / abs(max_drawdown) if annual_return is not None and max_drawdown < 0 else None, "最大回撤为零或年化收益不可计算"),
        "sortino": _metric((annual_return - risk_free) / downside_risk if annual_return is not None and downside_risk else None, "下行风险不可计算或为零"),
        "information": _metric((annual_return - benchmark_annual) / tracking if annual_return is not None and benchmark_annual is not None and tracking else None, "跟踪误差不可计算或为零"),
        "treynor": _metric((annual_return - risk_free) / beta if annual_return is not None and beta else None, "Beta 不可计算或为零"),
        "riskFreeRate": _metric(risk_free), "tradingDays": _metric(float(trading_days)),
        "tradingFrequency": _metric(((date.fromisoformat(rows[-1]["day"]) - date.fromisoformat(rows[0]["day"])).days + 1) / trading_days if trading_days else None),
        "capitalUtilization": _metric(curve[-1]["capitalUtilization"]), "cumulativeNav": _metric(nav),
        "profitDays": _metric(float(sum(1 for value in daily_returns if value > 0))),
    }
    return clean({
        "available": True, "account": account, "startDate": rows[0]["day"], "endDate": rows[-1]["day"],
        "metrics": metrics, "series": curve, "snapshots": rows, "fills": fills,
        "cashflows": cashflows, "strategyUses": list_records(data_root, account_id, "account_strategy_uses"),
        "reconciliation": {"authoritative": "snapshots", "maxDrawdownMode": "收盘净值（分钟估算待行情覆盖）"},
    })


__all__ = (
    "AccountError", "analyze_account", "create_account", "delete_record", "get_account", "list_accounts",
    "list_records", "purge_deleted", "restore_account", "soft_delete_account", "update_account", "upsert_cashflow",
    "upsert_fill", "upsert_snapshot", "upsert_strategy_use",
)
