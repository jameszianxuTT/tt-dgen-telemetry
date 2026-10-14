"""Minimal Prometheus text parsing for the metrics used by the dashboard."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>[^\s]+)(?:\s+\d+)?$"
)
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"')


@dataclass(frozen=True)
class Sample:
    value: float
    labels: dict[str, str]


def _unescape(value: str) -> str:
    return value.replace(r"\\", "\\").replace(r"\"", '"').replace(r"\n", "\n")


def parse_prometheus(text: str) -> dict[str, list[Sample]]:
    """Parse samples while retaining labels for future filtering."""
    metrics: dict[str, list[Sample]] = {}
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if not match:
            raise ValueError(f"invalid Prometheus sample on line {line_number}: {line!r}")
        try:
            value = float(match.group("value"))
        except ValueError as exc:
            raise ValueError(
                f"invalid Prometheus value on line {line_number}: {match.group('value')!r}"
            ) from exc
        labels_text = match.group("labels") or ""
        labels = {key: _unescape(value) for key, value in _LABEL.findall(labels_text)}
        metrics.setdefault(match.group("name"), []).append(Sample(value, labels))
    return metrics


def aggregate(metrics: dict[str, list[Sample]], name: str) -> float | None:
    """Sum all finite labeled series for a metric."""
    samples = metrics.get(name)
    if not samples:
        return None
    values = [sample.value for sample in samples if math.isfinite(sample.value)]
    return sum(values) if values else None
