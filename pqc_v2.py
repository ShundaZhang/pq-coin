#!/usr/bin/env python3
"""Local proof-of-work PQC chain with a built-in programmable escrow template."""
import argparse
import contextlib
from dataclasses import dataclass, field
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


COIN_NAME = "PQ币"
COIN_SYMBOL = "PQ"
SUPPLY = 1_000_000
MAX_TX = 64
MAX_U64 = (1 << 64) - 1
VERSION = "pqcchain-v2"
MIN_BITS = 8
MAX_BITS = 22
MAX_TTL = 4096
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
    return VERSION.encode("ascii") + b"/" + domain.encode("ascii") + b"\x00" + canonical(value)


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
    # Keep v1 address encoding so existing test wallet PEMs can be reused.
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


def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def normalize_hash(value, label):
    if value is None:
        return None
    value = value.removeprefix("0x")
    hex_bytes(value, 32, label)
    return value


def contract_address(chain_id, sender, nonce):
    return "pc1" + digest("contract-id", [chain_id, sender, nonce])


def check_contract(value):
    require(type(value) is str and value.startswith("pc1"), "合约地址格式错误")
    hex_bytes(value[3:], 32, "合约地址")


def credit(balances, receiver, amount):
    balances[receiver] = balances.get(receiver, 0) + amount


def debit(balances, sender, amount):
    require(balances.get(sender, 0) >= amount, "余额不足（含手续费）")
    balances[sender] -= amount


@dataclass(frozen=True)
class Escrow:
    sender: str
    beneficiary: str
    hashlock: str
    expires_height: int
    amount: int
    status: str = "open"

    @property
    def locked(self):
        return self.amount if self.status == "open" else 0


@dataclass
class Ledger:
    balances: dict = field(default_factory=dict)
    nonces: dict = field(default_factory=dict)
    contracts: dict = field(default_factory=dict)

    def copy(self):
        # Escrow records are immutable; copies need not clone their values.
        return Ledger(self.balances.copy(), self.nonces.copy(), self.contracts.copy())

    def total(self):
        return sum(self.balances.values()) + sum(c.locked for c in self.contracts.values())


@dataclass
class Replay:
    genesis_hash: str
    bits: int
    hashes: list
    confirmed: Ledger
    projected: Ledger
    pending_fees: int

    @property
    def tip(self):
        return self.hashes[-1]

    @property
    def height(self):
        return len(self.hashes) - 1


def tx_data(kind, data):
    if kind == "transfer":
        fields(data, "to amount", "转账")
        check_address(data["to"])
        integer(data["amount"], 1, SUPPLY, "金额")
    elif kind == "escrow-deploy":
        fields(data, "beneficiary amount hashlock ttl", "部署")
        check_address(data["beneficiary"])
        integer(data["amount"], 2, SUPPLY, "锁定金额")
        hex_bytes(data["hashlock"], 32, "SHA-256 哈希锁")
        integer(data["ttl"], 2, MAX_TTL, "有效区块数")
    elif kind == "escrow-claim":
        fields(data, "contract secret", "领取")
        check_contract(data["contract"])
        hex_bytes(data["secret"], 32, "原像")
    elif kind == "escrow-refund":
        fields(data, "contract", "退款")
        check_contract(data["contract"])
    else:
        raise ChainError("未知交易类型")


