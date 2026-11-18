# PQ币：Sepolia 部署与逐项验收

**你要部署的是两个独立合约：** `PqcCheckpoint` 为本地 PQ币账本存公开检查点；`TimedHashlockEscrow` 在 Sepolia 上独立托管测试 ETH。不发行 ERC-20 PQ，也不把本地链、余额或私钥上传。PoW、ML-DSA 签名和本地 PQ 余额在本地验证，Sepolia 不复核它们。

## 0. 准备

1. 先在仓库根目录跑 `bash demo.sh` 和 `bash check_evm.sh`（Node 依赖见 [README](../README.md)），再部署公开测试网。
2. 准备两个你控制的普通浏览器测试账户 **A（部署者）** 和 **B（收款人）**。连接 **Ethereum Sepolia，链 ID 11155111**；不要选 Mainnet 或 Base Sepolia。A、B 都要有测试 ETH：部署、锚定以及领取/退款调用都需要 gas。[Sepolia 水龙头列表](https://ethereum.org/developers/docs/networks/#sepolia)
3. 打开 [Remix](https://remix.ethereum.org/)，在工作区创建两个 Solidity 文件并粘贴仓库 [PqcCheckpoint.sol](../evm/PqcCheckpoint.sol) 和 [TimedHashlockEscrow.sol](../evm/TimedHashlockEscrow.sol) 源码。编译器选 **0.8.30**；若需和本地测试一致，EVM target 选 **Cancun**，启用优化器，runs 为 **200**。
4. **Deploy & Run → Browser Extension** 连接钱包，之后每笔交易都在弹窗核对网络、账户、合约和附带金额。不要把助记词、私钥或 PQC PEM 粘贴到 Remix 或发给别人。若只选 Remix VM，交易不会出现在 Sepolia。[Remix 官方文档](https://remix-ide.readthedocs.io/en/latest/run.html)

## 1. 生成并锚定两个 PQ 区块

从仓库根目录运行。目录名前面带 `runs/` 是为了避免把测试私钥误提交：

```bash
mkdir -p runs
bash demo.sh --keep ./runs/sepolia-demo
cat ./runs/sepolia-demo/checkpoint1.json ./runs/sepolia-demo/checkpoint2.json
```

两个 JSON 由同一条链生成，`genesisHash` 相同，`height` 为 1 和 2。传的 `blockHash` 已带 `0x`；**它不是整份 `chain-v2.json` 的文件哈希，也不是 Sepolia 交易哈希。**

| 步骤 | Remix 操作 | 验收 |
|---|---|---|
| 部署 | A 选择 `PqcCheckpoint`，`genesis` 填 `checkpoint1.json` 的 `genesisHash`，**Value=0** | 部署回执成功；`publisher()` 返回 A、`genesisHash()` 一致、`latestHeight()` 为 0 |
| 锚定第 1 块 | A 调 `anchor(height, blockHash)`，填 `checkpoint1.json` 的值，**Value=0** | `checkpoints(1)` 等于第 1 块哈希，回执有 `CheckpointAnchored` |
| 锚定第 2 块 | A 按相同方法提交 `checkpoint2.json` | `latestHeight()` 为 2；`checkpoints(2)` 正确；`checkpoints(1)` 仍不变 |
| 不可覆盖 / 权限 | 重提高度 1，或模拟让 B 提交高度 3 | 应分别失败为 `HeightNotIncreasing`、`NotPublisher`；本地 EVM 已自动覆盖这两项。若钱包预估提示失败，不要强行广播。 |

保存合约地址和两笔 **anchor 的交易哈希**，不是只保存部署交易哈希。用 [Sepolia Etherscan](https://sepolia.etherscan.io/) 核对状态、目标合约、日志和链上区块号。若希望记录稳定，等待收录区块最终确定。

可选：用只读 RPC 核对第 2 块（无需签名和 gas）。先执行 `(cd evm && npm ci --ignore-scripts)`，设置 `SEPOLIA_RPC_URL` 为可信的 Sepolia RPC 地址；如带 API key，只在自己的终端使用，不提交到 Git。

```bash
node evm/verify_sepolia.mjs "$CHECKPOINT_CONTRACT" \
  ./runs/sepolia-demo/checkpoint2.json "$ANCHOR_2_TX"
```

将 `CHECKPOINT_CONTRACT`、`ANCHOR_2_TX` 设为实际合约地址、第二笔锚定交易哈希。应返回 `chainId: "11155111"`、`matches: true` 和交易收录区块；`finalized: null` 表示所用 RPC 没返回最终确定高度。验链使用可信合约地址，不应只凭别人给的地址信任承诺。

要把合约记录和**当前本地账本**复核，可以运行（把变量设为合约的可信读值）：

```bash
python3 pqc_v2.py --chain ./runs/sepolia-demo/chain-v2.json \
  --genesis "$ONCHAIN_GENESIS" checkpoint --height 2 --expect "$ONCHAIN_HASH_2"
```

## 2. 测试独立的测试 ETH 托管

生成独立的一次性秘密和对应哈希。不要用真实密码，不要复用本地 PQ 托管秘密；领取交易会公开秘密。

```bash
python3 -c 'import hashlib,secrets; x=secrets.token_bytes(32); print("secret=0x"+x.hex()); print("lockHash=0x"+hashlib.sha256(x).hexdigest())'
```

### 实例一：领取

1. 在 Remix 选 **TimedHashlockEscrow** 和 A。构造参数：`receiver` 填 B 的地址，`lockHash` 填上一步**哈希**，`durationBlocks` 填 **200**（给切换账户留余量）。
2. 部署前将 **Value 设为 100000000000000，单位 wei**（即 0.0001 测试 ETH）；还需另有余额支付部署 gas。钱包弹窗确认后，查看合约 `amount()`、`beneficiary()`、`expiresAtBlock()`，初始 `status()` 为 **0（Open）**。保存这一实例地址。
3. 将 Value 恢复为 **0**；切到 B，在到期前调用 `claim(bytes32 secret)`。这次填写**原像**，不是哈希。成功后 `status()` 为 **1（Claimed）**，回执有 `Settled` 事件，接收者是 B，金额为部署时附带的值。B 净余额增加量还要扣它支付的 gas。
4. 错误账号、错误原像、重复领取应失败。本地 EVM 已自动验证；可在钱包估算提示失败时停止，不必广播注定失败的交易。

### 实例二：到期退款

1. 用新随机秘密重新计算哈希，A 再部署一个托管实例，仍给 B，使用小额测试 ETH。可以把 `durationBlocks` 设为 **5**，但两件事要分开：若要手测「过早退款」，选更长时间，避免切换窗口期间已过期。
2. **不领取**。等 Sepolia 最新区块高度达到该实例的 `expiresAtBlock()`。A 以 **Value=0** 调用 `refund()`。
3. 验收：`status()` 为 **2（Refunded）**，回执 `Settled` 接收者为 A，金额为入金。超期领取、非 A 退款、重复结算应失败。

## 3. 留存结果

至少记下网络（11155111）、三个合约地址（锚定、领取实例、退款实例）、部署交易、两笔锚定、领取、退款的交易哈希，以及对应本地区块高度与哈希。不要提交私钥或 RPC 凭据。若某次交易上链后失败，回执不应被当成成功证明。

本仓库交付时已在**本地 EVM**复现两种合约的成功及失败路径；**没有替你在 Sepolia 广播**。上面的公开回执需要你按步骤签名后产生。
