"""Real OpenSSL-backed CLI tests. Wallets live in disposable temporary directories."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("pqc_chain.py").resolve()
OPENSSL = os.environ.get("OPENSSL", "openssl")
SUPPLY = 1_000_000


class CLITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="pqc-chain-test-")
        self.addCleanup(tmp.cleanup)
        self.cwd = Path(tmp.name)
        self.alice = self.run_cli("wallet-new", "alice.pem")
        self.bob = self.run_cli("wallet-new", "bob.pem")
        self.producer = self.run_cli("wallet-new", "producer.pem")
        self.anchor = self.as_json("init", "--treasury", self.alice,
                                   "--producer", "producer.pem")["genesis_hash"]

    def run_cli(self, *args, chain="chain.json", fails=None):
        result = subprocess.run([sys.executable, str(SCRIPT), "--chain", chain, *args],
                                cwd=self.cwd, capture_output=True, text=True, timeout=20)
        if fails is None:
            self.assertEqual(result.returncode, 0, (args, result.stderr))
            return result.stdout.strip()
        self.assertEqual(result.returncode, 1, (args, result.stdout, result.stderr))
        self.assertIn(fails, result.stderr)
        return result.stderr

    def as_json(self, *args, **kwargs):
        return json.loads(self.run_cli(*args, **kwargs))

    def signed(self, amount=125, wallet="alice.pem", to=None, path="tx.json"):
        tx = self.as_json("sign", "--wallet", wallet, "--to", to or self.bob,
                          "--amount", str(amount), "--genesis", self.anchor)
        self.save(path, tx)
        return tx

    def save(self, path, obj):
        (self.cwd / path).write_text(json.dumps(obj), encoding="utf-8")

    def filehash(self):
        return hashlib.sha256((self.cwd / "chain.json").read_bytes()).hexdigest()

    def confirm_first(self):
        self.signed()
        self.run_cli("submit", "tx.json")
        return self.as_json("produce", "--producer", "producer.pem")

    def test_two_blocks_roundtrip_after_process_restart(self):
        self.assertEqual(self.as_json("balance", self.alice)["confirmed"], SUPPLY)
        self.assertEqual(self.as_json("balance", self.bob)["confirmed"], 0)
        tx = self.signed()
        self.assertEqual(len(bytes.fromhex(tx["public_key"])), 1312)
        self.assertEqual(len(bytes.fromhex(tx["signature"])), 2420)
        self.run_cli("submit", "tx.json")
        bob = self.as_json("balance", self.bob)
        self.assertEqual((bob["confirmed"], bob["including_pending"]), (0, 125))
        self.assertEqual(self.as_json("produce", "--producer", "producer.pem")["height"], 1)
        self.signed(amount=25, wallet="bob.pem", to=self.alice)
        self.run_cli("submit", "tx.json")
        block2 = self.as_json("produce", "--producer", "producer.pem")
        verified = self.as_json("verify", "--genesis", self.anchor)
        self.assertEqual((verified["height"], verified["pending"], verified["total_supply"]),
                         (2, 0, SUPPLY))
        self.assertEqual(verified["tip_hash"], block2["block_hash"])
        self.assertTrue(verified["genesis_pinned"])
        self.assertEqual(self.as_json("balance", self.bob)["confirmed"], 100)
        self.assertEqual(self.as_json("balance", self.alice)["confirmed"], SUPPLY - 100)
        self.assertEqual(self.as_json("balance", self.bob)["next_nonce"], 1)
        self.assertEqual(self.as_json("balance", self.alice)["next_nonce"], 1)

    def test_amount_tamper_rejected_without_writing(self):
        tx = self.signed()
        tx["body"]["amount"] += 1
        self.save("tx.json", tx)
        before = self.filehash()
        self.run_cli("submit", "tx.json", fails="tx 签名校验失败")
        self.assertEqual(self.filehash(), before)

    def test_replay_pending_and_confirmed(self):
        self.signed()
        self.run_cli("submit", "tx.json")
        before = self.filehash()
        self.run_cli("submit", "tx.json", fails="nonce 不匹配")
        self.assertEqual(self.filehash(), before)
        self.run_cli("produce", "--producer", "producer.pem")
        before = self.filehash()
        self.run_cli("submit", "tx.json", fails="nonce 不匹配")
        self.assertEqual(self.filehash(), before)

    def test_overspend_with_valid_signature_and_pending_reservation(self):
        self.signed(amount=SUPPLY - 1)
        self.run_cli("submit", "tx.json")
        self.signed(amount=2)
        before = self.filehash()
        self.run_cli("submit", "tx.json", fails="余额不足")
        self.assertEqual(self.filehash(), before)

    def test_cross_chain_replay_wrong_anchor(self):
        other = self.as_json("init", "--treasury", self.alice, "--producer", "producer.pem",
                             chain="other.json")
        self.assertNotEqual(other["genesis_hash"], self.anchor)
        self.signed()
        self.run_cli("submit", "tx.json", chain="other.json", fails="chain_id")
        self.run_cli("verify", "--genesis", self.anchor, chain="other.json",
                     fails="创世锚点不匹配")

    def test_wrong_producer_and_invalid_block_signature(self):
        self.signed()
        self.run_cli("submit", "tx.json")
        before = self.filehash()
        self.run_cli("produce", "--producer", "bob.pem", fails="不是创世区块指定的出块者")
        self.assertEqual(self.filehash(), before)
        self.run_cli("produce", "--producer", "producer.pem")
        state = json.loads((self.cwd / "chain.json").read_text(encoding="utf-8"))
        state["blocks"][0]["body"]["transactions"][0]["body"]["amount"] += 1
        self.save("tampered.json", state)
        self.run_cli("verify", "--genesis", self.anchor, chain="tampered.json",
                     fails="block 签名校验失败")

    def test_wrong_prev_hash(self):
        self.confirm_first()
        state = json.loads((self.cwd / "chain.json").read_text(encoding="utf-8"))
        state["blocks"][0]["body"]["prev"] = "00" * 32
        self.save("tampered.json", state)
        self.run_cli("verify", "--genesis", self.anchor, chain="tampered.json",
                     fails="前块哈希不匹配")

    def test_backup_restore_to_separate_directory(self):
        tip = self.confirm_first()["block_hash"]
        archive = self.cwd / "backup"
        restored = self.cwd / "restored"
        archive.mkdir()
        restored.mkdir()
        for name in ("chain.json", "producer.pem", "alice.pem", "bob.pem"):
            shutil.copy2(self.cwd / name, archive / name)
            shutil.copy2(archive / name, restored / name)
        (archive / "genesis.sha256").write_text(self.anchor + "\n", encoding="ascii")
        anchor = (archive / "genesis.sha256").read_text(encoding="ascii").strip()
        restored_chain = "restored/chain.json"
        before = self.filehash()
        result = self.as_json("verify", "--genesis", anchor, chain=restored_chain)
        self.assertEqual((result["height"], result["tip_hash"]), (1, tip))
        self.assertEqual(self.as_json("balance", self.bob, chain=restored_chain)["confirmed"], 125)
        tx = self.as_json("sign", "--wallet", "restored/alice.pem", "--to", self.bob,
                          "--amount", "5", "--genesis", anchor, chain=restored_chain)
        self.save("new-transfer.json", tx)
        self.run_cli("submit", "new-transfer.json", chain=restored_chain)
        self.run_cli("produce", "--producer", "restored/producer.pem", chain=restored_chain)
        self.assertEqual(self.as_json("balance", self.bob, chain=restored_chain)["confirmed"], 130)
        self.assertEqual(self.filehash(), before)

    def test_forged_sender_rejected(self):
        tx = self.signed(wallet="bob.pem", to=self.producer)
        tx["body"]["from"] = self.alice
        self.save("tx.json", tx)
        self.run_cli("submit", "tx.json", fails="公钥与发送者地址不匹配")

    def test_non_pqc_wallet_rejected(self):
        subprocess.run([OPENSSL, "genpkey", "-algorithm", "ED25519", "-out", "ed.pem"],
                       cwd=self.cwd, check=True, capture_output=True, timeout=15)
        self.run_cli("address", "ed.pem", fails="钱包必须使用 ML-DSA-44")

    def test_concurrent_submit_accepts_only_once(self):
        self.signed()
        cmd = [sys.executable, str(SCRIPT), "submit", "tx.json"]
        procs = [subprocess.Popen(cmd, cwd=self.cwd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = [(p.communicate(timeout=20), p.returncode) for p in procs]
        self.assertEqual(sorted(code for _, code in results), [0, 1])
        error = next(stderr for (_, stderr), code in results if code == 1)
        self.assertIn("nonce 不匹配", error)
        self.assertEqual(self.as_json("verify", "--genesis", self.anchor)["pending"], 1)

    def test_no_accidental_wallet_or_chain_overwrite(self):
        wallet = self.cwd / "alice.pem"
        before_wallet = hashlib.sha256(wallet.read_bytes()).hexdigest()
        self.assertEqual(wallet.stat().st_mode & 0o777, 0o600)
        self.run_cli("wallet-new", "alice.pem", fails="钱包文件已存在")
        self.assertEqual(hashlib.sha256(wallet.read_bytes()).hexdigest(), before_wallet)
        before_chain = self.filehash()
        self.run_cli("init", "--treasury", self.bob, "--producer", "producer.pem",
                     fails="链文件已存在")
        self.assertEqual(self.filehash(), before_chain)

    def test_rejects_noninteger_and_duplicate_json_key(self):
        tx = self.signed()
        tx["body"]["amount"] = True
        self.save("tx.json", tx)
        self.run_cli("submit", "tx.json", fails="金额必须")
        (self.cwd / "tx.json").write_text('{"body":{},"body":{}}', encoding="utf-8")
        self.run_cli("submit", "tx.json", fails="JSON 含重复字段")


if __name__ == "__main__":
    unittest.main(verbosity=2)