def apply_tx(tx, chain_id, height, ledger):
    fields(tx, "body public_key signature", "交易")
    body = tx["body"]
    fields(body, "chain_id from nonce fee kind data", "交易正文")
    require(body["chain_id"] == chain_id, "交易 chain_id 与本链不匹配")
    sender, nonce, fee, kind, data = (body[k] for k in ("from", "nonce", "fee", "kind", "data"))
    check_address(sender)
    integer(nonce, 0, MAX_U64, "nonce")
    integer(fee, 1, SUPPLY, "手续费")
    tx_data(kind, data)
    public = hex_bytes(tx["public_key"], PUB_BYTES, "公钥")
    signature = hex_bytes(tx["signature"], SIG_BYTES, "签名")
    require(address(public) == sender, "签名公钥与发送者地址不匹配")
    expect = ledger.nonces.get(sender, 0)
    require(nonce == expect, f"nonce 不匹配：预期 {expect}，收到 {nonce}")
    verify_signature(public, signature, "tx", body)

    if kind == "transfer":
        debit(ledger.balances, sender, data["amount"] + fee)
        credit(ledger.balances, data["to"], data["amount"])
    elif kind == "escrow-deploy":
        identifier = contract_address(chain_id, sender, nonce)
        require(identifier not in ledger.contracts, "合约已存在")
        require(height + data["ttl"] <= MAX_U64, "合约过期高度溢出")
        debit(ledger.balances, sender, data["amount"] + fee)
        ledger.contracts[identifier] = Escrow(sender, data["beneficiary"], data["hashlock"],
                                              height + data["ttl"], data["amount"])
    else:
        identifier = data["contract"]
        escrow = ledger.contracts.get(identifier)
        require(escrow is not None, "合约不存在")
        require(escrow.status == "open", "合约已结算")
        require(fee < escrow.amount, "手续费必须小于合约锁定金额")
        if kind == "escrow-claim":
            require(sender == escrow.beneficiary, "只有指定受益人能领取")
            require(height < escrow.expires_height, "合约已到期")
            require(hashlib.sha256(bytes.fromhex(data["secret"])).hexdigest() == escrow.hashlock,
                    "合约原像不匹配")
            status = "claimed"
        else:
            require(sender == escrow.sender, "只有部署者能退款")
            require(height >= escrow.expires_height, "合约尚未到期")
            status = "refunded"
        credit(ledger.balances, sender, escrow.amount - fee)
        ledger.contracts[identifier] = Escrow(escrow.sender, escrow.beneficiary, escrow.hashlock,
                                              escrow.expires_height, escrow.amount, status)
    ledger.nonces[sender] = nonce + 1
    return fee


def pow_id(header):
    return digest("pow", header)


def work_meets_target(block_hash, bits):
    return int(block_hash, 16) < 1 << (256 - bits)


def inspect_genesis(genesis, pinned):
    require(type(genesis) is dict and genesis.get("version") == VERSION,
            "不支持的链版本；v0.2 需新创世，不能直接打开 v0.1 账本")
    fields(genesis, "version network_tag supply treasury difficulty_bits", "创世区块")
    hex_bytes(genesis["network_tag"], 16, "创世网络标识")
    integer(genesis["supply"], SUPPLY, SUPPLY, "总发行量")
    check_address(genesis["treasury"])
    bits = genesis["difficulty_bits"]
    integer(bits, MIN_BITS, MAX_BITS, "难度位数")
    chain_id = digest("genesis", genesis)
    if pinned is not None:
        require(pinned == chain_id, "创世锚点不匹配")
    return chain_id, bits


def replay(state, pinned=None):
    fields(state, "genesis blocks pending", "链文件")
    genesis, blocks, pending = (state[k] for k in ("genesis", "blocks", "pending"))
    chain_id, bits = inspect_genesis(genesis, pinned)
    require(type(blocks) is list and len(blocks) < MAX_U64, "区块列表格式错误")
    require(type(pending) is list and len(pending) <= MAX_TX, "待打包队列格式错误或超长")
    ledger = Ledger({genesis["treasury"]: SUPPLY})
    hashes = [chain_id]
    for height, block in enumerate(blocks, 1):
        try:
            fields(block, "header transactions miner_key signature", "区块")
            header, txs = block["header"], block["transactions"]
            fields(header, "chain_id height prev tx_commitment miner work_nonce", "区块头")
            require(header["chain_id"] == chain_id, "区块 chain_id 与本链不匹配")
            integer(header["height"], height, height, "区块高度")
            require(header["prev"] == hashes[-1], "前块哈希不匹配")
            require(type(txs) is list and len(txs) <= MAX_TX, "区块交易数不合法")
            require(header["tx_commitment"] == digest("transactions", txs), "区块交易承诺不匹配")
            integer(header["work_nonce"], 0, MAX_U64, "工作量 nonce")
            check_address(header["miner"])
            block_hash = pow_id(header)
            require(work_meets_target(block_hash, bits), "PoW 不满足创世难度")
            public = hex_bytes(block["miner_key"], PUB_BYTES, "矿工公钥")
            require(address(public) == header["miner"], "矿工公钥与地址不匹配")
            sig = hex_bytes(block["signature"], SIG_BYTES, "区块签名")
            verify_signature(public, sig, "block", header)
            fees = sum(apply_tx(tx, chain_id, height, ledger) for tx in txs)
            credit(ledger.balances, header["miner"], fees)
            require(ledger.total() == SUPPLY, "余额加合约锁仓不等于固定发行量")
            hashes.append(block_hash)
        except ChainError as exc:
            raise ChainError(f"区块 #{height}: {exc}") from exc

    projected = ledger.copy()
    fees = 0
    for index, tx in enumerate(pending, 1):
        try:
            fees += apply_tx(tx, chain_id, len(hashes), projected)
        except ChainError as exc:
            raise ChainError(f"待打包交易 #{index}: {exc}") from exc
    require(projected.total() + fees == SUPPLY, "预计余额、锁仓和待结算手续费总量错误")
    return Replay(chain_id, bits, hashes, ledger, projected, fees)


