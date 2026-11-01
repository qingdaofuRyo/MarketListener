"""Data-page source jobs run after publication windows and deduplicate."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_monitor.data_page_auto_refresh import data_target, due_data_updates
from market_monitor.operations import Operation, OperationKind, OperationStatus


def test_after_close_target_checks_public_sources_daily_and_daily_disclosures() -> None:
    before = datetime(2026, 10, 20, 10, 59, tzinfo=timezone.utc)
    after = datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc)
    assert data_target(OperationKind.EQUITY_DASHBOARD_UPDATE, before)[0] == "2026-10-19"
    assert data_target(OperationKind.EQUITY_DASHBOARD_UPDATE, after)[0] == "2026-10-20"
    assert data_target(OperationKind.DISCLOSURES_UPDATE, after)[0] == "2026-10-19"
    assert data_target(OperationKind.A_SHARE_DELISTED_UPDATE, before)[0] == "2026-10-19"
    assert data_target(OperationKind.A_SHARE_DELISTED_UPDATE, after)[0] == "2026-10-20"
    assert due_data_updates([], after) == [OperationKind.EQUITY_DASHBOARD_UPDATE,
                                           OperationKind.DISCLOSURES_UPDATE, OperationKind.A_SHARE_DELISTED_UPDATE]
    mainland_holiday = datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)
    assert data_target(OperationKind.EQUITY_DASHBOARD_UPDATE, mainland_holiday)[0] == "2026-10-01"


def test_data_update_pass_and_active_job_stop_duplicates() -> None:
    now = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
    equity = Operation("equity", OperationKind.EQUITY_DASHBOARD_UPDATE, OperationStatus.PASS,
                       "2026-10-20T11:05:00+00:00", completed_at="2026-10-20T11:06:00+00:00")
    disclosure = Operation("disclosure", OperationKind.DISCLOSURES_UPDATE, OperationStatus.RUNNING,
                           "2026-10-20T12:35:00+00:00")
    assert due_data_updates([equity, disclosure], now) == [OperationKind.A_SHARE_DELISTED_UPDATE]
    disclosure.status = OperationStatus.PARTIAL_FAILURE
    assert due_data_updates([equity, disclosure], now) == [OperationKind.A_SHARE_DELISTED_UPDATE]
    later = datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc)
    assert due_data_updates([equity, disclosure], later) == [OperationKind.DISCLOSURES_UPDATE, OperationKind.A_SHARE_DELISTED_UPDATE]


def test_partial_retry_backoff_begins_when_long_job_completes() -> None:
    disclosure = Operation(
        "slow-disclosure", OperationKind.DISCLOSURES_UPDATE, OperationStatus.PARTIAL_FAILURE,
        "2026-10-20T12:35:00+00:00", completed_at="2026-10-20T18:59:00+00:00",
    )
    assert due_data_updates([disclosure], datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc)) == [
        OperationKind.EQUITY_DASHBOARD_UPDATE,
        OperationKind.A_SHARE_DELISTED_UPDATE,
    ]
    assert due_data_updates([disclosure], datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)) == [
        OperationKind.EQUITY_DASHBOARD_UPDATE, OperationKind.DISCLOSURES_UPDATE, OperationKind.A_SHARE_DELISTED_UPDATE,
    ]


@pytest.mark.parametrize("bad_time", ["invalid", "2026-10-20T11:05:00", ""])
def test_invalid_task_creation_time_only_blocks_that_source(bad_time: str) -> None:
    now = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
    broken = Operation("broken", OperationKind.EQUITY_DASHBOARD_UPDATE, OperationStatus.FAILED, bad_time)
    assert due_data_updates([broken], now) == [OperationKind.DISCLOSURES_UPDATE, OperationKind.A_SHARE_DELISTED_UPDATE]


def test_invalid_completion_time_does_not_block_other_sources() -> None:
    now = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
    broken = Operation("broken", OperationKind.EQUITY_DASHBOARD_UPDATE, OperationStatus.PARTIAL_FAILURE,
                       "2026-10-20T11:05:00+00:00", completed_at="2026-10-20T11:06:00")
    assert due_data_updates([broken], now) == [OperationKind.DISCLOSURES_UPDATE, OperationKind.A_SHARE_DELISTED_UPDATE]


def test_latest_task_is_ordered_by_actual_time_across_offsets() -> None:
    now = datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)
    older = Operation("old", OperationKind.EQUITY_DASHBOARD_UPDATE, OperationStatus.PASS,
                      "2026-10-20T20:00:00+08:00")
    newer = Operation("new", OperationKind.EQUITY_DASHBOARD_UPDATE, OperationStatus.FAILED,
                      "2026-10-20T13:00:00+00:00", completed_at="2026-10-20T13:01:00+00:00")
    assert OperationKind.EQUITY_DASHBOARD_UPDATE not in due_data_updates([older, newer], now)


def test_naive_current_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        due_data_updates([], datetime(2026, 10, 20, 13, 0))


def test_delisted_job_retries_independently_after_four_hours() -> None:
    job = Operation("delisted", OperationKind.A_SHARE_DELISTED_UPDATE, OperationStatus.PARTIAL_FAILURE,
                    "2026-10-20T11:01:00+00:00", completed_at="2026-10-20T11:05:00+00:00")
    assert OperationKind.A_SHARE_DELISTED_UPDATE not in due_data_updates([job], datetime(2026, 10, 20, 15, 4, tzinfo=timezone.utc))
    assert OperationKind.A_SHARE_DELISTED_UPDATE in due_data_updates([job], datetime(2026, 10, 20, 15, 5, tzinfo=timezone.utc))
