# Installation

## Requirements

| Component | Requirement | Notes |
|---|---|---|
| OS | Linux (verified), macOS (expected, not verified) | Windows not supported yet |
| Python | 3.11 or newer | verified on 3.13 |
| Memory | 4 GB minimum, 8 GB recommended | |
| Docker | Recommended | enables the container sandbox (`auto` default) |
| Model | Optional | any OpenAI-compatible server; the platform works without one |
| Internet | Not required at runtime | needed once to install packages / build the sandbox image / build the UI |
| Node.js | 20+ to build the web UI | not needed at runtime; the built UI is static files |

## Install

```bash
git clone <repository> && cd YM_DEV
uv venv && uv pip install -e .          # or: python -m venv .venv && .venv/bin/pip install -e .
export YEMENDEV_DATA_DIR=$PWD/.yemendev
.venv/bin/yemendev init --actor <your-name>   # prints an admin token ONCE - store it securely
export YEMENDEV_TOKEN=<that token>
.venv/bin/yemendev health
```

## Web UI

```bash
cd apps/web && npm ci && npm run build && cd ../..   # type-checks (incl. Arabic strings) and builds dist/
.venv/bin/yemendev serve                             # then open http://127.0.0.1:8765/ and paste a token
```

The UI is Arabic by default (switch in the header). It stores the token only for the browser tab.
To serve a UI built elsewhere, set `YEMENDEV_WEB_DIR=/path/to/dist`. Without a build, `/app` returns a
404 that says how to build it; the API is unaffected.

## Container sandbox (recommended)

```bash
docker pull python:3.13-slim                 # the only network step; images are never pulled implicitly
.venv/bin/yemendev sandbox build-image       # builds with --network none from host-downloaded wheels
.venv/bin/yemendev sandbox status            # shows the effective backend and any warnings
```

Fully offline machine: copy the base image (`docker save`/`docker load`) and a directory of wheels, then
`yemendev sandbox build-image --wheels <dir>`.

Projects with third-party dependencies: `yemendev sandbox build-image --project <project_id>` (admin).

## Optional: tracing export

`uv pip install -e '.[otel]'` adds the OpenTelemetry SDK and OTLP exporter; enable with `YEMENDEV_OTEL=otlp`
(or `console`). Without it the platform still emits logs and metrics and honours incoming `traceparent`.

## Optional: a local model

Run any OpenAI-compatible server (for example llama.cpp's server or Ollama), then set
`YEMENDEV_MODEL_BASE_URL` and `YEMENDEV_MODEL_NAME` (see `.env.example`). Model support is
**tested only against a protocol stub**; no real model has been evaluated yet.

## Verify the installation

```bash
.venv/bin/yemendev status        # schema version, counts, sandbox isolation report
.venv/bin/yemendev bench --seed 7  # 15 resilience scenarios on your machine (about 30-60 s)
```
