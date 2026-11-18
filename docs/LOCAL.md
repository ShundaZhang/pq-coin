# PQ币：本地验收和版本边界

## v0.1 / v0.2

| 项目 | v0.1 | v0.2 |
|---|---|---|
| 初始供应 | 创世发 1,000,000 | 同样固定总量，不增发 |
| 钱包、交易 | ML-DSA-44 钱包和地址；签名、验签、待打包池；nonce、余额和链 ID 校验；确认/预计余额 | 保留，交易签名域换为 v2，增加手续费和三种托管交易 |
| 出块 | 固定出块钱包签区块，无手续费和奖励 | 任何兼容钱包可试算 PoW；矿工签区块，收本区块手续费 |
| 验链 | 区块哈希链接、交易重放、创世哈希、原子写入和备份恢复 | 保留，另验 PoW、交易承诺、锁仓和手续费守恒 |
| 合约 | 无链内合约 | 固定哈希锁托管模板，无通用 VM：部署、领取、退款 |
| 网络 | 单机 | 仍为单机；没有 P2P、分叉选择、难度调整、跨链转移。Sepolia 仅部署独立合约 |

## 逐项检查

在根目录执行一次：

```bash
mkdir -p runs
bash demo.sh --keep ./runs/local-check
R=./runs/local-check
GENESIS=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["genesis_hash"])' < "$R/genesis.json")
pq() { python3 pqc_v2.py --chain "$R/chain-v2.json" --genesis "$GENESIS" "$@"; }
ALICE=$(python3 pqc_v2.py address "$R/alice.pem")
BOB=$(python3 pqc_v2.py address "$R/bob.pem")
MINER=$(python3 pqc_v2.py address "$R/miner.pem")
ESCROW=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["contract"])' < "$R/escrow.json")
pq verify
pq balance "$ALICE"
pq balance "$BOB"
pq balance "$MINER"
pq contract "$ESCROW"
pq checkpoint --height 1
```

| 验什么 | 应当看到什么 |
|---|---|
| 创世与签名 | 初始供应 1,000,000；验链输出 `coin_name: PQ币`、`symbol: PQ`。原型对实际 ML-DSA-44 签名验签；篡改会失败。 |
| 普通转账与挖矿 | 第 1 块转入 Bob 100；矿工得 3 PQ 手续费。演示难度为 8 位，因此每个 PoW 区块哈希开头均为 `00`；`minted` 均为 0。 |
| 托管部署与领取 | 第 2 块锁 150，矿工得 2；第 3 块 Bob 领取净额 146，出块钱包 Alice 收 4。结算后 `status: claimed`、`locked: 0`。 |
| 验链、最终余额 | 高度 3，`valid: true`，供应守恒，Alice/Bob/矿工为 999749/246/5。 |
| 错误 PoW、错误签名、重放、跨链、余额不足 | `python3 test_pqc_v2.py` 会独立构造上述反例，验证拒绝且部分场景下账本不被改写。 |
| 过期退款、权限、旧检查点 | 同一套测试构造到期退回、错误原像/调用者、重复领取，以及检查点不匹配等反例。 |

### 手动签一笔转账并挖出第 4 块

接着在同一 shell 执行：

```bash
pq sign --wallet "$R/alice.pem" --fee 2 transfer --to "$BOB" --amount 10 > "$R/tx.json"
pq submit "$R/tx.json"
pq balance "$BOB"                      # confirmed=246, including_pending=256
pq mine --wallet "$R/miner.pem"         # height=4, fees_to_miner=2, minted=0
pq balance "$BOB"                      # confirmed=256
pq verify                              # total_supply=1000000
```

再次 `pq submit "$R/tx.json"` 应返回退出码 1，报告 nonce 不匹配。默认不挖空块；需要推进区块高度测试过期时，明确执行 `pq mine --wallet "$R/miner.pem" --allow-empty`。空块无奖励。

## 备份和恢复

停止写入，再备份 `chain-v2.json`、钱包 PEM 和独立保存的创世哈希。复制到新目录后，用保存的 `--genesis` 跑 `verify`，有已锚定检查点时再用 `checkpoint --height N --expect 已知哈希` 复核。仅核验创世哈希不足以检测截断到一个较老合法高度。

如需退回 v0.1，使用 `legacy/v0.1/` 和 v0.1 原账本，不覆盖旧文件。可复验原版：

```bash
(cd legacy/v0.1 && shasum -a 256 -c SHA256SUMS && python3 test_pqc_chain.py)
```

原始压缩包 SHA-256（留档）：
- v0.1：`29c5b9751075693ef76c5170265369da03fb327a4cced1d3d6b5e222f19353e9`
- v0.2：`2ca2e604fabb58bcd2a26b6029158ba675ea2026d1523fa92e35d02e9957435d`

[v0.1 至 v0.2 协议差异](v0.1-to-v0.2.diff)供阅读，不是账本迁移补丁。
