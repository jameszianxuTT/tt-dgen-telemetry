"""Polling and live terminal rendering."""

from __future__ import annotations

import json
import os
import queue
import select
import shutil
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .metrics import Sample, aggregate, parse_prometheus

DEFAULT_PORTS = {"prefill": 20021, "decode": 20020, "frontend": 8000}


@dataclass(frozen=True)
class Endpoint:
    role: str
    address: str
    base_url: str

    @property
    def key(self) -> str:
        return f"{self.role}:{self.base_url}"


@dataclass
class Snapshot:
    endpoint: Endpoint
    scraped_at: float
    elapsed_ms: float
    health: dict = field(default_factory=dict)
    metrics: dict[str, list[Sample]] = field(default_factory=dict)
    health_error: str | None = None
    metrics_error: str | None = None


@dataclass(frozen=True)
class Metric:
    label: str
    name: str
    counter: bool = False


@dataclass
class ExploreState:
    source: int = 0
    cursor: int = 0


WORKER_COMMON = (
    Metric("Requests admitted", "tt_engine_admitted_total", True),
    Metric("Requests completed", "tt_engine_completed_total", True),
    Metric("Requests aborted", "tt_engine_aborted_total", True),
    Metric("Prompt tokens", "tt_engine_prompt_tokens_total", True),
    Metric("Prefix-cached tokens", "tt_engine_reused_tokens_total", True),
    Metric("Locally-prefilled tokens", "tt_engine_prompt_tokens_prefilled_total", True),
    Metric("Slots", "tt_engine_slots"),
    Metric("Free slots", "tt_engine_slots_free"),
    Metric("Pending admits", "tt_engine_pending_admits"),
    Metric("Stuck slots", "tt_engine_slots_stuck"),
    Metric("KV blocks used", "tt_engine_kv_blocks_used"),
    Metric("KV blocks cached", "tt_engine_kv_blocks_cached"),
)
DECODE_ONLY = (
    Metric("Disaggregated tokens", "tt_engine_migrated_tokens_total", True),
    Metric("Generated tokens", "tt_engine_generated_tokens_total", True),
)
FRONTEND_METRICS = (
    Metric("Active requests", "dynamo_frontend_active_requests"),
    Metric("Inflight requests", "dynamo_frontend_inflight_requests"),
    Metric("Requests started", "dynamo_frontend_requests_started_total", True),
    Metric("Requests completed", "dynamo_frontend_requests_total", True),
    Metric("Prefix-cached tokens", "dynamo_frontend_cached_tokens_sum", True),
    Metric("Cached-token samples", "dynamo_frontend_cached_tokens_count", True),
    Metric("Input tokens", "dynamo_frontend_input_sequence_tokens_sum", True),
    Metric("Output tokens", "dynamo_frontend_output_sequence_tokens_sum", True),
    Metric("Request latency", "dynamo_frontend_request_duration_seconds_sum", True),
    Metric("TTFT", "dynamo_frontend_time_to_first_token_seconds_sum", True),
)


def default_metric_names(role: str) -> set[str]:
    definitions = (
        FRONTEND_METRICS
        if role == "frontend"
        else WORKER_COMMON + (DECODE_ONLY if role == "decode" else ())
    )
    return {metric.name for metric in definitions}


def is_counter(name: str) -> bool:
    return name.endswith(("_total", "_sum", "_count"))


def parse_endpoint(role: str, value: str) -> Endpoint:
    """Parse HOST[:PORT] or an HTTP URL and apply the role's default port."""
    original = value
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid {role} endpoint: {original!r}")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"{role} endpoint must not include a path: {original!r}")
    try:
        port = parsed.port or DEFAULT_PORTS[role]
    except ValueError as exc:
        raise ValueError(f"invalid port in {role} endpoint: {original!r}") from exc
    host = parsed.hostname
    rendered_host = f"[{host}]" if ":" in host else host
    base_url = f"{parsed.scheme}://{rendered_host}:{port}"
    return Endpoint(role=role, address=f"{rendered_host}:{port}", base_url=base_url)


