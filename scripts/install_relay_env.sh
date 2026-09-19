#!/usr/bin/env bash
set -euo pipefail
bundle_root="$(cd "$(dirname "$0")/.." && pwd)"
env_dir="${1:-$bundle_root/relay/program/.venv}"
test "$(uname -m)" = x86_64 || { echo 'Requires Linux x86_64' >&2; exit 1; }
/usr/bin/python3 -c 'import sys; assert sys.version_info[:2] == (3,10), "Requires Python 3.10"'
if [ -e "$env_dir" ]; then echo "Refusing existing environment: $env_dir" >&2; exit 1; fi
/usr/bin/python3 -m venv "$env_dir"
"$env_dir/bin/python" -m pip install --no-index --no-deps "$bundle_root"/relay/wheels/*.whl
"$env_dir/bin/python" -c 'from lerobot.datasets.lerobot_dataset import LeRobotDataset; import av, numpy, torch; print("Export imports OK", numpy.__version__, torch.__version__, av.__version__)'

