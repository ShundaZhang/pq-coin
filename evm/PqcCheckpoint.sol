// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice Append-only checkpoints from one publisher for one local PQC chain.
/// @dev Records commitments only. Does not verify the local chain or its signatures.
contract PqcCheckpoint {
    error NotPublisher();
    error InvalidGenesis();
    error InvalidBlockHash();
    error HeightNotIncreasing(uint256 previousHeight, uint256 givenHeight);

    address public immutable publisher;
    bytes32 public immutable genesisHash;
    uint256 public latestHeight;
    mapping(uint256 => bytes32) public checkpoints;

    event CheckpointAnchored(
        bytes32 indexed genesisHash,
        uint256 indexed height,
        bytes32 blockHash
    );

    constructor(bytes32 genesis) {
        if (genesis == bytes32(0)) revert InvalidGenesis();
        publisher = msg.sender;
        genesisHash = genesis;
    }

    function anchor(uint256 height, bytes32 blockHash) external {
        if (msg.sender != publisher) revert NotPublisher();
        if (blockHash == bytes32(0)) revert InvalidBlockHash();
        if (height <= latestHeight) revert HeightNotIncreasing(latestHeight, height);

        checkpoints[height] = blockHash;
        latestHeight = height;
        emit CheckpointAnchored(genesisHash, height, blockHash);
    }
}