def _read(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "tt-telemetry-tui/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def scrape(endpoint: Endpoint, timeout: float) -> Snapshot:
    started = time.monotonic()
    snapshot = Snapshot(endpoint=endpoint, scraped_at=time.time(), elapsed_ms=0)
    try:
        health = json.loads(_read(f"{endpoint.base_url}/health", timeout))
        if not isinstance(health, dict):
            raise ValueError("response is not a JSON object")
        snapshot.health = health
    except (OSError, ValueError, urllib.error.URLError) as exc:
        snapshot.health_error = str(exc)
    try:
        snapshot.metrics = parse_prometheus(_read(f"{endpoint.base_url}/metrics", timeout))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        snapshot.metrics_error = str(exc)
    snapshot.elapsed_ms = (time.monotonic() - started) * 1000
    return snapshot


class KeyReader:
    """Read keys on a daemon thread while the main thread continues scraping."""

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.original = None
        self.keys: queue.SimpleQueue[str] = queue.SimpleQueue()

    def __enter__(self):
        if not os.isatty(self.fd):
            raise RuntimeError("the telemetry TUI requires an interactive terminal")
        self.original = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def __exit__(self, *_):
        if self.original is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original)

    def _read(self) -> None:
        while True:
            data = os.read(self.fd, 1)
            if not data:
                return
            if data == b"\x1b":
                while select.select([self.fd], [], [], 0.02)[0]:
                    data += os.read(self.fd, 1)
            self.keys.put(
                {
                    b"\x1b[A": "up",
                    b"\x1b[B": "down",
                    b"\x1b[Z": "backtab",
                    b"\t": "tab",
                    b" ": "toggle",
                }.get(data, data.decode(errors="ignore"))
            )

    def drain(self) -> list[str]:
        result = []
        while True:
            try:
                result.append(self.keys.get_nowait())
            except queue.Empty:
                return result


