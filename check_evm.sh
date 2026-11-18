#!/usr/bin/env bash
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/pqc-v2-evm.XXXXXX")
trap 'rm -rf -- "$scratch"' EXIT
bash "$src/demo.sh" --keep "$scratch/sample"
cd "$src/evm"
node local_test.mjs "$scratch/sample/checkpoint1.json" "$scratch/sample/checkpoint2.json"
