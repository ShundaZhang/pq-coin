import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { ethers } from 'ethers';

const ABI = [
  'function genesisHash() view returns (bytes32)',
  'function checkpoints(uint256) view returns (bytes32)',
  'event CheckpointAnchored(bytes32 indexed genesisHash, uint256 indexed height, bytes32 blockHash)',
];

export async function checkAnchor(provider, address, point, txHash, chainId = 11155111n) {
  address = ethers.getAddress(address);
  if (!point || !ethers.isHexString(point.genesisHash, 32) || !ethers.isHexString(point.blockHash, 32)
      || !Number.isSafeInteger(point.height) || point.height < 1
      || point.genesisHash.toLowerCase() === ethers.ZeroHash || point.blockHash.toLowerCase() === ethers.ZeroHash) {
    throw new Error('无效检查点文件');
  }
  if (!ethers.isHexString(txHash, 32)) throw new Error('无效的交易哈希');
  const network = await provider.getNetwork();
  if (network.chainId !== chainId) throw new Error(`网络不匹配：预期 ${chainId}，实际 ${network.chainId}`);
  const contract = new ethers.Contract(address, ABI, provider);
  if (await provider.getCode(address) === '0x') throw new Error('该地址没有合约代码');
  if ((await contract.genesisHash()).toLowerCase() !== point.genesisHash.toLowerCase()) {
    throw new Error('创世哈希不匹配');
  }
  if ((await contract.checkpoints(point.height)).toLowerCase() !== point.blockHash.toLowerCase()) {
    throw new Error('检查点哈希不匹配或该高度未锚定');
  }
  const receipt = await provider.getTransactionReceipt(txHash);
  if (receipt?.status !== 1 || receipt.to?.toLowerCase() !== address.toLowerCase()) {
    throw new Error('找不到成功提交给指定合约的交易');
  }
  const matches = receipt.logs.some((log) => {
    if (log.address.toLowerCase() !== address.toLowerCase()) return false;
    try {
      const event = contract.interface.parseLog(log);
      return event?.name === 'CheckpointAnchored' &&
        event.args[0].toLowerCase() === point.genesisHash.toLowerCase() &&
        event.args[1] === BigInt(point.height) &&
        event.args[2].toLowerCase() === point.blockHash.toLowerCase();
    } catch {
      return false;
    }
  });
  if (!matches) throw new Error('该交易未发布指定检查点');
  let finalized = null;
  try {
    const block = await provider.getBlock('finalized');
    if (block) finalized = receipt.blockNumber <= block.number;
  } catch {
    // Some providers do not implement the finalized tag.
  }
  return { chainId: network.chainId.toString(), contract: address, checkpoint: point,
           transactionHash: receipt.hash, includedAtBlock: receipt.blockNumber, finalized, matches };
}

async function cli() {
  const [address, file, tx] = process.argv.slice(2);
  if (!address || !file || !tx || !process.env.SEPOLIA_RPC_URL) {
    throw new Error('设置 SEPOLIA_RPC_URL，然后运行 node verify_sepolia.mjs 合约地址 检查点文件 锚定交易哈希');
  }
  const provider = new ethers.JsonRpcProvider(process.env.SEPOLIA_RPC_URL);
  try {
    const point = JSON.parse(readFileSync(file, 'utf8'));
    const result = await checkAnchor(provider, address, point, tx);
    console.log(JSON.stringify(result, null, 2));
  } finally {
    provider.destroy();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await cli().catch((error) => {
    // Do not echo a secret-bearing RPC URL or raw provider request.
    const url = process.env.SEPOLIA_RPC_URL;
    const reason = error instanceof Error ? (error.shortMessage ?? error.message) : '未知错误';
    console.error('错误:', url ? String(reason).replaceAll(url, '[RPC URL 已隐藏]') : reason);
    process.exitCode = 1;
  });
}
