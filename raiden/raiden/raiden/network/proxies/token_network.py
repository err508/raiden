from collections import defaultdict
from gevent.event import AsyncResult
from typing import List, NamedTuple, Optional

import structlog
from eth_utils import (
    encode_hex,
    event_abi_to_log_topic,
    is_binary_address,
    to_canonical_address,
    to_checksum_address,
    to_normalized_address,
)
from gevent.lock import RLock, Semaphore
from raiden_contracts.constants import (
    ChannelState,
    CONTRACT_TOKEN_NETWORK,
    ChannelEvent,
    ChannelInfoIndex,
    ParticipantInfoIndex,
)
from raiden_contracts.contract_manager import CONTRACT_MANAGER
from web3.utils.filters import Filter

from raiden.exceptions import (
    ChannelOutdatedError,
    ContractVersionMismatch,
    DepositMismatch,
    WithdrawMismatch,
    DuplicatedChannelError,
    InvalidAddress,
    InvalidSettleTimeout,
    RaidenRecoverableError,
    RaidenUnrecoverableError,
    SamePeerAddress,
    TransactionThrew,
)
from raiden.network.proxies import Token
from raiden.network.rpc.client import check_address_has_code
from raiden.network.rpc.transactions import (
    check_transaction_threw,
)
from raiden.settings import (
    EXPECTED_CONTRACTS_VERSION,
)
from raiden.utils import (
    compare_versions,
    pex,
    privatekey_to_address,
    typing,
)
from raiden.transfer.utils import hash_balance_data

log = structlog.get_logger(__name__)  # pylint: disable=invalid-name


class ChannelData(NamedTuple):
    channel_identifier: typing.ChannelID
    settle_block_number: typing.BlockNumber
    state: int


class ParticipantDetails(NamedTuple):
    address: typing.Address
    deposit: typing.TokenAmount
    withdrawn: typing.TokenAmount
    is_closer: bool
    balance_hash: typing.BalanceHash
    nonce: typing.Nonce


class ParticipantsDetails(NamedTuple):
    our_details: ParticipantDetails
    partner_details: ParticipantDetails


class ChannelDetails(NamedTuple):
    chain_id: typing.ChainID
    channel_data: int
    participants_data: ParticipantsDetails


