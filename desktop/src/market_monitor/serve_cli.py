"""Minimal command-line entry for the long-running local customer service.

The full CLI imports data collectors and report builders that are never used
by ``serve``. Parse its existing flags here, then import the web app only when
the request is valid. Other commands retain the established CLI entry point.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_monitor.configuration import ConfigurationError


EXIT_SUCCESS = 0
EXIT_ARGUMENT = 64


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="market-monitor",
        description="Market monitor desktop producer CLI skeleton.",
    )
    subcommands = parser.add_subparsers(dest="command", parser_class=_Parser)
    serve = subcommands.add_parser("serve", help="serve the local investment research terminal")
    serve.add_argument("--data-root", type=Path, default=Path("data"))
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="bind port (0 picks a free port)")
    serve.add_argument("--timeout-seconds", type=float, default=None, help="stop after N seconds (tests/CI)")
    serve.add_argument("--quiet", action="store_true", help="suppress per-request HTTP logs")
    serve.add_argument(
        "--no-auto-update-on-startup", action="store_true",
        help="启动网页服务但不自动排队更新行情、通达信及期货数据（F10始终不纳入自动任务）",
    )
    return parser


def _emit(status: str, exit_code: int, *, message: str) -> None:
    # Preserve the full CLI's output and redaction policy without importing
    # provider machinery before the service reaches its first request.
    from market_monitor.providers.runner import redact_secrets

    payload = {"status": status, "exit_code": exit_code, "message": message}
    print(json.dumps(redact_secrets(payload, secret_values=()), ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        if args.command != "serve":
            raise ValueError("missing serve command")
        if args.timeout_seconds is not None and args.timeout_seconds <= 0:
            raise ValueError("--timeout-seconds must be positive")

        from market_monitor.web_app import serve_web_app

        try:
            host, port = serve_web_app(
                args.data_root,
                host=args.host,
                port=args.port,
                timeout_seconds=args.timeout_seconds,
                quiet=args.quiet,
                auto_update_on_startup=not args.no_auto_update_on_startup,
            )
        except KeyboardInterrupt:
            _emit("SUCCESS", EXIT_SUCCESS, message="control center stopped by user")
            return EXIT_SUCCESS
        _emit("SUCCESS", EXIT_SUCCESS, message=f"local research terminal served at http://{host}:{port}/")
        return EXIT_SUCCESS
    except (ValueError, ConfigurationError) as error:
        from market_monitor.providers.runner import redact_secrets

        _emit("ARGUMENT_ERROR", EXIT_ARGUMENT, message=redact_secrets(str(error)))
        return EXIT_ARGUMENT
