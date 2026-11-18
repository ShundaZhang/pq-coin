"""Run with Python 3.9+ and OpenSSL with ML-DSA-44 support."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import pqc_v2 as chain


SCRIPT = Path(chain.__file__).resolve()
SECRET = "42" * 32
HASHLOCK = hashlib.sha256(bytes.fromhex(SECRET)).hexdigest()


class TestPqcV2(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="pqc-v2-")
        self.addCleanup(tmp.cleanup)
        self.cwd = Path(tmp.name)
        self.file = self.cwd / "chain-v2.json"
        self.alice = self.cli("wallet-new", "alice.pem")
        self.bob = self.cli("wallet-new", "bob.pem")
        self.miner = self.cli("wallet-new", "miner.pem")
        self.genesis = self.j("init", "--treasury", self.alice, "--bits", 8)["genesis_hash"]

    def cli(self, *args, error=None, chain_file=None, pin=False):
        opts = []
        if chain_file is not None:
            opts += ["--chain", str(chain_file)]
        if pin:
            opts += ["--genesis", self.genesis]
        r = subprocess.run([sys.executable, str(SCRIPT), *opts, *map(str, args)],
                           cwd=self.cwd, capture_output=True, text=True, timeout=25)
        if error is not None:
            self.assertEqual(r.returncode, 1, (args, r.stdout, r.stderr))
            self.assertIn(error, r.stderr)
        else:
            self.assertEqual(r.returncode, 0, (args, r.stdout, r.stderr))
        return r.stdout.strip()

    def j(self, *args, **opts):
        return json.loads(self.cli(*args, **opts))

    def signed(self, wallet, *args, fee=1, out="tx.json"):
        tx = self.j("sign", "--wallet", wallet + ".pem", "--fee", fee, *args)
        self.put(out, tx)
        return tx

    def put(self, path, value):
        (self.cwd / path).write_text(json.dumps(value), encoding="utf-8")

    def bytes_digest(self):
        return hashlib.sha256(self.file.read_bytes()).hexdigest()

    def transfer(self, amount=100, fee=3):
        return self.signed("alice", "transfer", "--to", self.bob, "--amount", amount, fee=fee)

    def escrow(self, ttl=3):
        self.signed("alice", "escrow-deploy", "--beneficiary", self.bob,
                    "--amount", 150, "--hashlock", HASHLOCK, "--ttl", ttl, fee=2)
        return self.j("submit", "tx.json")["contract"]

    def mining(self, wallet="miner", empty=False):
        return self.j("mine", "--wallet", wallet + ".pem", * (["--allow-empty"] if empty else []))

    def test_wallet_genesis_transfer_two_miners_fees_and_supply(self):
        self.assertEqual((self.cwd / "alice.pem").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.j("balance", self.alice)["confirmed"], chain.SUPPLY)
        tx = self.transfer()
        self.assertEqual(len(bytes.fromhex(tx["signature"])), 2420)
        self.j("submit", "tx.json")
        self.assertEqual(self.j("balance", self.bob)["including_pending"], 100)
        self.assertEqual(self.j("balance", self.bob)["confirmed"], 0)
        b = self.mining()
        self.assertEqual((b["height"], b["fees_to_miner"], b["minted"]), (1, 3, 0))
        self.assertTrue(chain.work_meets_target(b["block_hash"], 8))
        self.assertEqual(self.j("balance", self.miner)["confirmed"], 3)
        self.assertEqual(self.j("balance", self.alice)["confirmed"], 999897)
        self.assertEqual(self.j("balance", self.bob)["confirmed"], 100)
        self.signed("bob", "transfer", "--to", self.alice, "--amount", 20)
        self.j("submit", "tx.json")
        self.assertEqual(self.mining("bob")["height"], 2)
        self.assertEqual(self.j("balance", self.bob)["confirmed"], 80)
        confirmed = self.j("verify", pin=True)
        self.assertEqual((confirmed["coin_name"], confirmed["symbol"]), ("PQ币", "PQ"))
        self.assertEqual((confirmed["height"], confirmed["pending"], confirmed["total_supply"]),
                         (2, 0, 1000000))

    def test_failed_mining_is_atomic_and_invalid_proof_rejected(self):
        self.transfer()
        self.j("submit", "tx.json")
        state = json.loads(self.file.read_bytes())
        header = {"chain_id": self.genesis, "height": 1, "prev": self.genesis,
                  "tx_commitment": chain.digest("transactions", state["pending"]), "miner": self.miner}
        invalid = next(i for i in range(1000)
                       if not chain.work_meets_target(chain.pow_id({**header, "work_nonce": i}), 8))
        before = self.bytes_digest()
        self.cli("mine", "--wallet", "miner.pem", "--start-nonce", invalid,
                 "--max-attempts", 1, error="未找到工作量证明")
        self.assertEqual(self.bytes_digest(), before)
        self.mining()
        state = json.loads(self.file.read_bytes())
        state["blocks"][0]["header"]["work_nonce"] = invalid
        self.put("tampered.json", state)
        self.cli("verify", chain_file="tampered.json", pin=True, error="PoW 不满足")

    def test_changed_miner_signature_and_transaction_commitment(self):
        self.transfer()
        self.j("submit", "tx.json")
        self.mining()
        state = json.loads(self.file.read_bytes())
        block = state["blocks"][0]
        block["signature"] = ("00" if block["signature"][:2] != "00" else "01") + block["signature"][2:]
        self.put("bad-sig.json", state)
        self.cli("verify", chain_file="bad-sig.json", error="block 签名校验失败")
        block["transactions"][0]["body"]["data"]["amount"] += 1
        self.put("bad-tx.json", state)
        self.cli("verify", chain_file="bad-tx.json", error="区块交易承诺不匹配")

    def test_wallet_signature_tamper_replay_cross_chain_and_overdraft(self):
        tx = self.transfer()
        tampered = json.loads(json.dumps(tx))
        tampered["body"]["fee"] += 1
        self.put("tampered.json", tampered)
        before = self.bytes_digest()
        self.cli("submit", "tampered.json", error="tx 签名校验失败")
        self.assertEqual(self.bytes_digest(), before)
        self.j("init", "--treasury", self.alice, "--bits", 8, chain_file="other.json")
        self.cli("submit", "tx.json", chain_file="other.json", error="chain_id")
        self.j("submit", "tx.json")
        self.cli("submit", "tx.json", error="nonce 不匹配")
        self.mining()
        self.cli("submit", "tx.json", error="nonce 不匹配")
        self.transfer(amount=999897, fee=1)
        self.cli("submit", "tx.json", error="余额不足")

    def test_escrow_claim_rejects_wrong_actor_secret_and_repeat(self):
        identifier = self.escrow()
        self.assertIsNone(self.j("contract", identifier)["confirmed"])
        self.mining()
        status = self.j("contract", identifier)["confirmed"]
        self.assertEqual((status["status"], status["locked"], status["expires_height"]),
                         ("open", 150, 4))
        verified = self.j("verify", pin=True)
        self.assertEqual((verified["locked"], verified["total_supply"]), (150, 1_000_000))

        self.signed("alice", "escrow-claim", identifier, "--secret", SECRET)
        self.cli("submit", "tx.json", error="只有指定受益人")
        self.signed("bob", "escrow-claim", identifier, "--secret", "43" * 32)
        self.cli("submit", "tx.json", error="原像不匹配")
        self.assertEqual(self.j("balance", self.bob)["confirmed"], 0)
        self.signed("bob", "escrow-claim", identifier, "--secret", SECRET, fee=4)
        self.j("submit", "tx.json")
        self.mining()
        # Recipient didn't need funds before claim; fee was paid out of the escrow.
        self.assertEqual(self.j("balance", self.bob)["confirmed"], 146)
        self.assertEqual(self.j("balance", self.miner)["confirmed"], 6)
        self.assertEqual(self.j("contract", identifier)["confirmed"]["status"], "claimed")
        self.signed("bob", "escrow-claim", identifier, "--secret", SECRET)
        self.cli("submit", "tx.json", error="合约已结算")

    def test_escrow_refund_at_expiry_and_empty_block_fees(self):
        identifier = self.escrow(ttl=2)
        self.mining()
        self.signed("alice", "escrow-refund", identifier)
        self.cli("submit", "tx.json", error="尚未到期")
        self.cli("mine", "--wallet", "miner.pem", error="无待打包交易")
        self.assertEqual(self.mining(empty=True)["fees_to_miner"], 0)
        self.signed("bob", "escrow-claim", identifier, "--secret", SECRET)
        self.cli("submit", "tx.json", error="合约已到期")
        self.signed("bob", "escrow-refund", identifier)
        self.cli("submit", "tx.json", error="只有部署者")
        self.signed("alice", "escrow-refund", identifier, fee=3)
        self.j("submit", "tx.json")
        result = self.mining()
        self.assertEqual((result["height"], result["fees_to_miner"]), (3, 3))
        self.assertEqual(self.j("contract", identifier)["confirmed"]["status"], "refunded")
        self.assertEqual(self.j("balance", self.alice)["confirmed"], 999995)
        self.assertEqual(self.j("verify", pin=True)["locked"], 0)

    def test_checkpoint_mismatch_and_restore_copy(self):
        self.transfer()
        self.j("submit", "tx.json")
        b = self.mining()
        point = self.j("checkpoint", "--height", 1, pin=True)
        self.assertEqual(point, {"genesisHash": "0x" + self.genesis, "height": 1,
                                 "blockHash": "0x" + b["block_hash"]})
        self.assertTrue(self.j("checkpoint", "--height", 1, "--expect",
                               point["blockHash"], pin=True)["matches"])
        self.cli("checkpoint", "--height", 1, "--expect", "ff" * 32, pin=True, error="不匹配")
        (self.cwd / "restored").mkdir()
        copy = self.cwd / "restored/chain.json"
        shutil.copy2(self.file, copy)
        before = self.bytes_digest()
        self.assertEqual(self.j("verify", chain_file=copy, pin=True)["tip_hash"], b["block_hash"])
        self.assertEqual(self.j("balance", self.bob, chain_file=copy)["confirmed"], 100)
        self.assertEqual(self.bytes_digest(), before)

    def test_v1_chain_explicitly_not_migrated(self):
        self.put("v1.json", {"genesis": {"version": "pqcchain-v1"}, "blocks": [], "pending": []})
        self.cli("verify", chain_file="v1.json", error="不能直接打开 v0.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