class Dashboard:
    def __init__(self, endpoints: list[Endpoint], interval: float):
        self.endpoints = endpoints
        self.interval = interval
        self.previous: dict[tuple[str, str], tuple[float, float]] = {}
        self.available: dict[str, set[str]] = {endpoint.key: set() for endpoint in endpoints}
        self.enabled: dict[str, set[str]] = {
            endpoint.key: default_metric_names(endpoint.role) for endpoint in endpoints
        }

    def poll(self) -> list[Snapshot]:
        timeout = min(max(self.interval * 0.8, 0.5), 3.0)
        with ThreadPoolExecutor(max_workers=len(self.endpoints)) as pool:
            snapshots = list(pool.map(lambda endpoint: scrape(endpoint, timeout), self.endpoints))
        for snapshot in snapshots:
            self.available[snapshot.endpoint.key].update(snapshot.metrics)
        return snapshots

    def _rate(self, snapshot: Snapshot, metric: Metric, value: float | None) -> float | None:
        if value is None or not metric.counter:
            return None
        key = (snapshot.endpoint.key, metric.name)
        old = self.previous.get(key)
        self.previous[key] = (snapshot.scraped_at, value)
        if old is None:
            return None
        elapsed = snapshot.scraped_at - old[0]
        delta = value - old[1]
        return delta / elapsed if elapsed > 0 and delta >= 0 else None

    def _selected_metrics(self, endpoint: Endpoint) -> list[Metric]:
        defaults = (
            list(FRONTEND_METRICS)
            if endpoint.role == "frontend"
            else list(WORKER_COMMON + (DECODE_ONLY if endpoint.role == "decode" else ()))
        )
        selected = self.enabled[endpoint.key]
        result = [metric for metric in defaults if metric.name in selected]
        known = {metric.name for metric in defaults}
        result.extend(
            Metric(name, name, is_counter(name))
            for name in sorted(selected - known)
        )
        return result

    def render(self, snapshots: list[Snapshot]):
        from rich.columns import Columns
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        panels = []
        for snapshot in snapshots:
            status = snapshot.health.get("status")
            if snapshot.health_error:
                health = Text(f"unreachable: {snapshot.health_error}", style="bold red")
            elif status in {"ready", "healthy"}:
                health = Text(str(status), style="bold green")
            else:
                health = Text(str(status or "unknown"), style="bold yellow")

            table = Table(expand=True, box=None, pad_edge=False)
            table.add_column("Metric")
            table.add_column("Value", justify="right")
            table.add_column("/ sec", justify="right")
            for metric in self._selected_metrics(snapshot.endpoint):
                value = aggregate(snapshot.metrics, metric.name)
                rate = self._rate(snapshot, metric, value)
                table.add_row(
                    metric.label,
                    "—" if value is None else _format(value),
                    "—" if rate is None else _format(rate),
                )

            details = Text()
            details.append("health ", style="dim")
            details.append_text(health)
            details.append(f"   scrape {snapshot.elapsed_ms:.0f} ms", style="dim")
            if snapshot.metrics_error:
                details.append(f"\nmetrics error: {snapshot.metrics_error}", style="red")
            elif snapshot.endpoint.role == "frontend":
                details.append(
                    f"\ninstances: {len(snapshot.health.get('instances', []))}",
                    style="dim",
                )
            panels.append(
                Panel(
                    Group(details, table),
                    title=f"{snapshot.endpoint.role}  {snapshot.endpoint.address}",
                    border_style="cyan",
                )
            )
        header = Text(
            f"tt-d-gen telemetry   refresh {self.interval:g}s   e metrics   q exit",
            style="bold",
        )
        return Group(header, Columns(panels, equal=True, expand=True))

    def render_explorer(self, snapshots: list[Snapshot], state: ExploreState):
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        state.source %= len(snapshots)
        snapshot = snapshots[state.source]
        names = sorted(self.available[snapshot.endpoint.key])
        if names:
            state.cursor = min(max(state.cursor, 0), len(names) - 1)
        else:
            state.cursor = 0
        height = max(8, shutil.get_terminal_size().lines - 9)
        start = max(0, min(state.cursor - height // 2, len(names) - height))
        visible = names[start : start + height]

        table = Table(expand=True, box=None, pad_edge=False)
        table.add_column("", width=3)
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_column("/ sec", justify="right")
        enabled = self.enabled[snapshot.endpoint.key]
        for index, name in enumerate(visible, start):
            value = aggregate(snapshot.metrics, name)
            metric = Metric(name, name, is_counter(name))
            rate = self._rate(snapshot, metric, value)
            style = "reverse" if index == state.cursor else None
            table.add_row(
                "[x]" if name in enabled else "[ ]",
                name,
                "—" if value is None else _format(value),
                "—" if rate is None else _format(rate),
                style=style,
            )

        sources = "  ".join(
            (
                f"[bold reverse] {item.endpoint.role}:{item.endpoint.address} [/]"
                if index == state.source
                else f"[dim]{item.endpoint.role}:{item.endpoint.address}[/]"
            )
            for index, item in enumerate(snapshots)
        )
        help_text = Text(
            "↑/↓ or j/k move   Space toggle   Tab source   "
            "a all   n none   d defaults   q/Esc back",
            style="dim",
        )
        return Group(
            Text.from_markup(f"[bold]Metric explorer[/]   {sources}"),
            Panel(table, title=f"{len(names)} metrics available"),
            help_text,
        )

    def handle_explorer_key(self, state: ExploreState, key: str) -> bool:
        """Apply an explorer key. Return False when the submenu should close."""
        endpoint = self.endpoints[state.source]
        names = sorted(self.available[endpoint.key])
        if key in {"q", "\x1b"}:
            return False
        if key in {"tab", "backtab"}:
            step = -1 if key == "backtab" else 1
            state.source = (state.source + step) % len(self.endpoints)
            state.cursor = 0
            return True
        if key in {"up", "k"}:
            state.cursor = max(0, state.cursor - 1)
        elif key in {"down", "j"}:
            state.cursor = min(max(0, len(names) - 1), state.cursor + 1)
        elif key == "toggle" and names:
            name = names[state.cursor]
            selected = self.enabled[endpoint.key]
            selected.remove(name) if name in selected else selected.add(name)
        elif key == "a":
            self.enabled[endpoint.key] = set(names)
        elif key == "n":
            self.enabled[endpoint.key].clear()
        elif key == "d":
            self.enabled[endpoint.key] = default_metric_names(endpoint.role)
        return True


def _format(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.2f}k"
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}"


def run(endpoints: list[Endpoint], interval: float) -> None:
    from rich.live import Live

    dashboard = Dashboard(endpoints, interval)
    snapshots = dashboard.poll()
    explorer: ExploreState | None = None
    with KeyReader() as keys, Live(
        dashboard.render(snapshots),
        refresh_per_second=8,
        screen=True,
    ) as live:
        while True:
            started = time.monotonic()
            snapshots = dashboard.poll()
            deadline = started + interval
            while True:
                changed = False
                for key in keys.drain():
                    if explorer is not None:
                        if not dashboard.handle_explorer_key(explorer, key):
                            explorer = None
                        changed = True
                    elif key == "e":
                        explorer = ExploreState()
                        changed = True
                    elif key == "q":
                        return
                if changed or time.monotonic() >= deadline:
                    display = (
                        dashboard.render_explorer(snapshots, explorer)
                        if explorer is not None
                        else dashboard.render(snapshots)
                    )
                    live.update(display, refresh=True)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
