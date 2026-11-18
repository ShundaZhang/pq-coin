# PQC Chain v0.1

一条只在本机运行的教学链。创世区块创建 **1,000,000 枚无现实价值的测试币**，全部分配给指定地址。协议没有增发、挖矿奖励或交易费。整数金额没有小数位。

交易和区块分别由用户钱包与固定出块者用 **ML-DSA-44** 签名。程序使用 OpenSSL，不自行实现格密码。验证时从创世开始重放，不信任缓存余额；先验交易笔数、字段、个别签名和余额，最终总量必须保持不变。区块用 SHA-256 哈希链接；两类签名、各类哈希都用不同的域标记。

## 环境

- macOS 或 Linux（使用 Unix 文件锁），Python 3.9+。
- **OpenSSL 3.5 或更新版本**，且构建提供 ML-DSA。无需 pip 包，也无需联网。
- 如果 PATH 上的 `openssl` 不支持 ML-DSA，用 `OPENSSL=/绝对路径/openssl` 指定兼容版本。系统自带的 `/usr/bin/openssl` 在有些 macOS 上是 LibreSSL，不是这里需要的 OpenSSL。
- 在解压目录里执行以下命令：

```bash
openssl version
openssl list -signature-algorithms | grep 'ML-DSA-44'
bash demo.sh
python3 test_pqc_chain.py
```

`demo.sh` 在临时目录里跑完两次转账、两次出块、篡改与重放检查，然后删除临时文件。测试套件也使用临时目录和真实 OpenSSL，而非模拟签名。

## 手动操作：保留钱包和账本供检查

从**解压目录**运行。使用新的空目录，不要在已有钱包的路径上重跑初始化。

```bash
script="$PWD/pqc_chain.py"
mkdir local-demo
cd local-demo

alice=$(python3 "$script" wallet-new alice.pem)
bob=$(python3 "$script" wallet-new bob.pem)
python3 "$script" wallet-new producer.pem

python3 "$script" init --treasury "$alice" --producer producer.pem > genesis-receipt.json
anchor=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["genesis_hash"])' < genesis-receipt.json)
printf '创世哈希：%s\n' "$anchor"

python3 "$script" sign --wallet alice.pem --to "$bob" --amount 125 --genesis "$anchor" > tx.json
python3 "$script" submit tx.json
python3 "$script" balance "$bob"            # confirmed=0, including_pending=125
python3 "$script" produce --producer producer.pem
python3 "$script" balance "$bob"            # confirmed=125
python3 "$script" verify --genesis "$anchor" # height=1, total_supply=1000000
```

默认账本是当前目录的 `chain.json`。如需指定其他账本，把 `--chain 路径` 放在子命令之前。金额超额、签名篡改、重放等被拒绝时，程序退出码为 1，不提交变更。余额输出中的 `including_pending` 是待打包交易全部成功确认后的预计值，不是已确认余额。

## 协议与边界

- **创世锚点**：保留独立可信副本的创世哈希；它同时是交易签署的链 ID。未传入锚点的 `verify` 只检查给定文件内部是否一致，不能证明你打开的是原来那条链。
- **交易**：签名覆盖发送地址、收款地址、金额、链 ID 和发送方 nonce。发送地址必须由交易公钥导出。nonce 从 0 递增；对提交顺序累计校验余额，可防止同一份余额被待打包交易重复花费。签名不等于入队或确认。
- **区块**：签名覆盖高度、上个区块哈希和完整的有序交易列表。创世中固定一个出块者；每块和待打包队列上限均为 64 笔。账本写入持有独立的文件锁，并用临时文件原子替换。
- **不要用来保管真资产**：没有多节点共识、分叉选择、P2P、生产级输入限额或密钥轮换。出块者可以审查交易或签出相互冲突的历史；仅凭创世锚点也无法检测账本被截断到此前一个合法高度。PEM 私钥**未加密**，生成文件权限为 `0600`，不要上传。文件回放/签名验签会随链长增加，JSON 十六进制编码也会膨胀体积。
- **备份与恢复**：在无进程写账本时，备份 `chain.json`、各钱包 PEM 和独立保存的创世哈希。恢复到另一个目录后，先用已保存的哈希执行 `verify --genesis`。出块私钥如果丢失，现有链仍能验证，但这版不能更换出块者或继续出块。要清空实验，改用新的目录初始化；新创世哈希不同，旧交易不能跨链使用。

参考：[NIST FIPS 204](https://csrc.nist.gov/pubs/fips/204/final) · [OpenSSL ML-DSA 签名与验签](https://docs.openssl.org/3.5/man1/openssl-pkeyutl/)
