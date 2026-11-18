#!/usr/bin/env bash
# Uses disposable wallets and chain state. No external network.
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/pqc-chain-demo.XXXXXX")
trap 'rm -rf -- "$scratch"' EXIT
cd "$scratch"
chain() { "${PYTHON:-python3}" "$src/pqc_chain.py" "$@"; }

echo '=== 生成两个钱包和一个独立的出块钱包 ==='
alice=$(chain wallet-new alice.pem)
bob=$(chain wallet-new bob.pem)
producer=$(chain wallet-new producer.pem)
printf 'Alice=%s\nBob=%s\n出块者地址=%s\n' "$alice" "$bob" "$producer"

echo '=== 创世发币：全部分给 Alice ==='
genesis=$(chain init --treasury "$alice" --producer producer.pem)
printf '%s\n' "$genesis"
anchor=$(printf '%s\n' "$genesis" | "${PYTHON:-python3}" -c 'import json,sys; print(json.load(sys.stdin)["genesis_hash"])')

echo '=== Alice -> Bob：125；先验签入队，再出块 ==='
chain sign --wallet alice.pem --to "$bob" --amount 125 --genesis "$anchor" > alice-to-bob.json
chain submit alice-to-bob.json
chain balance "$bob"
chain produce --producer producer.pem

echo '=== 重放旧交易：必须返回退出码 1 ==='
rc=0
chain submit alice-to-bob.json || rc=$?
printf '实际退出码: %s\n' "$rc"
test "$rc" -eq 1

echo '=== Bob -> Alice：25；先篡改交易副本，再提交原版 ==='
chain sign --wallet bob.pem --to "$alice" --amount 25 --genesis "$anchor" > bob-to-alice.json
"${PYTHON:-python3}" -c 'import json,sys; t=json.load(sys.stdin); t["body"]["amount"]+=1; json.dump(t,sys.stdout)' \
  < bob-to-alice.json > tampered.json
rc=0
chain submit tampered.json || rc=$?
printf '实际退出码: %s\n' "$rc"
test "$rc" -eq 1
chain submit bob-to-alice.json
chain produce --producer producer.pem

echo '=== 独立进程从创世验链，再重算两份余额 ==='
chain verify --genesis "$anchor"
chain balance "$alice"
chain balance "$bob"
