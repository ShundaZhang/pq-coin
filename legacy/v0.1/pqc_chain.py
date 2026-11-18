#!/usr/bin/env python3
"""Local, single-producer blockchain demo. Standard library + OpenSSL."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile


SUPPLY = 1_000_000
MAX_TX = 64
MAX_NONCE = (1 << 64) - 1
VERSION = "pqcchain-v1"
ALGORITHM = "ML-DSA-44"
PUB_BYTES = 1312
SIG_BYTES = 2420
# Canonical DER SubjectPublicKeyInfo prefix for ML-DSA-44 (OID 2.16.840.1.101.3.4.3.17);
# algorithm parameters absent; BIT STRING holds a 1312-byte raw public key.
SPKI_PREFIX = bytes.fromhex("30820532300b06096086480165030403110382052100")


class ChainError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ChainError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def payload(domain, value):
    return b"pqcchain-v1/" + domain.encode("ascii") + b"\x00" + canonical(value)


def digest(domain, value):
    return hashlib.sha256(payload(domain, value)).hexdigest()


def fields(value, keys, label):
    require(type(value) is dict and set(value) == set(keys.split()), f"{label}字段不匹配")


def hex_bytes(value, size, label):
    require(type(value) is str and len(value) == size * 2
            and re.fullmatch(r"[0-9a-f]+", value) is not None, f"{label}格式错误")
    return bytes.fromhex(value)


def integer(value, lo, hi, label):
    require(type(value) is int and lo <= value <= hi, f"{label}必须是 {lo} 到 {hi} 的整数")


def address(raw_key):
    return "pq1" + hashlib.sha256(b"pqcchain-v1/address\x00" + raw_key).hexdigest()


def check_address(value):
    require(type(value) is str and value.startswith("pq1"), "地址格式错误")
    hex_bytes(value[3:], 32, "地址")


def openssl(*args):
    result = subprocess.run([os.environ.get("OPENSSL", "openssl"), *map(str, args)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=15, check=False)
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        hint = detail.splitlines()[0] if detail else result.stdout.decode(errors="replace").strip()
        raise ChainError(f"OpenSSL {args[0]} 失败（退出码 {result.returncode}）: {hint}")
    return result.stdout


def public_key(wallet):
    der = openssl("pkey", "-in", wallet, "-passin", "pass:", "-pubout", "-outform", "DER")
    require(der.startswith(SPKI_PREFIX) and len(der) == len(SPKI_PREFIX) + PUB_BYTES,
            f"钱包必须使用 {ALGORITHM}")
    return der[len(SPKI_PREFIX):]


def new_wallet(wallet):
    require(not wallet.exists(), f"钱包文件已存在: {wallet}")
    private_key = openssl("genpkey", "-algorithm", ALGORITHM)
    fd = os.open(wallet, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(private_key)
            stream.flush()
            os.fsync(stream.fileno())
        return address(public_key(wallet))
    except Exception:
        wallet.unlink()
        raise


def sign(wallet, domain, value):
    with tempfile.TemporaryDirectory(prefix="pqc-sign-") as tmp:
        message = Path(tmp) / "message"
        message.write_bytes(payload(domain, value))
        sig = openssl("pkeyutl", "-sign", "-rawin", "-inkey", wallet, "-passin", "pass:",
                      "-in", message)
    require(len(sig) == SIG_BYTES, "签名长度不符合 ML-DSA-44")
    return sig.hex()


def verify_signature(raw_key, sig, domain, value):
    with tempfile.TemporaryDirectory(prefix="pqc-verify-") as tmp:
        message, pubkey, sigfile = [Path(tmp) / name for name in ("message", "pub.der", "sig")]
        message.write_bytes(payload(domain, value))
        pubkey.write_bytes(SPKI_PREFIX + raw_key)
        sigfile.write_bytes(sig)
        try:
            openssl("pkeyutl", "-verify", "-rawin", "-pubin", "-keyform", "DER",
                    "-inkey", pubkey, "-in", message, "-sigfile", sigfile)
        except ChainError as exc:
            raise ChainError(f"{domain} 签名校验失败: {exc}") from exc


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, f"JSON 含重复字段: {key}")
        obj[key] = value
    return obj


def read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=unique_object)


def write_state(path, state):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as tmp:
        temp_path = Path(tmp.name)
        try:
            tmp.write(canonical(state) + b"\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()


@contextlib.contextmanager
def chain_lock(path):
    # Stable sidecar inode: replacing the state file must not invalidate this lock.
    with path.with_name(path.name + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def make_genesis(treasury, producer):
    check_address(treasury)
    return {"version": VERSION, "network_tag": secrets.token_hex(16), "supply": SUPPLY,
            "treasury": treasury, "producer_key": producer.hex()}


def apply_tx(tx, chain_id, balances, nonces):
    fields(tx, "body public_key signature", "交易")
    body = tx["body"]
    fields(body, "chain_id from to amount nonce", "交易正文")
    require(body["chain_id"] == chain_id, "交易 chain_id 与本链不匹配")
    sender, recipient, amount, nonce = (body[key] for key in ("from", "to", "amount", "nonce"))
    check_address(sender)
    check_address(recipient)
    integer(amount, 1, SUPPLY, "金额")
    integer(nonce, 0, MAX_NONCE, "nonce")
    raw_key = hex_bytes(tx["public_key"], PUB_BYTES, "交易公钥")
    sig = hex_bytes(tx["signature"], SIG_BYTES, "交易签名")
    require(address(raw_key) == sender, "签名公钥与发送者地址不匹配")
    expected = nonces.get(sender, 0)
    require(nonce == expected, f"nonce 不匹配：预期 {expected}，收到 {nonce}")
    require(balances.get(sender, 0) >= amount, "余额不足")
    verify_signature(raw_key, sig, "tx", body)
    balances[sender] -= amount
    balances[recipient] = balances.get(recipient, 0) + amount
    nonces[sender] = nonce + 1


def validate(state, pinned_genesis=None):
    """Rebuild balances by replay; the pending pool is validated separately."""
    fields(state, "genesis blocks pending", "链文件")
    genesis = state["genesis"]
    fields(genesis, "version network_tag supply treasury producer_key", "创世区块")
    require(genesis["version"] == VERSION, "不支持的链版本")
    hex_bytes(genesis["network_tag"], 16, "创世网络标识")
    integer(genesis["supply"], SUPPLY, SUPPLY, "总发行量")
    check_address(genesis["treasury"])
    producer_key = hex_bytes(genesis["producer_key"], PUB_BYTES, "创世出块者公钥")
    chain_id = digest("genesis", genesis)
    if pinned_genesis is not None:
        hex_bytes(pinned_genesis, 32, "创世锚点")
        require(pinned_genesis == chain_id, "创世锚点不匹配")
    blocks, pending = state["blocks"], state["pending"]
    require(type(blocks) is list, "区块列表格式错误")
    require(type(pending) is list and len(pending) <= MAX_TX, "待打包列表格式错误或超长")
    balances, nonces, tip = {genesis["treasury"]: SUPPLY}, {}, chain_id
    for height, block in enumerate(blocks, 1):
        try:
            fields(block, "body signature", "区块")
            body = block["body"]
            fields(body, "height prev transactions", "区块正文")
            integer(body["height"], height, height, "区块高度")
            require(body["prev"] == tip, "前块哈希不匹配")
            txs = body["transactions"]
            require(type(txs) is list and 1 <= len(txs) <= MAX_TX, "区块交易数不合法")
            verify_signature(producer_key, hex_bytes(block["signature"], SIG_BYTES, "区块签名"),
                             "block", body)
            for tx in txs:
                apply_tx(tx, chain_id, balances, nonces)
            tip = digest("block-id", block)
        except ChainError as exc:
            raise ChainError(f"区块 #{height}: {exc}") from exc
    require(sum(balances.values()) == SUPPLY, "已确认余额之和不等于固定发行量")
    pending_balances, pending_nonces = balances.copy(), nonces.copy()
    for index, tx in enumerate(pending, 1):
        try:
            apply_tx(tx, chain_id, pending_balances, pending_nonces)
        except ChainError as exc:
            raise ChainError(f"待打包交易 #{index}: {exc}") from exc
    require(sum(pending_balances.values()) == SUPPLY, "待打包余额之和不等于固定发行量")
    return chain_id, tip, balances, nonces, pending_balances, pending_nonces


def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def cli():
    parser = argparse.ArgumentParser(description="本地单节点 PQC 链（仅教学用途）")
    parser.add_argument("--chain", type=Path, default=Path("chain.json"), help="链 JSON 文件")
    sub = parser.add_subparsers(dest="command", required=True)
    wallet = sub.add_parser("wallet-new", help="生成未加密的 ML-DSA-44 PEM 私钥，输出地址")
    wallet.add_argument("wallet", type=Path)
    show = sub.add_parser("address", help="从钱包读取地址")
    show.add_argument("wallet", type=Path)
    init = sub.add_parser("init", help="创建创世区块并固定总量")
    init.add_argument("--treasury", required=True)
    init.add_argument("--producer", type=Path, required=True)
    tx = sub.add_parser("sign", help="签名转账，输出交易 JSON（不改账本）")
    tx.add_argument("--wallet", type=Path, required=True)
    tx.add_argument("--to", required=True)
    tx.add_argument("--amount", type=int, required=True)
    tx.add_argument("--genesis", help="校验独立保存的创世哈希")
    submit = sub.add_parser("submit", help="验签并将交易加入待打包队列")
    submit.add_argument("tx", type=Path)
    produce = sub.add_parser("produce", help="用指定的出块钱包打包待确认交易")
    produce.add_argument("--producer", type=Path, required=True)
    balance = sub.add_parser("balance", help="显示已确认余额和待打包后的预计余额")
    balance.add_argument("address")
    verify = sub.add_parser("verify", help="从创世区块验链并重算余额")
    verify.add_argument("--genesis", help="校验独立保存的创世哈希")
    args = parser.parse_args()

    if args.command == "wallet-new":
        print(new_wallet(args.wallet.resolve()))
        return
    if args.command == "address":
        print(address(public_key(args.wallet.resolve())))
        return

    path = args.chain.resolve()
    with chain_lock(path):
        if args.command == "init":
            require(not path.exists(), f"链文件已存在: {path}")
            genesis = make_genesis(args.treasury, public_key(args.producer.resolve()))
            state = {"genesis": genesis, "blocks": [], "pending": []}
            chain_id = validate(state)[0]
            write_state(path, state)
            emit({"genesis_hash": chain_id, "total_supply": SUPPLY, "treasury": args.treasury})
            return

        state = read_json(path)
        chain_id, tip, balances, nonces, pending_balances, pending_nonces = validate(
            state, getattr(args, "genesis", None))
        if args.command == "sign":
            check_address(args.to)
            integer(args.amount, 1, SUPPLY, "金额")
            wallet = args.wallet.resolve()
            public = public_key(wallet)
            sender = address(public)
            nonce = pending_nonces.get(sender, 0)
            integer(nonce, 0, MAX_NONCE, "nonce")
            body = {"from": sender, "to": args.to, "amount": args.amount, "nonce": nonce,
                    "chain_id": chain_id}
            emit({"body": body, "public_key": public.hex(), "signature": sign(wallet, "tx", body)})
        elif args.command == "submit":
            require(len(state["pending"]) < MAX_TX, "待打包队列已满")
            tx = read_json(args.tx)
            apply_tx(tx, chain_id, pending_balances, pending_nonces)
            state["pending"].append(tx)
            write_state(path, state)
            emit({"txid": digest("tx-id", tx), "pending": len(state["pending"])})
        elif args.command == "produce":
            require(state["pending"], "无待打包交易")
            wallet = args.producer.resolve()
            require(public_key(wallet).hex() == state["genesis"]["producer_key"],
                    "这个钱包不是创世区块指定的出块者")
            body = {"height": len(state["blocks"]) + 1, "prev": tip,
                    "transactions": state["pending"]}
            block = {"body": body, "signature": sign(wallet, "block", body)}
            state["blocks"].append(block)
            state["pending"] = []
            _, new_tip, *_ = validate(state, chain_id)
            write_state(path, state)
            emit({"height": body["height"], "block_hash": new_tip,
                  "transactions": len(body["transactions"])})
        elif args.command == "balance":
            check_address(args.address)
            emit({"address": args.address, "confirmed": balances.get(args.address, 0),
                  "including_pending": pending_balances.get(args.address, 0),
                  "next_nonce": nonces.get(args.address, 0),
                  "next_nonce_including_pending": pending_nonces.get(args.address, 0)})
        else:
            emit({"valid": True, "genesis_hash": chain_id, "tip_hash": tip,
                  "genesis_pinned": args.genesis is not None, "height": len(state["blocks"]),
                  "pending": len(state["pending"]), "total_supply": sum(balances.values())})


if __name__ == "__main__":
    try:
        cli()
    except (ChainError, ValueError, OSError, subprocess.TimeoutExpired, RecursionError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(1)
