#!/usr/bin/env bash

set -euo pipefail

if (( $# > 1 )); then
  echo "usage: $0 [profile]" >&2
  exit 2
fi

requirements=requirements.txt
if (( $# == 1 )); then
  requirements="requirements-$1.txt"
fi

ludos_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ludos_dir"

if [[ ! -f "$requirements" ]]; then
  echo "requirements file not found: $ludos_dir/$requirements" >&2
  exit 2
fi

python3 -m venv ../venv
../venv/bin/pip install --upgrade pip
../venv/bin/pip install -r "$requirements"
