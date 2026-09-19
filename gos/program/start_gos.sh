#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="/home/user/agx_arm_ws/vendor-python:${PYTHONPATH:-}"
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
exec /usr/bin/python3 -u collector.py "$@"

