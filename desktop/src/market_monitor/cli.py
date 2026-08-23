"""Machine-readable command-line entry points for the data producer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from market_monitor import __version__
from market_monitor.collector import run_fetch_session
from market_monitor.full_market import run_full_etf_backfill, run_full_stock_backfill
from market_monitor.futures_bulk import run_bulk_futures
from market_monitor.tdx_local import run_tdx_local_import
from market_monitor.ths_market import run_ths_market_snapshot
from market_monitor.configuration import ConfigurationError, load_local_configuration
from market_monitor.f10 import f10_status, run_f10_fetch, run_revenue_fetch
from market_monitor.industry_graph.f10.enrichment import enrich_batch
from market_monitor.industry_graph.f10.providers import get_governance, list_providers, validate_max_rps
from market_monitor.industry_atlas import build_atlas
from market_monitor.package_builder import build_android_package
from market_monitor.market_query_cache import rebuild_kline_query_cache
from market_monitor.report_pipeline import (
    build_chain_index,
    process_report_batch,
    report_status_summary,
    verify_report_batch,
)
from market_monitor.providers.comparison import compare_daily_bars, write_comparison
from market_monitor.providers.registry import registered_providers
from market_monitor.providers.runner import ProbeRunner, redact_secrets
from market_monitor.web_app import serve_web_app


EXIT_SUCCESS = 0
EXIT_PARTIAL_FAILURE = 2
EXIT_CONFIGURATION = 3
EXIT_ARGUMENT = 64


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="market-monitor",
        description="Market monitor desktop producer CLI skeleton.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", parser_class=_Parser)
    probe = subcommands.add_parser("probe", help="probe registered external providers")
    probe.add_argument("--report-dir", type=Path, default=Path("reports"))
    probe.add_argument("--config-file", type=Path, help="explicit local env file outside the repository")
    probe.add_argument("--provider", action="append", dest="providers", help="provider name; repeat to select")
    probe.add_argument("--timeout-seconds", type=float, default=45.0, help="maximum wall-clock time per provider invocation")
    compare = subcommands.add_parser("compare-sources", help="compare registered source data without blending rows")
    compare.add_argument("--report-dir", type=Path, default=Path("reports"))
    serve = subcommands.add_parser("serve", help="serve the local investment research terminal")
    serve.add_argument("--data-root", type=Path, default=Path("data"))
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="bind port (0 picks a free port)")
    serve.add_argument("--timeout-seconds", type=float, default=None, help="stop after N seconds (tests/CI)")
    serve.add_argument("--quiet", action="store_true", help="suppress per-request HTTP logs")
    kline_cache = subcommands.add_parser("kline-cache", help="build or refresh the local low-latency K-line query cache")
    kline_cache.add_argument("--data-root", type=Path, default=Path("data_control"))
    fetch = subcommands.add_parser("fetch", help="run a real data-fetch session and persist results")
    fetch.add_argument("--data-root", type=Path, default=Path("data_control"))
    fetch.add_argument("--limit-futures", type=int, default=15, help="number of domestic futures main contracts to fetch")
    fetch.add_argument("--limit-cn-stocks", type=int, default=5, help="number of CN stocks to fetch as samples")
    fetch.add_argument("--max-workers", type=int, default=4, help="concurrent fetch workers")
    fetch.add_argument("--task-timeout-seconds", type=float, default=90.0, help="per-task wall-clock timeout")
    bulk_stocks = subcommands.add_parser("bulk-stocks", help="resumable full A/H-share daily-bar backfill")
    bulk_stocks.add_argument("--data-root", type=Path, default=Path("data_control"))
    bulk_stocks.add_argument("--market", choices=["CN", "HK", "BOTH"], default="BOTH")
    bulk_stocks.add_argument("--history-days", type=int, default=450, help="calendar days to fetch per stock")
    bulk_stocks.add_argument("--workers", type=int, default=4, help="rate-limited concurrent requests (1-4)")
    bulk_stocks.add_argument("--batch-size", type=int, default=20, help="durable checkpoint interval")
    bulk_stocks.add_argument("--pause-seconds", type=float, default=0.3, help="per-worker pause after each request")
    bulk_etfs = subcommands.add_parser("bulk-etfs", help="resumable full domestic ETF daily-bar backfill")
    bulk_etfs.add_argument("--data-root", type=Path, default=Path("data_control"))
    bulk_etfs.add_argument("--workers", type=int, default=4)
    bulk_etfs.add_argument("--batch-size", type=int, default=20)
    bulk_etfs.add_argument("--pause-seconds", type=float, default=0.2)
    bulk_futures = subcommands.add_parser("bulk-futures", help="增量导入通达信期货通与 AKShare 期货行情")
    bulk_futures.add_argument("--data-root", type=Path, default=Path("data_control"))
    bulk_futures.add_argument("--tdx-futures-root", type=Path, default=None, help="通达信期货通安装目录")
    bulk_futures.add_argument("--domestic-only", action="store_true", help="仅导入国内期货与商品指数")
    bulk_futures.add_argument("--global-only", action="store_true", help="仅导入国外重点期货与参考指数")
    bulk_futures.add_argument("--local-only", action="store_true", help="仅导入通达信期货通本地文件，不请求 AKShare")
    bulk_futures.add_argument("--full-rescan", action="store_true", help="忽略本地文件检查点并全量重扫")
    tdx_local = subcommands.add_parser("import-tdx-local", help="增量导入通达信金融终端本地 A 股、港股日线与 5 分钟线")
    tdx_local.add_argument("--data-root", type=Path, default=Path("data_control"))
    tdx_local.add_argument("--tdx-root", type=Path, default=None, help="通达信金融终端安装目录")
    tdx_local.add_argument("--full-rescan", action="store_true", help="忽略本地文件检查点并全量重扫")
    tdx_local.add_argument("--batch-rows", type=int, default=250_000, help="每个 Silver 写入批次的最大 K 线数")
    tdx_local.add_argument("--skip-cache-rebuild", action="store_true", help="导入后不重建低延迟 K 线查询缓存")
    tdx_local.add_argument("--start-date", help="仅导入该日期（含）之后的本地K线，格式 YYYY-MM-DD")
    tdx_local.add_argument("--end-date", help="仅导入该日期（含）之前的本地K线，格式 YYYY-MM-DD")
    ths = subcommands.add_parser("ths-market", help="collect THS market breadth and CSI index snapshots")
    ths.add_argument("--data-root", type=Path, default=Path("data_control"))
    f10 = subcommands.add_parser("f10", help="fetch A/H share F10 basics (throttled, resumable)")
    f10.add_argument("--data-root", type=Path, default=Path("data_control"))
    f10.add_argument("--market", default="CN", choices=["CN", "HK"], help="CN or HK listed companies")
    f10.add_argument("--limit-details", type=int, default=200, help="max per-stock F10 detail calls per run")
    f10.add_argument("--detail-delay-seconds", type=float, default=1.2, help="pause between F10 detail calls")
    f10.add_argument("--skip-quotes", action="store_true", help="skip the Tencent bulk quote refresh")
    f10.add_argument("--force-details", action="store_true", help="refetch details even when already cached")
    f10.add_argument("--status", action="store_true", help="print current collection status and exit")
    f10.add_argument("--revenue-only", action="store_true", help="fetch BusinessAnalysis revenue breakdown for cached CN companies")
    f10.add_argument("--revenue-limit", type=int, default=200, help="max revenue-breakdown calls per run")
    f10.add_argument("--codes-file", type=Path, default=None, help="optional code list (one per line) for revenue fetch / enrichment")
    f10.add_argument("--enrich-missing", action="store_true", help="run the multi-source missing-field enrichment pipeline")
    f10.add_argument("--enrich-limit", type=int, default=None, help="max companies to enrich per run (default: all cached)")
    f10.add_argument("--provider", action="append", dest="enrich_providers", help="restrict enrichment to a provider; repeat to select")
    f10.add_argument("--max-rps", type=float, default=None, help="provider request rate cap (default 4, hard max 10)")
    f10.add_argument("--force-provider-refresh", action="store_true", help="re-enrich companies even when already enriched")
    f10.add_argument("--workers", type=int, default=1, help="parallel enrichment worker threads (default 1)")
    package = subcommands.add_parser("package", help="build and sign an immutable Android sync package")
    package.add_argument("--data-root", type=Path, default=Path("data_control"))
    package.add_argument(
        "--private-key",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "keys" / "market_package_private_key.pem",
        help="Ed25519 private key used to sign the package",
    )
    package.add_argument(
        "--ecdsa-private-key",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "keys" / "market_package_ecdsa_private.pem",
        help="ECDSA P-256 private key used for Android fallback signature",
    )
    package.add_argument("--minimum-app-version", default="0.1.0", help="minimum Android app version")
    reports = subcommands.add_parser("reports", help="industry research report knowledge-base pipeline")
    report_subcommands = reports.add_subparsers(dest="report_command", parser_class=_Parser)
    process = report_subcommands.add_parser("process", help="parse and extract facts from PDF reports")
    process.add_argument("--report-root", type=Path, default=Path("行业产业链研报"))
    process.add_argument("--output-root", type=Path, default=Path("reports/industry"))
    process.add_argument("--workers", type=int, default=4)
    process.add_argument("--limit", type=int, default=0, help="process only the first N reports (0=all)")
    process.add_argument("--version", type=int, default=1)
    status = report_subcommands.add_parser("status", help="show per-report pipeline status")
    status.add_argument("--output-root", type=Path, default=Path("reports/industry"))
    verify = report_subcommands.add_parser("verify", help="scripted review/verify of extracted reports")
    verify.add_argument("--output-root", type=Path, default=Path("reports/industry"))
    verify.add_argument("--workers", type=int, default=4)
    chains = report_subcommands.add_parser("chains", help="aggregate extracted facts into industry chains")
    chains.add_argument("--output-root", type=Path, default=Path("reports/industry"))
    chains.add_argument("--max-facts-per-chain", type=int, default=200)
    atlas = report_subcommands.add_parser("atlas", help="build the new brokerage-style industry atlas HTML/JSON")
    atlas.add_argument("--output-root", type=Path, default=Path("reports/industry"))
    atlas.add_argument("--data-root", type=Path, default=Path("data_control"), help="data_control root for F10 and sync target")
    atlas.add_argument("--chain-index", type=Path, default=None, help="chain_index.json path (default: <output-root>/chain_index.json)")
    atlas.add_argument("--legacy-html", type=Path, default=None, help="legacy A股企业产业链精细定位.html path")
    atlas.add_argument("--f10-dir", type=Path, default=None, help="F10 jsonl directory (default: <data-root>/industry/f10)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "probe":
            return _probe(args)
        if args.command == "compare-sources":
            providers = {provider.name: provider for provider in registered_providers()}
            report = compare_daily_bars(providers["joinquant"], providers["baostock"])
            machine_path, human_path = write_comparison(report, args.report_dir)
            _emit("SUCCESS", EXIT_SUCCESS, reports=[str(machine_path), str(human_path)])
            return EXIT_SUCCESS
        if args.command == "serve":
            return _serve(args)
        if args.command == "kline-cache":
            return _kline_cache(args)
        if args.command == "fetch":
            return _fetch(args)
        if args.command == "bulk-stocks":
            return _bulk_stocks(args)
        if args.command == "bulk-etfs":
            return _bulk_etfs(args)
        if args.command == "bulk-futures":
            return _bulk_futures(args)
        if args.command == "import-tdx-local":
            return _import_tdx_local(args)
        if args.command == "ths-market":
            return _ths_market(args)
        if args.command == "f10":
            return _f10(args)
        if args.command == "package":
            return _package(args)
        if args.command == "reports":
            return _reports(args)
        _emit("SUCCESS", EXIT_SUCCESS, message="market-monitor desktop skeleton ready")
        return EXIT_SUCCESS
    except (ValueError, ConfigurationError) as error:
        _emit("ARGUMENT_ERROR", EXIT_ARGUMENT, message=redact_secrets(str(error)))
        return EXIT_ARGUMENT


def _probe(args: argparse.Namespace) -> int:
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    repository_root = Path(__file__).resolve().parents[3]
    configuration = load_local_configuration(config_file=args.config_file, repo_root=repository_root)
    providers = registered_providers(configuration.values)
    if args.providers:
        available = {provider.name for provider in providers}
        if any(name not in available for name in args.providers):
            raise ValueError("an unknown provider was requested")
        providers = tuple(provider for provider in providers if provider.name in args.providers)
    report = ProbeRunner(timeout_seconds=args.timeout_seconds, secret_values=configuration.secret_values).run(providers)
    machine_path, human_path = ProbeRunner(secret_values=configuration.secret_values).write_reports(report, args.report_dir)
    statuses = [capability.status.value for result in report.results for capability in result.capabilities]
    if statuses and all(status == "BLOCKED" for status in statuses):
        exit_code, status = EXIT_CONFIGURATION, "CONFIGURATION_BLOCKED"
    elif any(status in {"FAILED", "BLOCKED"} for status in statuses):
        exit_code, status = EXIT_PARTIAL_FAILURE, "PARTIAL_FAILURE"
    else:
        exit_code, status = EXIT_SUCCESS, "SUCCESS"
    _emit(status, exit_code, reports=[str(machine_path), str(human_path)], secret_values=configuration.secret_values)
    return exit_code


def _serve(args: argparse.Namespace) -> int:
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    try:
        host, port = serve_web_app(
            args.data_root,
            host=args.host,
            port=args.port,
            timeout_seconds=args.timeout_seconds,
            quiet=args.quiet,
        )
    except KeyboardInterrupt:
        _emit("SUCCESS", EXIT_SUCCESS, message="control center stopped by user")
        return EXIT_SUCCESS
    _emit("SUCCESS", EXIT_SUCCESS, message=f"local research terminal served at http://{host}:{port}/")
    return EXIT_SUCCESS


def _kline_cache(args: argparse.Namespace) -> int:
    summary = rebuild_kline_query_cache(args.data_root)
    _emit(
        "SUCCESS",
        EXIT_SUCCESS,
        message=(
            f"K线查询缓存已建立：{summary['rows']} 根，"
            f"耗时 {summary.get('buildSeconds') or 0:.2f} 秒"
        ),
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return EXIT_SUCCESS


def _fetch(args: argparse.Namespace) -> int:
    if args.limit_futures <= 0:
        raise ValueError("--limit-futures must be positive")
    if args.limit_cn_stocks <= 0:
        raise ValueError("--limit-cn-stocks must be positive")
    summary = run_fetch_session(
        args.data_root,
        max_workers=args.max_workers,
        task_timeout_seconds=args.task_timeout_seconds,
        limit_futures=args.limit_futures,
        limit_cn_stocks=args.limit_cn_stocks,
    )
    exit_code = EXIT_SUCCESS if summary["status"] == "PASS" else EXIT_PARTIAL_FAILURE
    _emit(
        summary["status"],
        exit_code,
        message=(
            f"session {summary['session_id']}: {summary['passed']} passed, "
            f"{summary.get('partial_failure', 0)} partial, "
            f"{summary['failed']} failed, {summary['blocked']} blocked, "
            f"{summary['total_rows']} rows"
        ),
    )
    return exit_code


def _bulk_stocks(args: argparse.Namespace) -> int:
    summary = run_full_stock_backfill(
        args.data_root,
        market=args.market,
        history_days=args.history_days,
        workers=args.workers,
        batch_size=args.batch_size,
        pause_seconds=args.pause_seconds,
    )
    success = summary["状态"] == "完成"
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    _emit("SUCCESS" if success else "PARTIAL_FAILURE", EXIT_SUCCESS if success else EXIT_PARTIAL_FAILURE, message="全量个股日线抓取结束")
    return EXIT_SUCCESS if success else EXIT_PARTIAL_FAILURE


def _bulk_etfs(args: argparse.Namespace) -> int:
    summary = run_full_etf_backfill(args.data_root, workers=args.workers, batch_size=args.batch_size, pause_seconds=args.pause_seconds)
    success = summary["状态"] == "完成"
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    _emit("SUCCESS" if success else "PARTIAL_FAILURE", EXIT_SUCCESS if success else EXIT_PARTIAL_FAILURE, message="全量ETF日线抓取结束")
    return EXIT_SUCCESS if success else EXIT_PARTIAL_FAILURE


def _bulk_futures(args: argparse.Namespace) -> int:
    if args.domestic_only and args.global_only:
        raise ValueError("--domestic-only 与 --global-only 不能同时使用")
    if args.local_only and args.global_only:
        raise ValueError("--local-only 不能与 --global-only 同时使用")
    summary = run_bulk_futures(
        args.data_root,
        tdx_futures_root=args.tdx_futures_root,
        include_domestic=not args.global_only,
        include_global=not args.domestic_only and not args.local_only,
        include_akshare=not args.local_only,
        full_rescan=args.full_rescan,
    )
    success = summary["状态"] == "完成"
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    _emit("SUCCESS" if success else "PARTIAL_FAILURE", EXIT_SUCCESS if success else EXIT_PARTIAL_FAILURE, message="国内外期货增量任务结束")
    return EXIT_SUCCESS if success else EXIT_PARTIAL_FAILURE


def _import_tdx_local(args: argparse.Namespace) -> int:
    summary = run_tdx_local_import(
        args.data_root,
        tdx_root=args.tdx_root,
        full_rescan=args.full_rescan,
        batch_rows=args.batch_rows,
        rebuild_cache=not args.skip_cache_rebuild,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    success = summary["状态"] == "完成"
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    _emit(
        "SUCCESS" if success else "PARTIAL_FAILURE",
        EXIT_SUCCESS if success else EXIT_PARTIAL_FAILURE,
        message="通达信本地 A 股、港股行情导入结束",
    )
    return EXIT_SUCCESS if success else EXIT_PARTIAL_FAILURE


def _ths_market(args: argparse.Namespace) -> int:
    summary = run_ths_market_snapshot(args.data_root)
    success = summary["状态"] == "完成"
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    _emit("SUCCESS" if success else "PARTIAL_FAILURE", EXIT_SUCCESS if success else EXIT_PARTIAL_FAILURE, message="同花顺市场宽度与指数快照任务结束")
    return EXIT_SUCCESS if success else EXIT_PARTIAL_FAILURE


def _f10(args: argparse.Namespace) -> int:
    if args.limit_details < 0:
        raise ValueError("--limit-details must be non-negative")
    min_delay = 0.02
    if args.detail_delay_seconds < min_delay:
        raise ValueError(f"--detail-delay-seconds must be at least {min_delay}")
    if args.status:
        summary = f10_status(args.data_root, market=args.market)
        _emit("SUCCESS", EXIT_SUCCESS, message=f"f10 {args.market}: {summary['record_count']} records")
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return EXIT_SUCCESS
    if args.enrich_missing:
        if args.enrich_limit is not None and args.enrich_limit < 0:
            raise ValueError("--enrich-limit must be non-negative")
        if args.max_rps is not None:
            validate_max_rps(args.max_rps)
        if args.workers < 1:
            raise ValueError("--workers must be at least 1")
        codes = None
        if args.codes_file:
            path = Path(args.codes_file)
            codes = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        providers = list_providers()
        if args.enrich_providers:
            selected = {}
            for name in args.enrich_providers:
                key = name.strip().lower()
                if key not in providers:
                    raise ValueError(f"unknown F10 provider: {name}")
                selected[key] = providers[key]
            providers = selected
        governance = get_governance(max_rps=args.max_rps) if args.max_rps is not None else None
        summary = enrich_batch(
            args.data_root,
            market=args.market,
            codes=codes,
            limit=args.enrich_limit,
            providers=providers,
            governance=governance,
            force=args.force_provider_refresh,
            workers=args.workers,
        )
        if summary.get("status") == "SKIPPED":
            _emit(
                "SKIPPED",
                EXIT_SUCCESS,
                message=summary.get("message", f"f10 {args.market} enrichment skipped (lock held)"),
            )
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return EXIT_SUCCESS
        exit_code = EXIT_SUCCESS if summary["status"] == "PASS" else EXIT_PARTIAL_FAILURE
        _emit(
            summary["status"],
            exit_code,
            message=(
                f"f10 enrich: passed {summary.get('passed', 0)}, "
                f"skipped {summary.get('skipped', 0)}, failed {summary.get('failed', 0)}"
            ),
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return exit_code
    if args.revenue_only:
        codes = None
        if args.codes_file:
            path = Path(args.codes_file)
            codes = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        summary = run_revenue_fetch(
            args.data_root,
            market=args.market,
            limit=args.revenue_limit,
            detail_delay_seconds=args.detail_delay_seconds,
            codes=codes,
        )
        if summary.get("status") == "SKIPPED":
            _emit(
                "SKIPPED",
                EXIT_SUCCESS,
                message=summary.get("message", f"f10 {args.market} revenue skipped (lock held)"),
            )
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return EXIT_SUCCESS
        exit_code = EXIT_SUCCESS if summary["status"] == "PASS" else EXIT_PARTIAL_FAILURE
        _emit(
            summary["status"],
            exit_code,
            message=f"f10 revenue: new {summary.get('new_revenue', 0)}, total {summary.get('total_revenue', 0)}",
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return exit_code
    summary = run_f10_fetch(
        args.data_root,
        market=args.market,
        limit_details=args.limit_details,
        detail_delay_seconds=args.detail_delay_seconds,
        skip_quotes=args.skip_quotes,
        force_details=args.force_details,
    )
    if summary.get("status") == "SKIPPED":
        _emit(
            "SKIPPED",
            EXIT_SUCCESS,
            message=summary.get("message", f"f10 {args.market} skipped (lock held)"),
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return EXIT_SUCCESS
    exit_code = EXIT_SUCCESS if summary["status"] == "PASS" else EXIT_PARTIAL_FAILURE
    _emit(
        summary["status"],
        exit_code,
        message=(
            f"f10 {args.market}: universe {summary['universe_count']}, "
            f"quotes {summary['quote_count']}, new details {summary['new_details']}, "
            f"total details {summary['total_details']}"
        ),
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return exit_code


def _package(args: argparse.Namespace) -> int:
    summary = build_android_package(
        args.data_root,
        args.private_key,
        ecdsa_private_key=args.ecdsa_private_key,
        minimum_app_version=args.minimum_app_version,
    )
    _emit(
        "SUCCESS",
        EXIT_SUCCESS,
        message=(
            f"package {summary['package_id']}: {summary['bars']} bars, "
            f"{summary['gold_metrics']} gold metrics, {summary['package_bytes']} bytes"
        ),
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return EXIT_SUCCESS


def _reports(args: argparse.Namespace) -> int:
    if args.report_command == "process":
        if args.workers <= 0:
            raise ValueError("--workers must be positive")
        if args.limit < 0:
            raise ValueError("--limit must be non-negative")
        summary = process_report_batch(
            args.report_root,
            args.output_root,
            workers=args.workers,
            limit=args.limit,
            version=args.version,
        )
        _emit(
            "SUCCESS" if summary["failed"] == 0 else "PARTIAL_FAILURE",
            EXIT_SUCCESS if summary["failed"] == 0 else EXIT_PARTIAL_FAILURE,
            message=(
                f"reports: {summary['processed']} processed, {summary['skipped']} skipped, "
                f"{summary['failed']} failed, {summary['facts']} facts"
            ),
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return EXIT_SUCCESS
    if args.report_command == "status":
        summary = report_status_summary(args.output_root)
        _emit("SUCCESS", EXIT_SUCCESS, message=f"reports status: {summary['total']} tracked")
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return EXIT_SUCCESS
    if args.report_command == "verify":
        if args.workers <= 0:
            raise ValueError("--workers must be positive")
        summary = verify_report_batch(args.output_root, workers=args.workers)
        _emit(
            "SUCCESS" if summary["failed"] == 0 and summary["flagged"] == 0 else "PARTIAL_FAILURE",
            EXIT_SUCCESS if summary["failed"] == 0 and summary["flagged"] == 0 else EXIT_PARTIAL_FAILURE,
            message=(
                f"reports verify: {summary['passed']} passed, "
                f"{summary['flagged']} flagged, {summary['failed']} failed"
            ),
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return EXIT_SUCCESS
    if args.report_command == "chains":
        index = build_chain_index(args.output_root, max_facts_per_chain=args.max_facts_per_chain)
        _emit(
            "SUCCESS",
            EXIT_SUCCESS,
            message=f"industry chains: {index['chain_count']} chains, {index['report_count']} reports",
        )
        print(json.dumps(index, ensure_ascii=False, sort_keys=True))
        return EXIT_SUCCESS
    if args.report_command == "atlas":
        summary = build_atlas(
            args.output_root,
            data_root=args.data_root,
            chain_index_path=args.chain_index,
            legacy_html_path=args.legacy_html,
            f10_dir=args.f10_dir,
        )
        _emit(
            "SUCCESS",
            EXIT_SUCCESS,
            message=(
                f"industry atlas: {summary['chain_count']} chains, "
                f"{summary['company_codes']} companies with codes"
            ),
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return EXIT_SUCCESS
    raise ValueError("missing reports subcommand (process|status|verify|chains|atlas)")


def _emit(
    status: str,
    exit_code: int,
    *,
    message: str | None = None,
    reports: list[str] | None = None,
    secret_values: tuple[str, ...] = (),
) -> None:
    payload: dict[str, object] = {"status": status, "exit_code": exit_code}
    if message:
        payload["message"] = message
    if reports:
        payload["reports"] = reports
    print(json.dumps(redact_secrets(payload, secret_values=secret_values), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
