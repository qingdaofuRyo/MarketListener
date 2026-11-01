"""Tonghuashun's monthly CPI data stays separate from local TDX observations."""

from __future__ import annotations

import hashlib
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from market_monitor.ths_macro import CPI_URL, fetch_cpi_archive, parse_cpi_table


_HEAD = ("<thead><tr><th>月份</th><th>全国</th><th>城市</th><th>农村</th></tr>"
         "<tr><th>当月</th><th>同比增长</th><th>环比增长</th><th>累计</th>"
         "<th>当月</th><th>同比增长</th><th>累计</th>"
         "<th>当月</th><th>同比增长</th><th>累计</th></tr></thead>")


def _html(*rows: str, head: str = _HEAD) -> str:
    return f"<html><table>{head}<tbody>{''.join(rows)}</tbody></table></html>"


def _row(month: str, *, national: str = "100.90", mom: str = "0.20%") -> str:
    cells = [month, national, "0.90%", mom, "100.90", "100.80", "0.80%", "100.70",
             "100.60", "0.60%", "100.50"]
    return "<tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr>"


def _as_of() -> datetime:
    return datetime(2026, 10, 21, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_cpi_parser_preserves_monthly_units_and_missing_values() -> None:
    points = parse_cpi_table(_html(_row("2026-09"), _row("2026-08", mom="--")), as_of=_as_of())
    assert [item.period for item in points] == ["2026-09", "2026-08"]
    assert points[0].national_index == 100.9
    assert points[0].national_yoy_pct == .9
    assert points[0].national_mom_pct == .2
    assert points[0].urban_yoy_pct == .8
    assert points[1].national_mom_pct is None


@pytest.mark.parametrize("html", [
    _html(_row("2026-11")),
    _html(_row("2026-09"), _row("2026-09")),
    _html(_row("2026-09"), head=_HEAD.replace("环比增长", "月增长")),
    _html(_row("2026-09", national="0")),
    _html(_row("2026-09", mom="0.2")),
    _html(),
])
def test_cpi_parser_fails_closed_on_future_duplicate_or_changed_schema(html: str) -> None:
    with pytest.raises(ValueError):
        parse_cpi_table(html, as_of=_as_of())


def test_cpi_fetch_checks_origin_hash_and_gbk(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _html(_row("2026-09")).encode("gbk")
    calls = []

    class Session:
        def get(self, url: str, **kwargs: object) -> SimpleNamespace:
            calls.append((url, kwargs))
            return SimpleNamespace(url=CPI_URL, content=raw, raise_for_status=lambda: None)

    archive = fetch_cpi_archive(session=Session(), as_of=_as_of())  # type: ignore[arg-type]
    assert len(archive.observations) == 1
    assert archive.source_url == CPI_URL
    assert archive.payload_sha256 == hashlib.sha256(raw).hexdigest()
    assert calls[0][1]["timeout"] == 20

    class RedirectedSession(Session):
        def get(self, url: str, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(url="https://example.com/cpi", content=raw,
                                   raise_for_status=lambda: None)

    with pytest.raises(ValueError, match="official HTTPS host"):
        fetch_cpi_archive(session=RedirectedSession(), as_of=_as_of())  # type: ignore[arg-type]
