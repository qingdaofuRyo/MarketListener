"""End-to-end offline pipeline: ingest -> quality -> signed package -> ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .incremental import CheckpointStore, FetchOutcome, IncrementalCollector, IncrementalRunSummary
from .market_package import PackageLedger, build_market_package
from .profile_gate import profile_write_gate
from .quality import QualityReport, quarantine_partition, validate_cross_source, validate_partition
from .signing import generate_development_key, sign_market_package, verify_market_package
from .storage import MarketStore, PartitionKey


@dataclass(frozen=True)
class IngestUnit:
    source: str
    instrument: str
    period: str
    partition_key: PartitionKey
    fetch: Callable[[str | None], FetchOutcome]
    normalize: Callable[[Mapping[str, Any]], list[Mapping[str, Any]]] | None = None


@dataclass(frozen=True)
class PipelineResult:
    ingest: tuple[IncrementalRunSummary, ...]
    quality: QualityReport | None
    package_path: Path | None
    verified: bool
    ledger_status: str | None


class MarketPipeline:
    """Orchestrates ingestion, quality gating and immutable signed packages."""

    def __init__(
        self,
        *,
        data_root: Path,
        state_path: Path,
        output_dir: Path,
        private_key_path: Path,
        public_key_path: Path,
        max_retries: int = 2,
    ) -> None:
        self.store = MarketStore(data_root)
        self.checkpoints = CheckpointStore(state_path)
        self.output_dir = output_dir
        self.private_key_path = private_key_path
        self.public_key_path = public_key_path
        self.collector = IncrementalCollector(self.store, self.checkpoints, max_retries=max_retries)

    def close(self) -> None:
        self.checkpoints.close()
        self.store.close()

    def ensure_keys(self) -> None:
        if not self.private_key_path.is_file() or not self.public_key_path.is_file():
            generate_development_key(self.private_key_path, self.public_key_path)

    def ingest(self, units: Sequence[IngestUnit]) -> tuple[IncrementalRunSummary, ...]:
        return tuple(
            self.collector.collect(
                source=unit.source,
                instrument=unit.instrument,
                period=unit.period,
                partition_key=unit.partition_key,
                fetch=unit.fetch,
                normalize=unit.normalize,
            )
            for unit in units
        )

    def quality(
        self,
        partition_id: str,
        bars: Sequence[Mapping[str, Any]],
        data_cutoff: str,
        *,
        expected_offset: str | None = None,
        reference_bars: Sequence[Mapping[str, Any]] | None = None,
    ) -> QualityReport:
        report = validate_partition(partition_id, bars, data_cutoff, expected_offset=expected_offset)
        if reference_bars:
            cross = validate_cross_source(partition_id, bars, reference_bars)
            report = QualityReport(partition_id, tuple(report.issues) + tuple(cross.issues))
        if report.blocking:
            quarantine_partition(self.store.root, partition_id, bars, report)
        return report

    def package(
        self,
        *,
        package_id: str,
        partition_id: str,
        bars: Sequence[Mapping[str, Any]],
        quality_report: QualityReport,
        data_cutoff: str,
        source_run_summaries: Sequence[Mapping[str, str]],
        ledger: PackageLedger | None = None,
    ) -> Path:
        if quality_report.blocking:
            raise ValueError("blocking quality issues cannot be packaged")
        self.ensure_keys()
        package = build_market_package(
            self.output_dir,
            package_id,
            bars,
            quality_report,
            data_cutoff,
            source_run_summaries,
        )
        sign_market_package(package, self.private_key_path)
        if not verify_market_package(package, self.public_key_path):
            raise RuntimeError("package signature verification failed after signing")
        if ledger is not None:
            with profile_write_gate(self.store.root):
                ledger.register(package_id, "FULL")
                ledger.activate(package_id)
        return package

    def load_partition(self, partition_id: str) -> tuple[list[Mapping[str, Any]], str | None]:
        metadata = self.store.partition_metadata(partition_id)
        if metadata is None:
            return [], None
        return _read_parquet(self.store, self.store.root / metadata[0]), metadata[2]

    def run(
        self,
        *,
        package_id: str,
        units: Sequence[IngestUnit],
        data_cutoff: str,
        source_run_summaries: Sequence[Mapping[str, str]],
        ledger: PackageLedger | None = None,
        expected_offset: str | None = None,
        reference_bars: Sequence[Mapping[str, Any]] | None = None,
    ) -> PipelineResult:
        summaries = self.ingest(units)
        if any(summary.status == "FAILED" for summary in summaries):
            return PipelineResult(summaries, None, None, False, "INGEST_FAILED")
        all_bars: list[Mapping[str, Any]] = []
        for unit in units:
            partition_metadata = self.store.partition_metadata(unit.partition_key.partition_id)
            if partition_metadata is None:
                continue
            parquet = self.store.root / partition_metadata[0]
            all_bars.extend(_read_parquet(self.store, parquet))
        if not all_bars:
            return PipelineResult(summaries, None, None, False, "NO_DATA")
        report = self.quality(
            package_id + "-partition",
            all_bars,
            data_cutoff,
            expected_offset=expected_offset,
            reference_bars=reference_bars,
        )
        if report.blocking:
            return PipelineResult(summaries, report, None, False, "QUARANTINED")
        package = self.package(
            package_id=package_id,
            partition_id=report.partition_id,
            bars=all_bars,
            quality_report=report,
            data_cutoff=data_cutoff,
            source_run_summaries=source_run_summaries,
            ledger=ledger,
        )
        verified = verify_market_package(package, self.public_key_path)
        return PipelineResult(summaries, report, package, verified, "ACTIVE" if ledger is not None else "SIGNED")


def _read_parquet(store: MarketStore, parquet: Path) -> list[Mapping[str, Any]]:
    cursor = store.connection.execute(
        f"SELECT bar_json FROM read_parquet('{str(parquet).replace(chr(39), chr(39) * 2)}')"
    )
    return [json.loads(row[0]) for row in cursor.fetchall()]
