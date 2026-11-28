#!/usr/bin/env bash
# One-command installer for YEMENDEV AI (single Linux/macOS host).
#
#   scripts/install.sh [--data-dir DIR] [--actor NAME] [--token-file FILE] [--python PY]
#                      [--no-ui] [--with-sandbox-image] [--with-desktop] [--otel] [--dev] [--dry-run]
#
# Safe to run again: dependencies are updated, an existing data directory is never re-initialised and
# no new admin token is issued. Network is needed only for packages (PyPI, npm) and, with
# --with-sandbox-image, the python:3.13-slim base image; the platform itself runs offline.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${YEMENDEV_DATA_DIR:-$REPO/.yemendev}"
ACTOR="${USER:-admin}"
TOKEN_FILE=""
PYTHON=""
UI=1
SANDBOX_IMAGE=0
DESKTOP=0
EXTRAS=""
DRY=0

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "install: error: $*" >&2; exit 1; }
step() { echo "==> $*"; }
run() { if [ "$DRY" = 1 ]; then echo "    would run: $*"; else "$@"; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --data-dir) DATA_DIR="${2:?--data-dir needs a value}"; shift 2 ;;
    --actor) ACTOR="${2:?--actor needs a value}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:?--token-file needs a value}"; shift 2 ;;
    --python) PYTHON="${2:?--python needs a value}"; shift 2 ;;
    --no-ui) UI=0; shift ;;
    --with-sandbox-image) SANDBOX_IMAGE=1; shift ;;
    --with-desktop) DESKTOP=1; shift ;;
    --otel) EXTRAS="${EXTRAS:+$EXTRAS,}otel"; shift ;;
    --dev) EXTRAS="${EXTRAS:+$EXTRAS,}dev"; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done
case "$ACTOR" in *[!A-Za-z0-9_.@-]*|"") die "--actor may only contain letters, digits and _.@-" ;; esac

# ---- 1. requirements ------------------------------------------------------------------------------
step "checking requirements"
if [ -z "$PYTHON" ]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then PYTHON="$(command -v "$c")"; break; fi
  done
fi
if [ -z "$PYTHON" ] || ! command -v "$PYTHON" >/dev/null 2>&1; then die "Python 3.11 or newer is required"; fi
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "$PYTHON is $("$PYTHON" -V 2>&1); Python 3.11 or newer is required"
echo "    python: $("$PYTHON" -V 2>&1) ($PYTHON)"
if [ "$UI" = 1 ] && ! command -v npm >/dev/null 2>&1; then
  echo "    node/npm: not found - the web UI will not be built (the API and CLI work without it)"
  UI=0
fi
if [ "$DESKTOP" = 1 ]; then
  [ "$UI" = 1 ] || die "--with-desktop needs the web UI and npm (do not combine with --no-ui)"
  command -v cargo >/dev/null 2>&1 || die "--with-desktop needs Rust (cargo) and the WebKitGTK libraries; see docs/DESKTOP.md"