def mine(state, rt, wallet, start, count, allow_empty):
    require(state["pending"] or allow_empty, "无待打包交易；若需要空块请显式加 --allow-empty")
    integer(start, 0, MAX_U64, "起始工作量 nonce")
    integer(count, 1, 10_000_000, "尝试上限")
    public = public_key(wallet)
    txs = state["pending"]
    header = {"chain_id": rt.genesis_hash, "height": rt.height + 1, "prev": rt.tip,
              "tx_commitment": digest("transactions", txs), "miner": address(public)}
    for n in range(start, min(MAX_U64 + 1, start + count)):
        header["work_nonce"] = n
        if work_meets_target(pow_id(header), rt.bits):
            break
    else:
        raise ChainError("本次未找到工作量证明；账本未改动，可用 --start-nonce 继续试")
    state["blocks"].append({"header": header, "transactions": txs, "miner_key": public.hex(),
                            "signature": sign(wallet, "block", header)})
    state["pending"] = []
    new_rt = replay(state, rt.genesis_hash)
    return {"height": new_rt.height, "block_hash": new_rt.tip, "miner": header["miner"],
            "attempts": n - start + 1, "work_nonce": n, "difficulty_bits": rt.bits,
            "transactions": len(txs), "fees_to_miner": rt.pending_fees, "minted": 0}


def sign_tx(args, rt):
    wallet = args.wallet.resolve()
    public = public_key(wallet)
    sender = address(public)
    nonce = rt.projected.nonces.get(sender, 0)
    if args.intent == "transfer":
        data = {"to": args.to, "amount": args.amount}
    elif args.intent == "escrow-deploy":
        data = {"beneficiary": args.beneficiary, "amount": args.amount,
                "hashlock": args.hashlock, "ttl": args.ttl}
    elif args.intent == "escrow-claim":
        data = {"contract": args.contract, "secret": args.secret}
    else:
        data = {"contract": args.contract}
    tx_data(args.intent, data)
    integer(nonce, 0, MAX_U64, "nonce")
    integer(args.fee, 1, SUPPLY, "手续费")
    body = {"chain_id": rt.genesis_hash, "from": sender, "nonce": nonce, "fee": args.fee,
            "kind": args.intent, "data": data}
    return {"body": body, "public_key": public.hex(), "signature": sign(wallet, "tx", body)}


