#!/usr/bin/env bash

set -euo pipefail

pushd "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null

rm -f -- requirements*.txt

for input in *.in; do
    pip-compile \
        --upgrade \
        --no-strip-extras \
        --output-file="${input%.in}.txt" \
        "$input"
done

popd >/dev/null