fi
HAVE_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then HAVE_DOCKER=1; fi
echo "    docker: $([ "$HAVE_DOCKER" = 1 ] && echo available || echo 'not available (process sandbox will be used)')"
# As root, the process sandbox runs project tests as an unprivileged user (uid 65534): it must be able to
# reach this installation's Python (every parent directory needs o+x).
if [ "$(id -u)" = 0 ]; then
  d="$REPO"
  while [ "$d" != "/" ]; do
    perms="$(stat -c '%a' "$d" 2>/dev/null || stat -f '%Lp' "$d")"
    if [ $(( 8#$perms & 1 )) -eq 0 ]; then
      echo "    WARNING: $d is not traversable by other users (mode $perms): the process sandbox will refuse"
      echo "             to run tests. Use the docker sandbox (--with-sandbox-image) or install elsewhere."
      break
    fi
    d="$(dirname "$d")"
  done
fi

# ---- 2. Python environment ------------------------------------------------------------------------
VENV="$REPO/.venv"
SPEC="$REPO${EXTRAS:+[$EXTRAS]}"
step "installing the platform into $VENV"
if command -v uv >/dev/null 2>&1; then
  [ -x "$VENV/bin/python" ] || run uv venv --python "$PYTHON" "$VENV"
  run uv pip install --python "$VENV/bin/python" -e "$SPEC"
else
  [ -x "$VENV/bin/python" ] || run "$PYTHON" -m venv "$VENV"
  run "$VENV/bin/python" -m pip install --upgrade pip
  run "$VENV/bin/python" -m pip install -e "$SPEC"
fi
YD="$VENV/bin/yemendev"

# ---- 3. web UI ------------------------------------------------------------------------------------
if [ "$UI" = 1 ]; then
  step "building the web UI (apps/web)"
  run npm --prefix "$REPO/apps/web" ci --no-audit --no-fund
  run npm --prefix "$REPO/apps/web" run build
fi

# ---- 4. data directory and first admin token ------------------------------------------------------
export YEMENDEV_DATA_DIR="$DATA_DIR"
if [ -f "$DATA_DIR/yemendev.sqlite3" ]; then
  step "data directory $DATA_DIR already initialised (no new token issued)"
  [ "$DRY" = 1 ] || "$YD" health >/dev/null   # applies pending migrations
else
  step "initialising $DATA_DIR (first admin: $ACTOR)"
  if [ "$DRY" = 1 ]; then
    echo "    would run: $YD init --actor $ACTOR"
  else
    out="$("$YD" init --actor "$ACTOR")"
    token="$(printf '%s\n' "$out" | tail -n 1)"
    case "$token" in ydv_*) ;; *) die "could not read the admin token from 'yemendev init'" ;; esac
    if [ -n "$TOKEN_FILE" ]; then
      (umask 077; printf '%s\n' "$token" > "$TOKEN_FILE")
      echo "    admin token written to $TOKEN_FILE (mode 0600); it is not shown again"
    else
      echo "    admin token for '$ACTOR' (shown once, store it securely):"
      echo "    $token"
    fi
  fi
fi

# ---- 5. container sandbox image (optional) --------------------------------------------------------
if [ "$SANDBOX_IMAGE" = 1 ]; then
  [ "$HAVE_DOCKER" = 1 ] || die "--with-sandbox-image needs a running Docker daemon"
  step "building the container sandbox image"
  run docker pull python:3.13-slim
  # include Node.js when it is installed, so JavaScript/TypeScript suites can also run in containers
  NODE_ARG=""
  if command -v node >/dev/null 2>&1; then NODE_ARG="--with-node"; fi
  if [ "$DRY" = 1 ]; then echo "    would run: $YD sandbox build-image $NODE_ARG"
  elif [ -n "${token:-}" ]; then YEMENDEV_TOKEN="$token" "$YD" sandbox build-image $NODE_ARG
  elif [ -n "${YEMENDEV_TOKEN:-}" ]; then "$YD" sandbox build-image $NODE_ARG
  else echo "    skipped: set YEMENDEV_TOKEN (an admin token) and run: $YD sandbox build-image $NODE_ARG"
  fi
fi

# ---- 6. desktop shell (optional) -------------------------------------------------------------------
if [ "$DESKTOP" = 1 ]; then
  step "building the desktop shell (Tauri)"
  run npm --prefix "$REPO/apps/desktop" ci --no-audit --no-fund
  run npm --prefix "$REPO/apps/desktop" run build:binary
  CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/yemendev"
  if [ "$DRY" = 1 ]; then echo "    would write: $CONF_DIR/desktop.json"
  else
    mkdir -p "$CONF_DIR"
    "$PYTHON" -c 'import json,sys; print(json.dumps({"bin": sys.argv[1], "data_dir": sys.argv[2]}, indent=2))' \
      "$YD" "$DATA_DIR" > "$CONF_DIR/desktop.json"
    echo "    wrote $CONF_DIR/desktop.json (the shell starts this platform with this data directory)"
  fi
  echo "    app: $REPO/apps/desktop/src-tauri/target/release/yemendev-desktop"
fi

# ---- 7. verify ------------------------------------------------------------------------------------
step "verifying"
if [ "$DRY" = 1 ]; then
  echo "    would run: $YD health"
else
  "$YD" health
fi
cat <<EOF

YEMENDEV AI is installed.
  data directory : $DATA_DIR
  start          : YEMENDEV_DATA_DIR="$DATA_DIR" $YD serve      (then open http://127.0.0.1:8765/)
  command line   : export YEMENDEV_TOKEN=<admin token>; $YD status
EOF