class TokenNetwork:
    def __init__(
            self,
            jsonrpc_client,
            manager_address,
    ):
        if not is_binary_address(manager_address):
            raise InvalidAddress('Expected binary address format for token nework')

        check_address_has_code(jsonrpc_client, manager_address, CONTRACT_TOKEN_NETWORK)

        proxy = jsonrpc_client.new_contract_proxy(
            CONTRACT_MANAGER.get_contract_abi(CONTRACT_TOKEN_NETWORK),
            to_normalized_address(manager_address),
        )

        is_good_version = compare_versions(
            proxy.contract.functions.contract_version().call(),
            EXPECTED_CONTRACTS_VERSION,
        )
        if not is_good_version:
            raise ContractVersionMismatch('Incompatible ABI for TokenNetwork')

        self.address = manager_address
        self.proxy = proxy
        self.client = jsonrpc_client
        self.node_address = privatekey_to_address(self.client.privkey)
        self.open_channel_transactions = dict()

        # Forbids concurrent operations on the same channel
        self.channel_operations_lock = defaultdict(RLock)

        # Serializes concurent deposits on this token network. This must be an
        # exclusive lock, since we need to coordinate the approve and
        # setTotalDeposit calls.
        self.deposit_lock = Semaphore()

    def _call_and_check_result(self, function_name: str, *args):
        fn = getattr(self.proxy.contract.functions, function_name)
        call_result = fn(*args).call()

        if call_result == b'':
            raise RuntimeError(f"Call to '{function_name}' returned nothing")

        return call_result

    def token_address(self) -> typing.Address:
        """ Return the token of this manager. """
        return to_canonical_address(self.proxy.contract.functions.token().call())

    def new_netting_channel(
            self,
            partner: typing.Address,
            settle_timeout: int,
    ) -> typing.ChannelID:
        """ Creates a new channel in the TokenNetwork contract.

        Args:
            partner: The peer to open the channel with.
            settle_timeout: The settle timout to use for this channel.

        Returns:
            The ChannelID of the new netting channel.
        """
        if not is_binary_address(partner):
            raise InvalidAddress('Expected binary address format for channel partner')

        invalid_timeout = (
            settle_timeout < self.settlement_timeout_min() or
            settle_timeout > self.settlement_timeout_max()
        )
        if invalid_timeout:
            raise InvalidSettleTimeout('settle_timeout must be in range [{}, {}], is {}'.format(
                self.settlement_timeout_min(),
                self.settlement_timeout_max(),
                settle_timeout,
            ))

        if self.node_address == partner:
            raise SamePeerAddress('The other peer must not have the same address as the client.')

        # Prevent concurrent attempts to open a channel with the same token and
        # partner address.
        if partner not in self.open_channel_transactions:
            new_open_channel_transaction = AsyncResult()
            self.open_channel_transactions[partner] = new_open_channel_transaction

            try:
                transaction_hash = self._new_netting_channel(partner, settle_timeout)
            except Exception as e:
                new_open_channel_transaction.set_exception(e)
                raise
            else:
                new_open_channel_transaction.set(transaction_hash)
            finally:
                self.open_channel_transactions.pop(partner, None)
        else:
            # All other concurrent threads should block on the result of opening this channel
            self.open_channel_transactions[partner].get()

        channel_created = self.channel_exists_and_not_settled(self.node_address, partner)
        if channel_created is False:
            log.error(
                'creating new channel failed',
                peer1=pex(self.node_address),
                peer2=pex(partner),
            )
            raise RaidenUnrecoverableError('creating new channel failed')

        channel_identifier = self.detail_channel(self.node_address, partner).channel_identifier

        log.info(
            'new_netting_channel called',
            peer1=pex(self.node_address),
            peer2=pex(partner),
            channel_identifier=channel_identifier,
        )

        return channel_identifier

    def _new_netting_channel(self, partner: typing.Address, settle_timeout: int):
        if self.channel_exists_and_not_settled(self.node_address, partner):
            raise DuplicatedChannelError('Channel with given partner address already exists')

        transaction_hash = self.proxy.transact(
            'openChannel',
            self.node_address,
            partner,
            settle_timeout,
        )

        if not transaction_hash:
            raise RuntimeError('open channel transaction failed')

        self.client.poll(transaction_hash)

        if check_transaction_threw(self.client, transaction_hash):
            raise DuplicatedChannelError('Duplicated channel')

        return transaction_hash

    def _inspect_channel_identifier(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            called_by_fn: str,
            channel_identifier: typing.ChannelID = None,
    ) -> typing.ChannelID:
        if not channel_identifier:
            channel_identifier = self._call_and_check_result(
                'getChannelIdentifier',
                to_checksum_address(participant1),
                to_checksum_address(participant2),
            )
        assert isinstance(channel_identifier, typing.T_ChannelID)
        if channel_identifier == 0:
            raise RaidenRecoverableError(
                f'When calling {called_by_fn} either 0 value was given for the '
                'channel_identifier or getChannelIdentifier returned 0, meaning '
                f'no channel currently exists between {pex(participant1)} and '
                f'{pex(participant2)}',
            )
        return channel_identifier

    def channel_exists_and_not_settled(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID = None,
    ) -> bool:
        """Returns if the channel exists and is in a non-settled state"""
        try:
            channel_state = self._get_channel_state(participant1, participant2, channel_identifier)
        except RaidenRecoverableError:
            return False
        exists_and_not_settled = (
            channel_state > ChannelState.NONEXISTENT and
            channel_state < ChannelState.SETTLED
        )
        return exists_and_not_settled

    def detail_participant(
            self,
            channel_identifier: typing.ChannelID,
            participant: typing.Address,
            partner: typing.Address,
    ) -> ParticipantDetails:
        """ Returns a dictionary with the channel participant information. """

        data = self._call_and_check_result(
            'getChannelParticipantInfo',
            channel_identifier,
            to_checksum_address(participant),
            to_checksum_address(partner),
        )
        return ParticipantDetails(
            address=participant,
            deposit=data[ParticipantInfoIndex.DEPOSIT],
            withdrawn=data[ParticipantInfoIndex.WITHDRAWN],
            is_closer=data[ParticipantInfoIndex.IS_CLOSER],
            balance_hash=data[ParticipantInfoIndex.BALANCE_HASH],
            nonce=data[ParticipantInfoIndex.NONCE],
        )

    def detail_channel(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID = None,
    ) -> ChannelData:
        """ Returns a ChannelData instance with the channel specific information.

        If no specific channel_identifier is given then it tries to see if there
        is a currently open channel and uses that identifier.

        """
        channel_identifier = self._inspect_channel_identifier(
            participant1=participant1,
            participant2=participant2,
            called_by_fn='detail_channel',
            channel_identifier=channel_identifier,
        )

        channel_data = self._call_and_check_result(
            'getChannelInfo',
            channel_identifier,
            to_checksum_address(participant1),
            to_checksum_address(participant2),
        )

        return ChannelData(
            channel_identifier=channel_identifier,
            settle_block_number=channel_data[ChannelInfoIndex.SETTLE_BLOCK],
            state=channel_data[ChannelInfoIndex.STATE],
        )

    def detail_participants(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID = None,
    ) -> ParticipantsDetails:
        """ Returns a ParticipantsDetails instance with the participants'
            channel information.

        Note:
            For now one of the participants has to be the node_address
        """
        if self.node_address not in (participant1, participant2):
            raise ValueError('One participant must be the node address')

        if self.node_address == participant2:
            participant1, participant2 = participant2, participant1

        channel_identifier = self._inspect_channel_identifier(
            participant1=participant1,
            participant2=participant2,
            called_by_fn='details_participants',
            channel_identifier=channel_identifier,
        )

        our_data = self.detail_participant(channel_identifier, participant1, participant2)
        partner_data = self.detail_participant(channel_identifier, participant2, participant1)
        return ParticipantsDetails(our_details=our_data, partner_details=partner_data)

    def detail(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID = None,
    ) -> ChannelDetails:
        """ Returns a ChannelDetails instance with all the details of the
            channel and the channel participants.

        Note:
            For now one of the participants has to be the node_address
        """
        if self.node_address not in (participant1, participant2):
            raise ValueError('One participant must be the node address')

        if self.node_address == participant2:
            participant1, participant2 = participant2, participant1

        channel_data = self.detail_channel(participant1, participant2, channel_identifier)
        participants_data = self.detail_participants(
            participant1,
            participant2,
            channel_data.channel_identifier,
        )
        chain_id = self.proxy.contract.functions.chain_id().call()

        return ChannelDetails(
            chain_id=chain_id,
            channel_data=channel_data,
            participants_data=participants_data,
        )

    def settlement_timeout_min(self) -> int:
        """ Returns the minimal settlement timeout for the token network. """
        return self.proxy.contract.functions.settlement_timeout_min().call()

    def settlement_timeout_max(self) -> int:
        """ Returns the maximal settlement timeout for the token network. """
        return self.proxy.contract.functions.settlement_timeout_max().call()

    def settle_block_number(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID = None,
    ) -> typing.Optional[typing.BlockNumber]:
        """ Returns the channel settle_block_number if it is not yet settled and None Otherwise """
        channel_data = self.detail_channel(
            participant1=participant1,
            participant2=participant2,
            channel_identifier=channel_identifier,
        )
        return channel_data.settle_block_number

    def channel_is_opened(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID = None,
    ) -> bool:
        """ Returns true if the channel is in an open state, false otherwise. """
        try:
            channel_state = self._get_channel_state(participant1, participant2, channel_identifier)
        except RaidenRecoverableError:
            return False
        return channel_state == ChannelState.OPENED

    def channel_is_closed(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID = None,
    ) -> bool:
        """ Returns true if the channel is in a closed state, false otherwise. """
        try:
            channel_state = self._get_channel_state(participant1, participant2, channel_identifier)
        except RaidenRecoverableError:
            return False
        return channel_state == ChannelState.CLOSED

    def channel_is_settled(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID = None,
    ) -> bool:
        """ Returns true if the channel is in a settled state, false otherwise. """
        try:
            channel_state = self._get_channel_state(participant1, participant2, channel_identifier)
        except RaidenRecoverableError:
            return False
        return channel_state >= ChannelState.SETTLED

    def closing_address(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID = None,
    ) -> Optional[typing.Address]:
        """ Returns the address of the closer, if the channel is closed and not settled. None
        otherwise. """

        try:
            channel_data = self.detail_channel(participant1, participant2, channel_identifier)
        except RaidenRecoverableError:
            return None

        if channel_data.state >= ChannelState.SETTLED:
            return None

        participants_data = self.detail_participants(
            participant1=participant1,
            participant2=participant2,
            channel_identifier=channel_data.channel_identifier,
        )

        if participants_data.our_details.is_closer:
            return participants_data.our_details.address
        elif participants_data.partner_details.is_closer:
            return participants_data.partner_details.address

        return None

    def can_transfer(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID = None,
    ) -> bool:
        """ Returns True if the channel is opened and the node has deposit in
        it.

        Note: Having a deposit does not imply having a balance for off-chain
        transfers. """
        opened = self.channel_is_opened(participant1, participant2, channel_identifier)

        if opened is False:
            return False

        deposit = self.detail_participant(
            channel_identifier,
            participant1,
            participant2,
        ).deposit
        return deposit > 0

    def set_total_deposit(
            self,
            channel_identifier: typing.ChannelID,
            total_deposit: typing.TokenAmount,
            partner: typing.Address,
    ):
        """ Set total token deposit in the channel to total_deposit.

        Raises:
            ChannelBusyError: If the channel is busy with another operation
            RuntimeError: If the token address is empty.
        """
        if not isinstance(total_deposit, int):
            raise ValueError('total_deposit needs to be an integral number.')

        self._check_for_outdated_channel(
            self.node_address,
            partner,
            channel_identifier,
        )

        token_address = self.token_address()
        token = Token(self.client, token_address)

        with self.channel_operations_lock[partner], self.deposit_lock:
            # setTotalDeposit requires a monotonically increasing value. This
            # is used to handle concurrent actions:
            #
            #  - The deposits will be done in order, i.e. the monotonic
            #  property is preserved by the caller
            #  - The race of two deposits will be resolved with the larger
            #  deposit winning
            #  - Retries wont have effect
            #
            # This check is serialized with the channel_operations_lock to avoid
            # sending invalid transactions on-chain (decreasing total deposit).
            #
            current_deposit = self.detail_participant(
                channel_identifier,
                self.node_address,
                partner,
            ).deposit
            amount_to_deposit = total_deposit - current_deposit
            if total_deposit < current_deposit:
                raise DepositMismatch(
                    f'Current deposit ({current_deposit}) is already larger '
                    f'than the requested total deposit amount ({total_deposit})',
                )
            if amount_to_deposit <= 0:
                raise ValueError(f'deposit {amount_to_deposit} must be greater than 0.')

            # A node may be setting up multiple channels for the same token
            # concurrently. Because each deposit changes the user balance this
            # check must be serialized with the operation locks.
            #
            # This check is merely informational, used to avoid sending
            # transactions which are known to fail.
            #
            # It is serialized with the deposit_lock to avoid sending invalid
            # transactions on-chain (account without balance). The lock
            # channel_operations_lock is not sufficient, as it allows two
            # concurrent deposits for different channels.
            #
            current_balance = token.balance_of(self.node_address)
            if current_balance < amount_to_deposit:
                raise ValueError(
                    f'deposit {amount_to_deposit} can not be larger than the '
                    f'available balance {current_balance}, '
                    f'for token at address {pex(token_address)}',
                )

            # If there are channels being set up concurrenlty either the
            # allowance must be accumulated *or* the calls to `approve` and
            # `setTotalDeposit` must be serialized. This is necessary otherwise
            # the deposit will fail.
            #
            # Calls to approve and setTotalDeposit are serialized with the
            # deposit_lock to avoid transaction failure, because with two
            # concurrent deposits, we may have the transactions executed in the
            # following order
            #
            # - approve
            # - approve
            # - setTotalDeposit
            # - setTotalDeposit
            #
            # in which case  the second `approve` will overwrite the first,
            # and the first `setTotalDeposit` will consume the allowance,
            #  making the second deposit fail.
            token.approve(self.address, amount_to_deposit)

            log_details = {
                'token_network': pex(self.address),
                'node': pex(self.node_address),
                'partner': pex(partner),
                'total_deposit': total_deposit,
                'amount_to_deposit': amount_to_deposit,
                'id': id(self),
            }
            log.info('deposit called', **log_details)

            transaction_hash = self.proxy.transact(
                'setTotalDeposit',
                channel_identifier,
                self.node_address,
                total_deposit,
                partner,
            )
            self.client.poll(transaction_hash)

            receipt_or_none = check_transaction_threw(self.client, transaction_hash)

            if receipt_or_none:
                if token.allowance(self.node_address, self.address) < amount_to_deposit:
                    log_msg = (
                        'deposit failed. The allowance is insufficient, check concurrent deposits '
                        'for the same token network but different proxies.'
                    )
                elif token.balance_of(self.node_address) < amount_to_deposit:
                    log_msg = 'deposit failed. The address doesnt have funds'
                else:
                    log_msg = 'deposit failed'

                log.critical(log_msg, **log_details)

                self._check_channel_state_for_deposit(
                    self.node_address,
                    partner,
                    channel_identifier,
                    total_deposit,
                )

                raise TransactionThrew('Deposit', receipt_or_none)

            log.info('deposit successful', **log_details)

    def close(
            self,
            channel_identifier: typing.ChannelID,
            partner: typing.Address,
            balance_hash: typing.BalanceHash,
            nonce: typing.Nonce,
            additional_hash: typing.AdditionalHash,
            signature: typing.Signature,
    ):
        """ Close the channel using the provided balance proof.

        Raises:
            ChannelBusyError: If the channel is busy with another operation.
            RaidenRecoverableError: If the channel is already closed.
            RaidenUnrecoverableError: If the channel does not exist or is settled.
        """

        log_details = {
            'token_network': pex(self.address),
            'node': pex(self.node_address),
            'partner': pex(partner),
            'nonce': nonce,
            'balance_hash': encode_hex(balance_hash),
            'additional_hash': encode_hex(additional_hash),
            'signature': encode_hex(signature),
        }
        log.info('close called', **log_details)

        self._check_for_outdated_channel(
            self.node_address,
            partner,
            channel_identifier,
        )

        self._check_channel_state_for_close(
            self.node_address,
            partner,
            channel_identifier,
        )

        with self.channel_operations_lock[partner]:
            transaction_hash = self.proxy.transact(
                'closeChannel',
                channel_identifier,
                partner,
                balance_hash,
                nonce,
                additional_hash,
                signature,
            )
            self.client.poll(transaction_hash)

            receipt_or_none = check_transaction_threw(self.client, transaction_hash)
            if receipt_or_none:
                log.critical('close failed', **log_details)

                self._check_channel_state_for_close(
                    self.node_address,
                    partner,
                    channel_identifier,
                )

                raise TransactionThrew('Close', receipt_or_none)

            log.info('close successful', **log_details)

    def update_transfer(
            self,
            channel_identifier: typing.ChannelID,
            partner: typing.Address,
            balance_hash: typing.BalanceHash,
            nonce: typing.Nonce,
            additional_hash: typing.AdditionalHash,
            closing_signature: typing.Signature,
            non_closing_signature: typing.Signature,
    ):
        log_details = {
            'token_network': pex(self.address),
            'node': pex(self.node_address),
            'partner': pex(partner),
            'nonce': nonce,
            'balance_hash': encode_hex(balance_hash),
            'additional_hash': encode_hex(additional_hash),
            'closing_signature': encode_hex(closing_signature),
            'non_closing_signature': encode_hex(non_closing_signature),
        }
        log.info('updateNonClosingBalanceProof called', **log_details)

        self._check_for_outdated_channel(
            self.node_address,
            partner,
            channel_identifier,
        )

        transaction_hash = self.proxy.transact(
            'updateNonClosingBalanceProof',
            channel_identifier,
            partner,
            self.node_address,
            balance_hash,
            nonce,
            additional_hash,
            closing_signature,
            non_closing_signature,
        )

        self.client.poll(transaction_hash)

        receipt_or_none = check_transaction_threw(self.client, transaction_hash)
        if receipt_or_none:
            log.critical('updateNonClosingBalanceProof failed', **log_details)
            channel_closed = self.channel_is_closed(
                participant1=self.node_address,
                participant2=partner,
                channel_identifier=channel_identifier,
            )
            if channel_closed is False:
                raise RaidenUnrecoverableError('Channel is not in a closed state')
            raise TransactionThrew('Update NonClosing balance proof', receipt_or_none)

        log.info('updateNonClosingBalanceProof successful', **log_details)

    def withdraw(
            self,
            channel_identifier: typing.ChannelID,
            partner: typing.Address,
            total_withdraw: int,
            partner_signature: typing.Address,
            signature: typing.Signature,
    ):
        log_details = {
            'token_network': pex(self.address),
            'node': pex(self.node_address),
            'partner': pex(partner),
            'total_withdraw': total_withdraw,
            'partner_signature': encode_hex(partner_signature),
            'signature': encode_hex(signature),
        }
        log.info('withdraw called', **log_details)

        self._check_for_outdated_channel(
            self.node_address,
            partner,
            channel_identifier,
        )

        current_withdraw = self.detail_participant(
            channel_identifier,
            self.node_address,
            partner,
        ).withdrawn
        amount_to_withdraw = total_withdraw - current_withdraw
        if total_withdraw < current_withdraw:
            raise WithdrawMismatch(
                f'Current withdraw ({current_withdraw}) is already larger '
                f'than the requested total withdraw amount ({total_withdraw})',
            )
        if amount_to_withdraw <= 0:
            raise ValueError(f'withdraw {amount_to_withdraw} must be greater than 0.')

        with self.channel_operations_lock[partner]:
            transaction_hash = self.proxy.transact(
                'setTotalWithdraw',
                channel_identifier,
                self.node_address,
                total_withdraw,
                partner_signature,
                signature,
            )
            self.client.poll(transaction_hash)

            receipt_or_none = check_transaction_threw(self.client, transaction_hash)
            if receipt_or_none:
                log.critical('withdraw failed', **log_details)

                self._check_channel_state_for_withdraw(
                    self.node_address,
                    partner,
                    channel_identifier,
                    total_withdraw,
                )

                raise TransactionThrew('Withdraw', receipt_or_none)

            log.info('withdraw successful', **log_details)

    def unlock(
            self,
            channel_identifier: typing.ChannelID,
            partner: typing.Address,
            merkle_tree_leaves: typing.MerkleTreeLeaves,
    ):
        log_details = {
            'token_network': pex(self.address),
            'node': pex(self.node_address),
            'partner': pex(partner),
            'merkle_tree_leaves': merkle_tree_leaves,
        }

        if merkle_tree_leaves is None or not merkle_tree_leaves:
            log.info('skipping unlock, tree is empty', **log_details)
            return

        log.info('unlock called', **log_details)

        leaves_packed = b''.join(lock.encoded for lock in merkle_tree_leaves)

        transaction_hash = self.proxy.transact(
            'unlock',
            channel_identifier,
            self.node_address,
            partner,
            leaves_packed,
        )

        self.client.poll(transaction_hash)
        receipt_or_none = check_transaction_threw(self.client, transaction_hash)

        if receipt_or_none:
            channel_settled = self.channel_is_settled(
                participant1=self.node_address,
                participant2=partner,
                channel_identifier=channel_identifier,
            )

            if channel_settled is False:
                log.critical('unlock failed. Channel is not in a settled state', **log_details)
                raise RaidenUnrecoverableError(
                    'Channel is not in a settled state. An unlock cannot be made',
                )

            log.critical('unlock failed', **log_details)
            raise TransactionThrew('Unlock', receipt_or_none)

        log.info('unlock successful', **log_details)

    def settle(
            self,
            channel_identifier: typing.ChannelID,
            transferred_amount: int,
            locked_amount: int,
            locksroot: typing.Locksroot,
            partner: typing.Address,
            partner_transferred_amount: int,
            partner_locked_amount: int,
            partner_locksroot: typing.Locksroot,
    ):
        """ Settle the channel.

        Raises:
            ChannelBusyError: If the channel is busy with another operation
        """
        log_details = {
            'token_network': pex(self.address),
            'node': pex(self.node_address),
            'partner': pex(partner),
            'transferred_amount': transferred_amount,
            'locked_amount': locked_amount,
            'locksroot': encode_hex(locksroot),
            'partner_transferred_amount': partner_transferred_amount,
            'partner_locked_amount': partner_locked_amount,
            'partner_locksroot': encode_hex(partner_locksroot),
        }
        log.info('settle called', **log_details)

        self._check_for_outdated_channel(
            self.node_address,
            partner,
            channel_identifier,
        )

        with self.channel_operations_lock[partner]:
            if self._verify_settle_state(
                transferred_amount,
                locked_amount,
                locksroot,
                partner,
                partner_transferred_amount,
                partner_locked_amount,
                partner_locksroot,
            ) is False:
                raise RaidenRecoverableError('local state can not be used to call settle')
            our_maximum = transferred_amount + locked_amount
            partner_maximum = partner_transferred_amount + partner_locked_amount

            # The second participant transferred + locked amount must be higher
            our_bp_is_larger = our_maximum > partner_maximum

            if our_bp_is_larger:
                transaction_hash = self.proxy.transact(
                    'settleChannel',
                    channel_identifier,
                    partner,
                    partner_transferred_amount,
                    partner_locked_amount,
                    partner_locksroot,
                    self.node_address,
                    transferred_amount,
                    locked_amount,
                    locksroot,
                )
            else:
                transaction_hash = self.proxy.transact(
                    'settleChannel',
                    channel_identifier,
                    self.node_address,
                    transferred_amount,
                    locked_amount,
                    locksroot,
                    partner,
                    partner_transferred_amount,
                    partner_locked_amount,
                    partner_locksroot,
                )

            self.client.poll(transaction_hash)
            receipt_or_none = check_transaction_threw(self.client, transaction_hash)
            if receipt_or_none:
                log.info('settle failed', **log_details)
                self._check_channel_state_for_settle(
                    self.node_address,
                    partner,
                    channel_identifier,
                )
                raise TransactionThrew('Settle', receipt_or_none)

            log.info('settle successful', **log_details)

    def _verify_settle_state(
        self,
        transferred_amount: int,
        locked_amount: int,
        locksroot: typing.Locksroot,
        partner: typing.Address,
        partner_transferred_amount: int,
        partner_locked_amount: int,
        partner_locksroot: typing.Locksroot,
    ):
        """Check if our local state is up to date with on-chain state."""
        participant_data = self.detail_participants(self.node_address, partner)
        our_balance_hash_ok = hash_balance_data(
            transferred_amount,
            locked_amount,
            locksroot,
        ) == participant_data.our_details.balance_hash
        partner_balance_hash_ok = hash_balance_data(
            partner_transferred_amount,
            partner_locked_amount,
            partner_locksroot,
        ) == participant_data.partner_details.balance_hash
        return our_balance_hash_ok and partner_balance_hash_ok

    def events_filter(
            self,
            topics: List[str] = None,
            from_block: typing.BlockSpecification = None,
            to_block: typing.BlockSpecification = None,
    ) -> Filter:
        """ Install a new filter for an array of topics emitted by the contract.
        Args:
            topics: A list of event ids to filter for. Can also be None,
                    in which case all events are queried.
            from_block: The block number at which to start looking for events.
            to_block: The block number at which to stop looking for events.
        Return:
            Filter: The filter instance.
        """
        return self.client.new_filter(
            self.address,
            topics=topics,
            from_block=from_block,
            to_block=to_block,
        )

    def channelnew_filter(
            self,
            from_block: typing.BlockSpecification = 0,
            to_block: typing.BlockSpecification = 'latest',
    ) -> Filter:
        """ Install a new filter for ChannelNew events.

        Args:
            from_block: Create filter starting from this block number (default: 0).
            to_block: Create filter stopping at this block number (default: 'latest').

        Return:
            The filter instance.
        """
        event_abi = CONTRACT_MANAGER.get_event_abi(CONTRACT_TOKEN_NETWORK, ChannelEvent.OPENED)
        event_id = encode_hex(event_abi_to_log_topic(event_abi))
        topics = [event_id]
        return self.events_filter(topics, from_block, to_block)

    def all_events_filter(
            self,
            from_block: typing.BlockSpecification = 0,
            to_block: typing.BlockSpecification = 'latest',
    ) -> Filter:
        """ Install a new filter for all the events emitted by the current token network contract

        Args:
            from_block: Create filter starting from this block number (default: 0).
            to_block: Create filter stopping at this block number (default: 'latest').

        Return:
            The filter instance.
        """
        return self.events_filter(None, from_block, to_block)

    def _check_for_outdated_channel(
            self,
            participant1: typing.Address,
            participant2: typing.Address,
            channel_identifier: typing.ChannelID,
    ):
        """
        Checks whether an operation is being execute on a channel
        between two participants using an old channel identifier
        """
        try:
            onchain_channel_details = self.detail_channel(
                participant1,
                participant2,
            )
        except RaidenRecoverableError:
            return

        onchain_channel_identifier = onchain_channel_details.channel_identifier

        if onchain_channel_identifier != channel_identifier:
            raise ChannelOutdatedError(
                'Current channel identifier is outdated. '
                f'current={channel_identifier}, '
                f'new={onchain_channel_identifier}',
            )

    def _get_channel_state(self, participant1, participant2, channel_identifier):
        channel_data = self.detail_channel(participant1, participant2, channel_identifier)

        if not isinstance(channel_data.state, typing.T_ChannelState):
            raise ValueError('channel state must be of type ChannelState')

        return channel_data.state

    def _check_channel_state_for_close(self, participant1, participant2, channel_identifier):
        channel_state = self._get_channel_state(
            participant1=participant1,
            participant2=participant2,
            channel_identifier=channel_identifier,
        )

        if channel_state in (ChannelState.NONEXISTENT, ChannelState.REMOVED):
            raise RaidenUnrecoverableError(
                f'Channel between participant {participant1} '
                f'and {participant2} does not exist',
            )
        elif channel_state == ChannelState.SETTLED:
            raise RaidenUnrecoverableError(
                'A settled channel cannot be closed',
            )
        elif channel_state == ChannelState.CLOSED:
            raise RaidenRecoverableError(
                'Channel is already closed',
            )

    def _check_channel_state_for_deposit(
            self,
            participant1,
            participant2,
            channel_identifier,
            deposit_amount,
    ):
        participant_details = self.detail_participants(
            participant1,
            participant2,
            channel_identifier,
        )

        channel_state = self._get_channel_state(
            participant1=self.node_address,
            participant2=participant2,
            channel_identifier=channel_identifier,
        )
        # Check if deposit is being made on a nonexistent channel
        if channel_state in (ChannelState.NONEXISTENT, ChannelState.REMOVED):
            raise RaidenUnrecoverableError(
                f'Channel between participant {participant1} '
                f'and {participant2} does not exist',
            )
        # Deposit was prohibited because the channel is settled
        elif channel_state == ChannelState.SETTLED:
            raise RaidenUnrecoverableError(
                'Deposit is not possible due to channel being settled',
            )
        # Deposit was prohibited because the channel is closed
        elif channel_state == ChannelState.CLOSED:
            raise RaidenRecoverableError(
                'Channel is already closed',
            )
        elif participant_details.our_details.deposit < deposit_amount:
            raise RaidenUnrecoverableError('Deposit amount decreased')

    def _check_channel_state_for_withdraw(
            self,
            participant1,
            participant2,
            channel_identifier,
            withdraw_amount,
    ):
        participant_details = self.detail_participants(
            participant1,
            participant2,
            channel_identifier,
        )

        if participant_details.our_details.withdrawn > withdraw_amount:
            raise WithdrawMismatch('Withdraw amount decreased')

        channel_state = self._get_channel_state(
            participant1=participant1,
            participant2=participant2,
            channel_identifier=channel_identifier,
        )

        if channel_state in (ChannelState.NONEXISTENT, ChannelState.REMOVED):
            raise RaidenUnrecoverableError(
                f'Channel between participant {participant1} '
                f'and {participant2} does not exist',
            )
        elif channel_state == ChannelState.SETTLED:
            raise RaidenUnrecoverableError(
                'A settled channel cannot be closed',
            )
        elif channel_state == ChannelState.CLOSED:
            raise RaidenRecoverableError(
                'Channel is already closed',
            )

    def _check_channel_state_for_settle(self, participant1, participant2, channel_identifier):
        channel_data = self.detail_channel(participant1, participant2, channel_identifier)
        if channel_data.state == ChannelState.SETTLED:
            raise RaidenUnrecoverableError(
                'Channel is not in a closed state. It cannot be settled',
            )
        elif channel_data.state == ChannelState.REMOVED:
            raise RaidenRecoverableError(
                'Channel is already unlocked. It cannot be settled',
            )
        elif channel_data.state == ChannelState.OPENED:
            raise RaidenUnrecoverableError(
                'Channel is still open. It cannot be settled',
            )
        elif channel_data.state == ChannelState.CLOSED:
            if self.client.block_number() < channel_data.settle_block_number:
                raise RaidenUnrecoverableError(
                    'Channel cannot be settled before settlement window is over',
                )

            raise RaidenRecoverableError(
                'Channel cannot be settled before closing',
            )
