from __future__ import annotations

import argparse
import contextlib
import io
import unittest

from telemetry_tui.__main__ import endpoints_from_args, parser
from telemetry_tui.app import Dashboard, Metric, Snapshot, parse_endpoint
from telemetry_tui.metrics import aggregate, parse_prometheus


class PrometheusTests(unittest.TestCase):
    def test_parses_labels_and_aggregates_series(self):
        metrics = parse_prometheus(
            """
# HELP requests example
requests_total{role="decode",model="a"} 2
requests_total{role="decode",model="b"} 3.5
message{value="line\\nquoted\\\""} 1
"""
        )
        self.assertEqual(aggregate(metrics, "requests_total"), 5.5)
        self.assertEqual(metrics["message"][0].labels["value"], 'line\nquoted"')

    def test_missing_metric_is_none(self):
        self.assertIsNone(aggregate({}, "missing"))

    def test_rejects_malformed_sample(self):
        with self.assertRaisesRegex(ValueError, "line 1"):
            parse_prometheus("not a sample")


class EndpointTests(unittest.TestCase):
    def test_default_and_explicit_ports(self):
        self.assertEqual(
            parse_endpoint("prefill", "10.0.0.1").base_url,
            "http://10.0.0.1:20021",
        )
        self.assertEqual(
            parse_endpoint("decode", "host.example:21020").base_url,
            "http://host.example:21020",
        )
        self.assertEqual(
            parse_endpoint("frontend", "https://host.example:8443").base_url,
            "https://host.example:8443",
        )

    def test_rejects_url_path(self):
        with self.assertRaisesRegex(ValueError, "must not include a path"):
            parse_endpoint("frontend", "http://host.example:8000/metrics")

    def test_cli_requires_an_endpoint(self):
        cli = parser()
        args = argparse.Namespace(
            interval=1,
            prefill_worker=[],
            decode_worker=[],
            frontend=[],
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            endpoints_from_args(args, cli)


class RateTests(unittest.TestCase):
    def test_counter_rate_uses_successive_scrapes(self):
        endpoint = parse_endpoint("decode", "decode")
        dashboard = Dashboard([endpoint], 1)
        metric = Metric("Tokens", "tokens_total", counter=True)
        first = Snapshot(endpoint, scraped_at=10, elapsed_ms=1)
        second = Snapshot(endpoint, scraped_at=12, elapsed_ms=1)
        self.assertIsNone(dashboard._rate(first, metric, 20))
        self.assertEqual(dashboard._rate(second, metric, 30), 5)

    def test_counter_reset_has_no_negative_rate(self):
        endpoint = parse_endpoint("decode", "decode")
        dashboard = Dashboard([endpoint], 1)
        metric = Metric("Tokens", "tokens_total", counter=True)
        dashboard._rate(Snapshot(endpoint, 10, 1), metric, 20)
        self.assertIsNone(dashboard._rate(Snapshot(endpoint, 11, 1), metric, 1))


if __name__ == "__main__":
    unittest.main()