def cli():
    parser = argparse.ArgumentParser(description=f"{COIN_NAME}（{COIN_SYMBOL}）v0.2 本地教学链")
    parser.add_argument("--chain", type=Path, default=Path("chain-v2.json"))
    parser.add_argument("--genesis", help="独立保存的创世哈希；可带 0x")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("wallet-new").add_argument("wallet", type=Path)
    sub.add_parser("address").add_argument("wallet", type=Path)
    init = sub.add_parser("init")
    init.add_argument("--treasury", required=True)
    init.add_argument("--bits", type=int, default=12, help="创世 PoW 难度：8 到 22 位")
    tr = sub.add_parser("sign", help="只签名；用 submit 入队")
    tr.add_argument("--wallet", required=True, type=Path)
    tr.add_argument("--fee", default=1, type=int)
    intent = tr.add_subparsers(dest="intent", required=True)
    xfer = intent.add_parser("transfer")
    xfer.add_argument("--to", required=True)
    xfer.add_argument("--amount", type=int, required=True)
    dep = intent.add_parser("escrow-deploy")
    dep.add_argument("--beneficiary", required=True)
    dep.add_argument("--amount", required=True, type=int)
    dep.add_argument("--hashlock", required=True)
    dep.add_argument("--ttl", default=4, type=int)
    claim = intent.add_parser("escrow-claim")
    claim.add_argument("contract")
    claim.add_argument("--secret", required=True)
    intent.add_parser("escrow-refund").add_argument("contract")
    sub.add_parser("submit").add_argument("tx", type=Path)
    mining = sub.add_parser("mine")
    mining.add_argument("--wallet", required=True, type=Path)
    mining.add_argument("--start-nonce", type=int, default=0)
    mining.add_argument("--max-attempts", type=int, default=1_000_000)
    mining.add_argument("--allow-empty", action="store_true")
    sub.add_parser("balance").add_argument("address")
    sub.add_parser("contract").add_argument("address")
    sub.add_parser("verify")
    checkpoint = sub.add_parser("checkpoint", help="验链并导出 Sepolia 检查点")
    checkpoint.add_argument("--height", type=int)
    checkpoint.add_argument("--expect", help="可信合约读到的哈希；需同时指定高度和创世")
    args = parser.parse_args()

    if args.command == "wallet-new":
        print(new_wallet(args.wallet.resolve()))
        return
    if args.command == "address":
        print(address(public_key(args.wallet.resolve())))
        return

    path = args.chain.resolve()
    pinned = normalize_hash(args.genesis, "创世锚点")
    with chain_lock(path):
        if args.command == "init":
            require(pinned is None, "初始化新链时不指定旧创世哈希")
            require(not path.exists(), f"链文件已存在: {path}")
            genesis = {"version": VERSION, "network_tag": secrets.token_hex(16), "supply": SUPPLY,
                       "treasury": args.treasury, "difficulty_bits": args.bits}
            state = {"genesis": genesis, "blocks": [], "pending": []}
            rt = replay(state)
            write_state(path, state)
            emit({"coin_name": COIN_NAME, "symbol": COIN_SYMBOL,
                  "genesis_hash": rt.genesis_hash, "total_supply": SUPPLY,
                  "treasury": args.treasury, "difficulty_bits": rt.bits})
            return

        state = read_json(path)
        rt = replay(state, pinned)
        if args.command == "sign":
            emit(sign_tx(args, rt))
        elif args.command == "submit":
            require(len(state["pending"]) < MAX_TX, "待打包队列已满")
            tx = read_json(args.tx)
            fee = apply_tx(tx, rt.genesis_hash, rt.height + 1, rt.projected)
            require(rt.projected.total() + rt.pending_fees + fee == SUPPLY, "手续费结算不守恒")
            state["pending"].append(tx)
            write_state(path, state)
            receipt = {"txid": digest("tx-id", tx), "pending": len(state["pending"])}
            if tx["body"]["kind"] == "escrow-deploy":
                receipt["contract"] = contract_address(rt.genesis_hash, tx["body"]["from"],
                                                       tx["body"]["nonce"])
            emit(receipt)
        elif args.command == "mine":
            receipt = mine(state, rt, args.wallet.resolve(), args.start_nonce,
                           args.max_attempts, args.allow_empty)
            write_state(path, state)
            emit(receipt)
        elif args.command == "balance":
            check_address(args.address)
            a = args.address
            emit({"address": a, "confirmed": rt.confirmed.balances.get(a, 0),
                  "including_pending": rt.projected.balances.get(a, 0),
                  "next_nonce": rt.confirmed.nonces.get(a, 0),
                  "next_nonce_including_pending": rt.projected.nonces.get(a, 0)})
        elif args.command == "contract":
            check_contract(args.address)
            a = args.address
            def describe(ledger):
                c = ledger.contracts.get(a)
                return {**vars(c), "locked": c.locked} if c is not None else None
            confirmed, pending = describe(rt.confirmed), describe(rt.projected)
            require(confirmed is not None or pending is not None, "合约不存在")
            emit({"address": a, "type": "hashlock-escrow", "confirmed": confirmed,
                  "including_pending": pending})
        elif args.command == "checkpoint":
            expected = normalize_hash(args.expect, "链上区块哈希")
            require(expected is None or (pinned is not None and args.height is not None),
                    "--expect 必须与 --genesis 和 --height 一起使用")
            h = rt.height if args.height is None else args.height
            integer(h, 1, rt.height, "检查点高度（需先挖出区块）")
            block_hash = rt.hashes[h]
            require(expected is None or expected == block_hash, "本地区块哈希与链上记录不匹配")
            result = {"genesisHash": "0x" + rt.genesis_hash, "height": h,
                      "blockHash": "0x" + block_hash}
            if expected is not None:
                result["matches"] = True
            emit(result)
        else:
            emit({"valid": True, "coin_name": COIN_NAME, "symbol": COIN_SYMBOL,
                  "genesis_hash": rt.genesis_hash, "genesis_pinned": pinned is not None,
                  "tip_hash": rt.tip, "height": rt.height, "difficulty_bits": rt.bits,
                  "pending": len(state["pending"]), "pending_fees": rt.pending_fees,
                  "circulating": sum(rt.confirmed.balances.values()),
                  "locked": sum(c.locked for c in rt.confirmed.contracts.values()),
                  "contracts": len(rt.confirmed.contracts), "total_supply": rt.confirmed.total()})


if __name__ == "__main__":
    try:
        cli()
    except (ChainError, ValueError, OSError, subprocess.TimeoutExpired, RecursionError) as exc:
        sys.exit(f"错误: {exc}")
