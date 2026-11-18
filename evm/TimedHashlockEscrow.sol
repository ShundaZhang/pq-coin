// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice Educational, one-shot escrow of native test ETH.
/// @dev Independent of the PQC chain; does not move PQC coins or verify ML-DSA.
contract TimedHashlockEscrow {
    error InvalidTerms();
    error NotBeneficiary();
    error NotDepositor();
    error AlreadySettled();
    error Expired();
    error NotExpired();
    error WrongSecret();
    error PaymentFailed();

    enum Status { Open, Claimed, Refunded }
    address public immutable depositor;
    address public immutable beneficiary;
    bytes32 public immutable hashlock;
    uint256 public immutable expiresAtBlock;
    uint256 public immutable amount;
    Status public status;

    event Settled(Status status, address indexed recipient, uint256 amount);

    constructor(address receiver, bytes32 lockHash, uint256 durationBlocks) payable {
        if (receiver == address(0) || lockHash == bytes32(0) ||
            durationBlocks < 2 || durationBlocks > 100_000 || msg.value == 0) {
            revert InvalidTerms();
        }
        depositor = msg.sender;
        beneficiary = receiver;
        hashlock = lockHash;
        expiresAtBlock = block.number + durationBlocks;
        amount = msg.value;
    }

    function claim(bytes32 secret) external {
        if (msg.sender != beneficiary) revert NotBeneficiary();
        if (status != Status.Open) revert AlreadySettled();
        if (block.number >= expiresAtBlock) revert Expired();
        if (sha256(abi.encodePacked(secret)) != hashlock) revert WrongSecret();
        settle(Status.Claimed, beneficiary);
    }

    function refund() external {
        if (msg.sender != depositor) revert NotDepositor();
        if (status != Status.Open) revert AlreadySettled();
        if (block.number < expiresAtBlock) revert NotExpired();
        settle(Status.Refunded, depositor);
    }

    function settle(Status newStatus, address recipient) private {
        status = newStatus;
        (bool success,) = payable(recipient).call{value: amount}("");
        if (!success) revert PaymentFailed();
        emit Settled(newStatus, recipient, amount);
    }
}
