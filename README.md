# PQ币（PQ）

用 **ML-DSA-44** 签名的教学型区块链。**PQ** 是本地链的原生测试币，创世发行 **1,000,000 PQ**，不增发。链内转账、合约调用和区块签名使用 OpenSSL 的 ML-DSA-44；挖矿使用 SHA-256 工作量证明。名字和简称是展示信息，不改变已发布的 v0.2 创世或签名格式。

> 这是实验项目，不是公开 P2P 主网，不适合保管真实资产。Sepolia 上部署的是独立的 Solidity **区块检查点**和**测试 ETH 托管**合约，不是在以太坊上铸造 PQ，也不会在以太坊上执行本地 PoW 或验 ML-DSA。

## 本地快速开始

需要 macOS/Linux、Python 3.9+ 和启用 ML-DSA 的 OpenSSL 3.5+。本地链不用 pip。

```bash
git clone https://github.com/ShundaZhang/pq-coin.git
cd pq-coin
git checkout v0.2.0

openssl list -signature-algorithms | grep ML-DSA-44
bash demo.sh
python3 test_pqc_v2.py
```

如果 PATH 上的 OpenSSL 不支持 ML-DSA，可以先设置 `OPENSSL` 指向另一个二进制（例如 Homebrew：`export OPENSSL="$(brew --prefix openssl@3)/bin/openssl"`），再用 `"$OPENSSL" list -signature-algorithms` 核对。

演示跑完三块：普通转账、锁定 150 PQ、领取并扣除手续费。示例最终余额应为 **Alice 999,749、Bob 246、矿工 5 PQ**，合约锁仓为 0。哈希和尝试次数每次不同。要保留账本及锚定参数：

```bash
mkdir -p runs
bash demo.sh --keep ./runs/sepolia-demo
```

目标目录必须不存在。保留下来的 PEM 是未加密的临时测试私钥，**不要上传**。

## 验证本地 EVM

装好 Node.js 24，安装锁定依赖，然后回到仓库根目录执行：

```bash
(cd evm && npm ci --ignore-scripts)
bash check_evm.sh
```

脚本启动 Hardhat 进程内 EVM（链 ID 31337），编译并部署两个 Solidity 合约、锚定两个真实本地区块、验证测试 ETH 领取和超时退款。成功标记：`ALL LOCAL EVM CHECKS PASSED`。不会连接 Sepolia，不需要钱包或真实 ETH。

## 部署到 Sepolia 并逐项验收

看 **[Sepolia 部署与验收手册](docs/SEPOLIA.md)**。它包含从测试 ETH、Remix 编译、部署、写入检查点、链上读值、领取和退款，到保存链上证据的完整步骤。**尚未广播到 Sepolia**；公开部署由你的测试钱包在 Sepolia 签名完成。

## 仓库结构

| 路径 | 内容 |
|---|---|
| `pqc_v2.py`、`test_pqc_v2.py` | PQ币 v0.2 本地链和真实 OpenSSL 签名测试 |
| `demo.sh`、`check_evm.sh` | 本地链演示和 EVM 端到端测试 |
| `evm/` | 锚定及托管合约、本地 EVM 测试、Sepolia 只读验证器 |
| `docs/SEPOLIA.md` | 公开测试网部署和验收清单 |
| `docs/LOCAL.md` | 基础功能、手动验证、备份和版本边界 |
| `legacy/v0.1/` | 未修改的第一版源码与测试，含原始校验清单 |

**v0.1 与 v0.2 不是同一条链。** 钱包 PEM 和地址可复用，但 v0.2 要新创世，旧余额和区块不会自动迁移。旧锚定合约固定旧创世哈希，不能用来核验新链。
