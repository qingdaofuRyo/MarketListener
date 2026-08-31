"""Controlled snapshots of date-effective Chinese futures contract rules.

The synchronizer deliberately separates three concerns:

* local Silver decides which trading dates and commodity products are in scope;
* ``akshare.futures_rule`` supplies the rule table for each exact date;
* consumers resolve only an exact snapshot date, never a guessed prior rule.

The provider table is parsed by its documented positional contract because its
Chinese column labels have changed encoding across upstream releases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

import duckdb


SNAPSHOT_SCHEMA = "market-listener/futures-contract-rule-snapshots/v1"
SNAPSHOT_PROVIDER = "akshare.futures_rule"
SNAPSHOT_RELATIVE_PATH = Path("state") / "futures_contract_rule_snapshots.json"

_CODE_POSITION = 2
_MARGIN_PERCENT_POSITION = 3
_MULTIPLIER_POSITION = 5
_OTHER_MARGIN_POSITION = 8
_MIN_PROVIDER_COLUMNS = _OTHER_MARGIN_POSITION + 1
_PRODUCT = re.compile(r"^[A-Z]+$")
_PRODUCT_FROM_SYMBOL = re.compile(r"^([A-Z]+)")
_OPTION_CODE = re.compile(r"(?:_O$|OPTION$)", re.I)
_CONTRACT_MARGIN = re.compile(
    r"(?P<contract>[A-Za-z]{1,3}\d{3,4})\s*合约\s*"
    r"交易保证金(?:比例)?(?:调整)?为\s*"
    r"(?P<percent>\d+(?:\.\d+)?)\s*[%％]",
    re.I,
)

RuleFetcher = Callable[[str], Any]


@dataclass(frozen=True)
class FuturesContractRule:
    """One exact-day contract rule returned by :class:`RuleBook`."""

    trading_day: date
    exchange: str
    product_code: str
    contract_code: str | None
    contract_multiplier: float
    margin_rate: float
    source: str


class RuleBook:
    """Read-only exact-day rule snapshots.

    A missing day, product, or malformed contract never falls back to the most
    recent known value.  That is important for historical replay: unavailable
    evidence must remain unavailable rather than being silently invented.
    """

    def __init__(self, days: Mapping[str, Mapping[str, Any]]) -> None:
        copied: dict[str, Mapping[str, Any]] = {}
        for day_text, snapshot in days.items():
            products = {
                str(key): MappingProxyType(dict(value))
                for key, value in _mapping(snapshot.get("products")).items()
                if isinstance(value, Mapping)
            }
            overrides = {
                str(key): MappingProxyType(dict(value))
                for key, value in _mapping(snapshot.get("contractOverrides")).items()
                if isinstance(value, Mapping)
            }
            copied[str(day_text)] = MappingProxyType(
                {
                    "products": MappingProxyType(products),
                    "contractOverrides": MappingProxyType(overrides),
                }
            )
        self._days = MappingProxyType(copied)

    @property
    def trading_days(self) -> tuple[str, ...]:
        return tuple(sorted(self._days))

    def resolve(
        self,
        trading_day: date | str,
        exchange: str,
        product_code: str,
        contract_code: str | None = None,
    ) -> FuturesContractRule | None:
        """Resolve one rule from the exact requested trading-day snapshot."""

        day = _parse_day(trading_day)
        if day is None:
            return None
        venue = str(exchange or "").strip().upper()
        product = str(product_code or "").strip().upper()
        if not venue or not _PRODUCT.fullmatch(product) or venue == "CFFEX":
            return None
        snapshot = self._days.get(day.isoformat())
        if snapshot is None:
            return None
        product_key = f"{venue}.{product}"
        base = snapshot["products"].get(product_key)
        if not isinstance(base, Mapping):
            return None
        multiplier = _positive_float(base.get("contractMultiplier"))
        margin_rate = _rate(base.get("marginRate"))
        if multiplier is None or margin_rate is None:
            return None

        normalized_contract = _normalize_contract(contract_code)
        source = "BASE"
        if normalized_contract is not None:
            override = snapshot["contractOverrides"].get(
                f"{venue}.{normalized_contract}"
            )
            if isinstance(override, Mapping):
                overridden_rate = _rate(override.get("marginRate"))
                if overridden_rate is not None:
                    margin_rate = overridden_rate
                    source = "CONTRACT_OVERRIDE"
        return FuturesContractRule(
            trading_day=day,
            exchange=venue,
            product_code=product,
            contract_code=normalized_contract,
            contract_multiplier=multiplier,
            margin_rate=margin_rate,
            source=source,
        )

    def resolve_multiplier(
        self,
        trading_day: date | str,
        exchange: str,
        product_code: str,
    ) -> float | None:
        """Return only an exact-day contract multiplier for notional valuation.

        Notional open-interest value requires the multiplier but not a margin
        rate.  Keeping this lookup separate prevents a missing historical
        margin field from incorrectly suppressing an otherwise auditable
        settlement-value observation.
        """

        day = _parse_day(trading_day)
        if day is None:
            return None
        venue = str(exchange or "").strip().upper()
        product = str(product_code or "").strip().upper()
        if not venue or not _PRODUCT.fullmatch(product) or venue == "CFFEX":
            return None
        snapshot = self._days.get(day.isoformat())
        if snapshot is None:
            return None
        base = snapshot["products"].get(f"{venue}.{product}")
        return _positive_float(base.get("contractMultiplier")) if isinstance(base, Mapping) else None


def load_rule_book(data_root: Path) -> RuleBook:
    """Load an immutable rule book; a missing snapshot file is an empty book."""

    path = Path(data_root) / SNAPSHOT_RELATIVE_PATH
    if not path.exists():
        return RuleBook({})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(f"invalid futures rule snapshot: {path}") from error
    _validate_snapshot_envelope(payload)
    return RuleBook(_mapping(payload.get("days")))


def sync_futures_rule_snapshots(
    data_root: Path,
    *,
    lookback_days: int = 10,
    fetcher: RuleFetcher | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    """Fetch and atomically merge missing rule snapshots for recent Silver days.

    Existing day snapshots are immutable and retained.  Consequently a repeat
    run over the same Silver window performs no provider calls and no write.
    """

    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
        raise ValueError("lookback_days must be an integer")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be > 0")
    root = Path(data_root)
    inventory = _recent_silver_inventory(root, lookback_days)
    path = root / SNAPSHOT_RELATIVE_PATH
    payload = _load_snapshot_for_merge(path)
    existing_days: dict[str, dict[str, Any]] = {}
    metadata_updated = False
    envelope_retrieved_at = str(payload.get("retrievedAt") or "")
    for day_text, raw_snapshot in _mapping(payload.get("days")).items():
        snapshot = dict(_mapping(raw_snapshot))
        if "provider" not in snapshot:
            snapshot["provider"] = SNAPSHOT_PROVIDER
            metadata_updated = True
        if "retrievedAt" not in snapshot:
            snapshot["retrievedAt"] = envelope_retrieved_at
            metadata_updated = True
        existing_days[str(day_text)] = snapshot
    pending_days = [day for day in inventory if day not in existing_days]
    if not inventory:
        return _sync_result(path, inventory, (), 0, 0, "NO_DATA")
    if not pending_days and not metadata_updated:
        return _sync_result(path, inventory, (), 0, 0, "UNCHANGED")
    if not pending_days:
        migrated = dict(payload)
        migrated["days"] = {day: existing_days[day] for day in sorted(existing_days)}
        _atomic_write_json(path, migrated)
        return _sync_result(path, inventory, (), 0, 0, "UPDATED")

    rule_fetcher = fetcher or _akshare_futures_rule
    timestamp = retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    additions: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    product_count = 0
    override_count = 0
    for day_text in pending_days:
        try:
            table = rule_fetcher(day_text.replace("-", ""))
            parsed = _parse_provider_table(table, inventory[day_text])
        except Exception as error:  # Provider failures must not discard other exact-day evidence.
            failures.append(
                {
                    "tradingDay": day_text,
                    "reason": f"{type(error).__name__}: {str(error)[:200]}",
                }
            )
            continue
        if not parsed["products"]:
            failures.append(
                {
                    "tradingDay": day_text,
                    "reason": "provider returned no matching product rules",
                }
            )
            continue
        parsed["provider"] = SNAPSHOT_PROVIDER
        parsed["retrievedAt"] = timestamp
        additions[day_text] = parsed
        product_count += len(parsed["products"])
        override_count += len(parsed["contractOverrides"])

    if not additions:
        if metadata_updated:
            migrated = dict(payload)
            migrated["days"] = {day: existing_days[day] for day in sorted(existing_days)}
            _atomic_write_json(path, migrated)
        return _sync_result(
            path,
            inventory,
            (),
            0,
            0,
            "FAILED",
            failures=failures,
        )

    merged_days = dict(existing_days)
    merged_days.update(additions)
    merged = {
        "schema": SNAPSHOT_SCHEMA,
        "provider": SNAPSHOT_PROVIDER,
        "retrievedAt": timestamp,
        "days": {day: merged_days[day] for day in sorted(merged_days)},
    }
    _atomic_write_json(path, merged)
    return _sync_result(
        path,
        inventory,
        additions,
        product_count,
        override_count,
        "PARTIAL" if failures else "UPDATED",
        failures=failures,
    )


def _recent_silver_inventory(
    data_root: Path, lookback_days: int
) -> dict[str, dict[str, str]]:
    base = (
        data_root
        / "silver"
        / "market=CN"
        / "asset_type=FUTURE"
        / "period=1d"
    )
    files = sorted(base.rglob("*.parquet"))
    if not files:
        return {}
    connection = duckdb.connect(":memory:")
    try:
        rows = connection.execute(
            "SELECT DISTINCT "
            "substr(coalesce(json_extract_string(bar_json, '$.trading_date'), "
            "json_extract_string(bar_json, '$.trading_day'), ''), 1, 10) AS day, "
            "upper(coalesce(json_extract_string(bar_json, '$.exchange'), "
            "json_extract_string(bar_json, '$.instrument_key.exchange'), "
            "split_part(instrument_id, '.', 2), '')) AS exchange, "
            "upper(coalesce(json_extract_string(bar_json, '$.product_code'), "
            "json_extract_string(bar_json, '$.productCode'), "
            "regexp_extract(coalesce(json_extract_string(bar_json, '$.symbol'), "
            "json_extract_string(bar_json, '$.contract_code'), ''), "
            "'^([A-Za-z]+)', 1), '')) AS product "
            "FROM read_parquet(?, union_by_name=true)",
            [[str(path) for path in files]],
        ).fetchall()
    finally:
        connection.close()

    by_day: dict[str, dict[str, str]] = {}
    for raw_day, raw_exchange, raw_product in rows:
        parsed_day = _parse_day(raw_day)
        if parsed_day is None or parsed_day.weekday() >= 5:
            continue
        exchange = str(raw_exchange or "").strip().upper()
        product = str(raw_product or "").strip().upper()
        if exchange == "CFFEX" or not exchange or not _PRODUCT.fullmatch(product):
            continue
        current = by_day.setdefault(parsed_day.isoformat(), {})
        known_exchange = current.get(product)
        if known_exchange is None:
            current[product] = exchange
        elif known_exchange != exchange:
            raise ValueError(
                f"ambiguous Silver product exchange on {parsed_day}: {product}"
            )
    effective_days = sorted((day for day, products in by_day.items() if products))
    selected = set(effective_days[-lookback_days:])
    return {
        day: dict(sorted(by_day[day].items()))
        for day in effective_days
        if day in selected
    }


def _parse_provider_table(
    table: Any, product_exchange: Mapping[str, str]
) -> dict[str, Any]:
    products: dict[str, dict[str, float]] = {}
    overrides: dict[str, dict[str, float]] = {}
    for row in _positional_rows(table):
        if len(row) < _MIN_PROVIDER_COLUMNS:
            continue
        code = str(row[_CODE_POSITION] or "").strip().upper()
        if not code or _OPTION_CODE.search(code) or not _PRODUCT.fullmatch(code):
            continue
        exchange = product_exchange.get(code)
        if exchange is None or exchange == "CFFEX":
            continue
        margin_rate = _percent_rate(row[_MARGIN_PERCENT_POSITION])
        multiplier = _positive_float(row[_MULTIPLIER_POSITION])
        if margin_rate is None or multiplier is None:
            continue
        product_key = f"{exchange}.{code}"
        products.setdefault(
            product_key,
            {"contractMultiplier": multiplier, "marginRate": margin_rate},
        )
        other_margin = row[_OTHER_MARGIN_POSITION]
        for match in _CONTRACT_MARGIN.finditer(_plain_text(other_margin)):
            contract = match.group("contract").upper()
            contract_product = _PRODUCT_FROM_SYMBOL.match(contract)
            if contract_product is None or contract_product.group(1) != code:
                continue
            override_rate = _percent_rate(match.group("percent"))
            if override_rate is not None:
                overrides[f"{exchange}.{contract}"] = {"marginRate": override_rate}
    return {
        "products": {key: products[key] for key in sorted(products)},
        "contractOverrides": {key: overrides[key] for key in sorted(overrides)},
    }


def _positional_rows(table: Any) -> Iterable[Sequence[Any]]:
    iterator = getattr(table, "itertuples", None)
    if callable(iterator):
        yield from iterator(index=False, name=None)
        return
    if isinstance(table, Iterable) and not isinstance(table, (str, bytes, Mapping)):
        for row in table:
            if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
                yield row


def _akshare_futures_rule(day: str) -> Any:
    import akshare as ak

    return ak.futures_rule(date=day)


def _load_snapshot_for_merge(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema": SNAPSHOT_SCHEMA,
            "provider": SNAPSHOT_PROVIDER,
            "retrievedAt": "",
            "days": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(f"invalid futures rule snapshot: {path}") from error
    _validate_snapshot_envelope(payload)
    return payload


def _validate_snapshot_envelope(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("futures rule snapshot must be an object")
    if payload.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("unsupported futures rule snapshot schema")
    if payload.get("provider") != SNAPSHOT_PROVIDER:
        raise ValueError("unexpected futures rule snapshot provider")
    if not isinstance(payload.get("retrievedAt"), str):
        raise ValueError("futures rule snapshot retrievedAt must be a string")
    if not isinstance(payload.get("days"), dict):
        raise ValueError("futures rule snapshot days must be an object")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _sync_result(
    path: Path,
    inventory: Mapping[str, Mapping[str, str]],
    fetched_days: Iterable[str],
    product_count: int,
    override_count: int,
    status: str,
    *,
    failures: Iterable[Mapping[str, str]] = (),
) -> dict[str, Any]:
    fetched = tuple(fetched_days)
    failed = tuple(dict(item) for item in failures)
    return {
        "status": status,
        "path": str(path),
        "tradingDays": list(inventory),
        "fetchedDays": list(fetched),
        "fetchedDayCount": len(fetched),
        "productRuleCount": product_count,
        "contractOverrideCount": override_count,
        "failures": list(failed),
        "failureCount": len(failed),
    }


def _parse_day(value: date | str | Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _percent_rate(value: Any) -> float | None:
    text = _plain_text(value).replace("％", "%").strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    percent = _positive_float(text)
    if percent is None or percent > 100:
        return None
    return percent / 100.0


def _rate(value: Any) -> float | None:
    parsed = _positive_float(value)
    return parsed if parsed is not None and parsed <= 1 else None


def _plain_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def _normalize_contract(value: str | None) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    return text.rsplit(".", 1)[-1]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = (
    "FuturesContractRule",
    "RuleBook",
    "SNAPSHOT_PROVIDER",
    "SNAPSHOT_RELATIVE_PATH",
    "SNAPSHOT_SCHEMA",
    "load_rule_book",
    "sync_futures_rule_snapshots",
)
