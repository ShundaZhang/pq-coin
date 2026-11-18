import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { ethers } from 'ethers';
import { network } from 'hardhat';
import solc from 'solc';
import { checkAnchor } from './verify_sepolia.mjs';

const inputs = process.argv.slice(2);
assert.equal(inputs.length, 2, 'Provide two checkpoint JSON files as arguments');
const [first, next] = inputs.map(file => JSON.parse(readFileSync(file)));
assert.equal(first.genesisHash, next.genesisHash);
assert.ok(next.height > first.height && first.height > 0);
const sources = Object.fromEntries(
  ['PqcCheckpoint.sol', 'TimedHashlockEscrow.sol'].map(file => [file, {content: readFileSync(file, 'utf8')}])
);
const output = JSON.parse(solc.compile(JSON.stringify({
  language: 'Solidity',
  sources,
  settings: {optimizer: {enabled: true, runs: 200}, evmVersion: 'cancun',
             outputSelection: {'*': {'*': ['abi', 'evm.bytecode.object']}}}
})));
for (const issue of output.errors ?? []) {
  if (issue.severity === 'error') throw new Error(issue.formattedMessage);
}

const connection = await network.connect('localEvm');
const provider = new ethers.BrowserProvider(connection.provider);
try {
  const deployer = await provider.getSigner(0);
  const receiver = await provider.getSigner(1);
  const other = await provider.getSigner(2);
  const receiverAddress = await receiver.getAddress();
  const factory = (name) => {
    const compiled = output.contracts[name + '.sol'][name];
    return new ethers.ContractFactory(compiled.abi, compiled.evm.bytecode.object, deployer);
  };
  const receipt = async (submission) => {
    const result = await (await submission).wait();
    assert.equal(result.status, 1);
    return result;
  };
  const rawBalance = addr => connection.provider.request({method: 'eth_getBalance', params: [addr, 'latest']})
    .then(BigInt);
  console.log('Solidity:', solc.version(), '| Local chain ID:', String((await provider.getNetwork()).chainId));

  const registry = await factory('PqcCheckpoint').deploy(first.genesisHash);
  await receipt(registry.deploymentTransaction());
  assert.equal(await registry.genesisHash(), first.genesisHash);
  let firstTx;
  let lastTx;
  for (const p of [first, next]) {
    const anchored = await receipt(registry.anchor(p.height, p.blockHash));
    firstTx ??= anchored.hash;
    lastTx = anchored.hash;
    const log = registry.interface.parseLog(anchored.logs[0]);
    assert.equal(log.name, 'CheckpointAnchored');
    assert.deepEqual([...log.args], [p.genesisHash, BigInt(p.height), p.blockHash]);
    assert.equal(await registry.checkpoints(p.height), p.blockHash);
    console.log('Checkpoint recorded:', p.height, p.blockHash, '| gas:', String(anchored.gasUsed));
  }
  await assert.rejects(registry.anchor.staticCall(first.height, next.blockHash), /HeightNotIncreasing/);
  await assert.rejects(registry.connect(other).anchor.staticCall(next.height + 1, next.blockHash), /NotPublisher/);
  assert.equal(await registry.checkpoints(first.height), first.blockHash);
  console.log('Checkpoint rejects overwrite and non-publisher');
  const registryAddress = await registry.getAddress();
  const localChain = (await provider.getNetwork()).chainId;
  assert.equal((await checkAnchor(provider, registryAddress, next, lastTx, localChain)).matches, true);
  await assert.rejects(checkAnchor(provider, registryAddress, next, firstTx, localChain), /未发布指定检查点/);
  await assert.rejects(checkAnchor(provider, registryAddress, next, lastTx), /网络不匹配/);
  console.log('Public-read verifier checked against EVM: matching receipt, unrelated receipt, wrong chain');

  const secret = '0x' + '42'.repeat(32);
  const lockHash = ethers.sha256(secret);
  const value = 10n ** 14n;
  const escrowFactory = factory('TimedHashlockEscrow');
  await assert.rejects(escrowFactory.deploy(receiverAddress, lockHash, 10), /InvalidTerms|execution reverted/);
  const escrow = await escrowFactory.deploy(receiverAddress, lockHash, 10, {value});
  await receipt(escrow.deploymentTransaction());
  const escrowAddress = await escrow.getAddress();
  assert.equal(await rawBalance(escrowAddress), value);
  await assert.rejects(escrow.connect(other).claim.staticCall(secret), /NotBeneficiary/);
  await assert.rejects(escrow.connect(receiver).claim.staticCall('0x' + '43'.repeat(32)), /WrongSecret/);
  await assert.rejects(escrow.refund.staticCall(), /NotExpired/);
  const beforeReceiver = await rawBalance(receiverAddress);
  const claimed = await receipt(escrow.connect(receiver).claim(secret));
  assert.equal(await escrow.status(), 1n);
  assert.equal(await rawBalance(escrowAddress), 0n);
  assert.equal((await rawBalance(receiverAddress)) - beforeReceiver + claimed.fee, value);
  await assert.rejects(escrow.connect(receiver).claim.staticCall(secret), /AlreadySettled/);
  console.log('Escrow claimed:', String(value), 'wei; wrong user, wrong secret, and double claim rejected');

  const refundable = await escrowFactory.deploy(receiverAddress, lockHash, 2, {value});
  await receipt(refundable.deploymentTransaction());
  await connection.provider.request({method: 'hardhat_mine', params: ['0x2']});
  await assert.rejects(refundable.connect(receiver).claim.staticCall(secret), /Expired/);
  await assert.rejects(refundable.connect(other).refund.staticCall(), /NotDepositor/);
  const owner = await deployer.getAddress();
  const beforeOwner = await rawBalance(owner);
  const refunded = await receipt(refundable.refund());
  assert.equal(await refundable.status(), 2n);
  assert.equal(await rawBalance(await refundable.getAddress()), 0n);
  assert.equal((await rawBalance(owner)) - beforeOwner + refunded.fee, value);
  console.log('Expired escrow refunded:', String(value), 'wei; late claim and foreign refund rejected');
  console.log('ALL LOCAL EVM CHECKS PASSED');
} finally {
  await connection.close();
}
