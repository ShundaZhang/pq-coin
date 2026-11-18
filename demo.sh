#!/usr/bin/env bash
# Run offline. "--keep NEW_DIRECTORY" preserves throwaway keys and the ledger.
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
if [[ $# -eq 2 && "$1" == --keep ]]; then
    mkdir -- "$2"
    demo=$(cd "$2" && pwd)
elif [[ $# -eq 0 ]]; then
    demo=$(mktemp -d "${TMPDIR:-/tmp}/pqc-v2-demo.XXXXXX")
    trap 'rm -rf -- "$demo"' EXIT
else
    echo 'Usage: bash demo.sh [--keep NEW_DIRECTORY]' >&2
    exit 2
fi
cd "$demo"
pqc() { "${PYTHON:-python3}" "$src/pqc_v2.py" "$@"; }
get() { "${PYTHON:-python3}" -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
alice=$(pqc wallet-new alice.pem)
bob=$(pqc wallet-new bob.pem)
miner=$(pqc wallet-new miner.pem)
pqc init --treasury "$alice" --bits 8 > genesis.json
genesis=$(get genesis_hash < genesis.json)
chain() { pqc --genesis "$genesis" "$@"; }
secret=$("${PYTHON:-python3}" -c 'import secrets; print(secrets.token_hex(32))')
lock=$(printf %s "$secret" | "${PYTHON:-python3}" -c 'import hashlib,sys; print(hashlib.sha256(bytes.fromhex(sys.stdin.read())).hexdigest())')

echo '=== 创世发币；普通转账并进行 PoW ==='
cat genesis.json
chain sign --wallet alice.pem --fee 3 transfer --to "$bob" --amount 100 > tx.json
chain submit tx.json
chain mine --wallet miner.pem
chain checkpoint --height 1 > checkpoint1.json

echo '=== 部署哈希锁托管合约（锁定 150，支付 2 手续费） ==='
chain sign --wallet alice.pem --fee 2 escrow-deploy --beneficiary "$bob" --amount 150 \
  --hashlock "$lock" --ttl 4 > tx.json
chain submit tx.json > escrow.json
escrow=$(get contract < escrow.json)
cat escrow.json
chain mine --wallet miner.pem
chain contract "$escrow"
chain checkpoint --height 2 > checkpoint2.json

echo '=== Bob 提交正确原像领取；另一钱包挖矿 ==='
chain sign --wallet bob.pem --fee 4 escrow-claim "$escrow" --secret "$secret" > tx.json
chain submit tx.json
chain mine --wallet alice.pem
chain contract "$escrow"
chain balance "$alice"
chain balance "$bob"
chain balance "$miner"
chain verify
chain checkpoint > checkpoint.json
echo '=== 最新 Sepolia 检查点参数 ==='
cat checkpoint.json
if [[ $# -gt 0 ]]; then
    printf '已保留一次性演示数据：%s\n' "$demo"
fi
