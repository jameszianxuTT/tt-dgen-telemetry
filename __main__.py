"""Command-line entry point for ``python3 -m telemetry_tui``."""

from __future__ import annotations

import argparse

from .app import Endpoint, parse_endpoint, run


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="python3 -m telemetry_tui",
        description="Live health and metrics dashboard for a tt-d-gen deployment.",
    )
    result.add_argument(
        "--prefill-worker",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help="prefill worker system endpoint; default port 20021 (repeatable)",
    )
    result.add_argument(
        "--decode-worker",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help="decode worker system endpoint; default port 20020 (repeatable)",
    )
    result.add_argument(
        "--frontend",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help="Dynamo frontend endpoint; default port 8000 (repeatable)",
    )
    result.add_argument(
        "--interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="metrics scrape interval (default: 1)",
    )
    return result


def endpoints_from_args(args: argparse.Namespace, cli: argparse.ArgumentParser) -> list[Endpoint]:
    if args.interval <= 0:
        cli.error("--interval must be greater than zero")
    endpoints = []
    try:
        endpoints.extend(parse_endpoint("prefill", value) for value in args.prefill_worker)
        endpoints.extend(parse_endpoint("decode", value) for value in args.decode_worker)
        endpoints.extend(parse_endpoint("frontend", value) for value in args.frontend)
    except ValueError as exc:
        cli.error(str(exc))
    if not endpoints:
        cli.error(
            "at least one of --prefill-worker, --decode-worker, or --frontend is required"
        )
    return endpoints


def main() -> int:
    cli = parser()
    args = cli.parse_args()
    endpoints = endpoints_from_args(args, cli)
    try:
        run(endpoints, args.interval)
    except ModuleNotFoundError as exc:
        if exc.name == "rich":
            cli.error("Rich is required; install it with: python3 -m pip install rich")
        raise
    except RuntimeError as exc:
        cli.error(str(exc))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
