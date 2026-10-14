# tt-d-gen telemetry TUI

A small live dashboard for Dynamo frontend and worker health and metrics. It is
independent of the launch runbook.

## Run with uvx

Run directly from a Git checkout without creating an environment:

```bash
uvx --from . tt-dgen-telemetry \
  --frontend 10.32.32.193 \
  --prefill-worker 10.32.32.193 \
  --decode-worker 10.32.88.129
```

The same command can install directly from the public GitHub URL:

```bash
uvx --from git+https://github.com/jameszianxuTT/tt-dgen-telemetry.git \
  tt-dgen-telemetry \
  --frontend 10.32.32.193 \
  --prefill-worker 10.32.32.193 \
  --decode-worker 10.32.88.129
```

## Local development

```bash
uv sync
uv run tt-dgen-telemetry \
  --frontend 10.32.32.193 \
  --prefill-worker 10.32.32.193 \
  --decode-worker 10.32.88.129
```

At least one endpoint is required.

The defaults are frontend port `8000`, prefill worker port `20021`, decode
worker port `20020`, and a one-second scrape interval. Override a port or the
interval when needed:

```bash
uvx --from . tt-dgen-telemetry \
  --decode-worker 10.32.88.129:21020 \
  --interval 2
```

Each endpoint option is repeatable. HTTP and HTTPS URLs are also accepted.
Press Ctrl-C to exit.

## Display

The tool polls `/health` and `/metrics`. A failed endpoint is shown as
unreachable while polling continues.

Worker panels include request counts, prompt tokens, local prefix-cache reuse,
disaggregated tokens received by decode, local prefill tokens, generated
tokens, slots, and KV block usage. The frontend panel includes request counts,
cached-token totals, input/output tokens, request latency, and time to first
token.

Cumulative counters show their total and their change per second. Gauges show
only their current value. Prometheus labels are retained by the parser but
series are summed by metric name in this initial version.
