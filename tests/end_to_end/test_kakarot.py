import logging

import pytest
import pytest_asyncio
from starknet_py.contract import Contract

from kakarot_scripts.constants import NETWORK, RPC_CLIENT
from kakarot_scripts.utils.kakarot import get_contract as get_solidity_contract
from kakarot_scripts.utils.kakarot import get_deployments, get_eoa, get_starknet_address
from kakarot_scripts.utils.starknet import (
    call,
    deploy_starknet_account,
    get_contract,
    get_starknet_account,
    invoke,
    wait_for_transaction,
)
from tests.end_to_end.bytecodes import test_cases
from tests.utils.constants import TRANSACTION_GAS_LIMIT
from tests.utils.helpers import (
    extract_memory_from_execute,
    generate_random_evm_address,
    hex_string_to_bytes_array,
)

params_execute = [pytest.param(case.pop("params"), **case) for case in test_cases]

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@pytest.fixture(scope="session")
def evm(deployer):
    """
    Return a cached EVM contract.
    """
    return get_contract("EVM", provider=deployer)


@pytest_asyncio.fixture(scope="session")
async def other():
    """
    Just another Starknet contract.
    """
    account_info = await deploy_starknet_account()
    return await get_starknet_account(account_info["address"])


@pytest.fixture(scope="session")
def class_hashes():
    """
    All declared class hashes.
    """
    from kakarot_scripts.utils.starknet import get_declarations

    return get_declarations()


@pytest_asyncio.fixture(scope="session")
async def origin(evm, max_fee):
    """
    Return a random EVM account to be used as origin.
    """
    evm_address = int(generate_random_evm_address(), 16)
    await evm.functions["deploy_account"].invoke_v1(evm_address, max_fee=max_fee)
    return evm_address


@pytest.mark.asyncio(scope="session")
class TestKakarot:
    class TestEVM:
        @pytest.mark.parametrize("params", params_execute)
        async def test_execute(
            self, eth: Contract, params: dict, evm: Contract, max_fee, origin
        ):
            result = await evm.functions["evm_call"].call(
                origin=origin,
                value=int(params["value"]),
                bytecode=hex_string_to_bytes_array(params["code"]),
                calldata=hex_string_to_bytes_array(params["calldata"]),
                access_list=[],
            )
            origin_starknet_address = (
                await evm.functions["get_starknet_address"].call(origin)
            ).contract_address
            self_balance = (
                await eth.functions["balanceOf"].call(origin_starknet_address)
            ).balance
            assert result.success == params["success"]
            assert result.stack_values[: result.stack_size] == (
                [
                    int(x)
                    for x in params["stack"]
                    .format(
                        account_address=origin,
                        timestamp=result.block_timestamp,
                        block_number=result.block_number,
                        self_balance=self_balance,
                    )
                    .split(",")
                ]
                if params["stack"]
                else []
            )
            assert bytes(extract_memory_from_execute(result)).hex() == params["memory"]
            assert bytes(result.return_data).hex() == params["return_data"]

            events = params.get("events")
            if events:
                # Events only show up in a transaction, thus we run the same call, but in a tx
                tx = await evm.functions["evm_execute"].invoke_v1(
                    origin=origin,
                    value=int(params["value"]),
                    bytecode=hex_string_to_bytes_array(params["code"]),
                    calldata=hex_string_to_bytes_array(params["calldata"]),
                    max_fee=max_fee,
                    access_list=[],
                )
                status = await wait_for_transaction(tx.hash)
                assert status == "✅"
                receipt = await RPC_CLIENT.get_transaction_receipt(tx.hash)
                assert [
                    [
                        # we remove the key that is used to convey the emitting kakarot evm contract
                        event.keys[1:],
                        event.data,
                    ]
                    for event in receipt.events
                    if event.from_address != eth.address
                ] == events

        # https://github.com/code-423n4/2024-09-kakarot-findings/issues/44
        async def test_execute_jump_creation_code(self, evm: Contract, origin):
            params = {
                "value": 0,
                "code": "605f5f53605660015360025f5ff0",
                "calldata": "",
                "stack": "0000000000000000000000000000000000000000000000000000000000000000",
                "memory": "",
                "return_data": "",
                "success": 1,
            }
            result = await evm.functions["evm_call"].call(
                origin=origin,
                value=int(params["value"]),
                bytecode=hex_string_to_bytes_array(params["code"]),
                calldata=hex_string_to_bytes_array(params["calldata"]),
                access_list=[],
            )
            assert result.success == params["success"]

    class TestGetStarknetAddress:
        async def test_should_return_same_as_deployed_address(self, new_eoa):
            eoa = await new_eoa()
            starknet_address = await get_starknet_address(eoa.address)
            assert eoa.starknet_contract.address == starknet_address

    class TestDeployExternallyOwnedAccount:
        async def test_should_deploy_starknet_contract_at_corresponding_address(
            self, new_eoa
        ):
            eoa = await new_eoa()
            actual_evm_address = (
                await call(
                    "account_contract",
                    "get_evm_address",
                    address=eoa.starknet_contract.address,
                )
            ).address
            assert actual_evm_address == int(eoa.address, 16)

    class TestRegisterAccount:
        async def test_should_fail_when_sender_is_not_account(self):
            evm_address = generate_random_evm_address()
            tx_hash = await invoke("kakarot", "register_account", int(evm_address, 16))
            receipt = await RPC_CLIENT.get_transaction_receipt(tx_hash)
            assert receipt.execution_status.name == "REVERTED"
            assert "Kakarot: Caller should be" in receipt.revert_reason

        async def test_should_fail_when_account_is_already_registered(self, new_eoa):
            eoa = await new_eoa()
            tx_hash = await invoke("kakarot", "register_account", int(eoa.address, 16))
            receipt = await RPC_CLIENT.get_transaction_receipt(tx_hash)
            assert receipt.execution_status.name == "REVERTED"
            assert "Kakarot: account already registered" in receipt.revert_reason

    class TestSetAccountStorage:
        class TestSetAuthorizedPreEip155Tx:
            async def test_should_fail_not_owner(self, new_eoa, other):
                eoa = await new_eoa()
                tx_hash = await invoke(
                    "kakarot",
                    "set_authorized_pre_eip155_tx",
                    int(eoa.address, 16),
                    True,
                    account=other,
                )
                receipt = await RPC_CLIENT.get_transaction_receipt(tx_hash)
                assert receipt.execution_status.name == "REVERTED"
                assert "Ownable: caller is not the owner" in receipt.revert_reason

    class TestUpgradeAccount:
        async def test_should_upgrade_account_class(self, new_eoa, class_hashes):
            eoa = await new_eoa()

            await invoke(
                "kakarot",
                "upgrade_account",
                int(eoa.address, 16),
                class_hashes["uninitialized_account_fixture"],
            )
            assert (
                await RPC_CLIENT.get_class_hash_at(eoa.starknet_contract.address)
                == class_hashes["uninitialized_account_fixture"]
            )

        async def test_should_fail_not_owner(self, new_eoa, class_hashes, other):
            eoa = await new_eoa()

            tx_hash = await invoke(
                "kakarot",
                "upgrade_account",
                int(eoa.address, 16),
                class_hashes["uninitialized_account_fixture"],
                account=other,
            )
            receipt = await RPC_CLIENT.get_transaction_receipt(tx_hash)
            assert receipt.execution_status.name == "REVERTED"
            assert "Ownable: caller is not the owner" in receipt.revert_reason

    class TestEthCallNativeCoinTransfer:
        async def test_eth_call_should_succeed(self, kakarot, new_eoa):
            eoa = await new_eoa()
            result = await kakarot.functions["eth_call"].call(
                nonce=0,
                origin=int(eoa.address, 16),
                to={"is_some": 1, "value": 0xDEAD},
                gas_limit=TRANSACTION_GAS_LIMIT,
                gas_price=1_000,
                value=1_000,
                data=bytes(),
                access_list=[],
            )

            assert result.success == 1
            assert result.return_data == []
            assert result.gas_used == 21_000

    class TestEthCallJumpCreationCodeDeployTx:
        async def test_eth_call_jump_creation_code_deploy_tx_should_succeed(
            self, kakarot, new_eoa
        ):
            eoa = await new_eoa()
            result = await kakarot.functions["eth_call"].call(
                nonce=0,
                origin=int(eoa.address, 16),
                to={"is_some": 0, "value": 0},
                gas_limit=TRANSACTION_GAS_LIMIT,
                gas_price=1_000,
                value=0,
                data=bytes.fromhex("605f5f53605660015360025f5ff0"),
                access_list=[],
            )

            assert result.success == 1

        async def test_eth_call_should_handle_uninitialized_class_update(
            self, kakarot, new_eoa, class_hashes
        ):
            eoa = await new_eoa()
            await invoke(
                "kakarot",
                "set_uninitialized_account_class_hash",
                class_hashes["uninitialized_account_fixture"],
            )

            # Verifying that when updating the uninitialized account class hash, the starknet address
            # of an already deployed account is not impacted
            assert (
                await call("kakarot", "get_starknet_address", int(eoa.address, 16))
            ).starknet_address == eoa.starknet_contract.address

            result = await kakarot.functions["eth_call"].call(
                nonce=0,
                origin=int(eoa.address, 16),
                to={"is_some": 1, "value": 0xDEAD},
                gas_limit=TRANSACTION_GAS_LIMIT,
                gas_price=1_000,
                value=1_000,
                data=bytes(),
                access_list=[],
            )

            assert result.success == 1
            assert result.return_data == []
            assert result.gas_used == 21_000

            await invoke(
                "kakarot",
                "set_uninitialized_account_class_hash",
                class_hashes["uninitialized_account"],
            )

    class TestUpgrade:
        async def test_should_raise_when_caller_is_not_owner(self, other, class_hashes):
            tx_hash = await invoke(
                "kakarot", "upgrade", class_hashes["EVM"], account=other
            )
            receipt = await RPC_CLIENT.get_transaction_receipt(tx_hash)
            assert receipt.execution_status.name == "REVERTED"
            assert "Ownable: caller is not the owner" in receipt.revert_reason

        async def test_should_raise_when_class_hash_is_not_declared(self):
            tx_hash = await invoke("kakarot", "upgrade", 0xDEAD)
            receipt = await RPC_CLIENT.get_transaction_receipt(tx_hash)
            assert receipt.execution_status.name == "REVERTED"
            assert "is not declared" in receipt.revert_reason

        async def test_should_upgrade_class_hash(self, kakarot, class_hashes):
            prev_class_hash = await RPC_CLIENT.get_class_hash_at(kakarot.address)
            await invoke("kakarot", "upgrade", class_hashes["replace_class"])
            new_class_hash = await RPC_CLIENT.get_class_hash_at(kakarot.address)
            assert prev_class_hash != new_class_hash
            assert new_class_hash == class_hashes["replace_class"]
            await invoke("kakarot", "upgrade", prev_class_hash)

    class TestTransferOwnership:
        async def test_should_raise_when_caller_is_not_owner(self, kakarot, other):
            prev_owner = (await kakarot.functions["get_owner"].call()).owner
            await invoke("kakarot", "transfer_ownership", other.address, account=other)
            new_owner = (await kakarot.functions["get_owner"].call()).owner
            assert prev_owner != other.address
            assert prev_owner == new_owner

        async def test_should_transfer_ownership(self, kakarot, other):
            prev_owner = (await kakarot.functions["get_owner"].call()).owner
            await invoke("kakarot", "transfer_ownership", other.address)
            new_owner = (await kakarot.functions["get_owner"].call()).owner

            assert prev_owner != new_owner
            assert new_owner == other.address

            await invoke("kakarot", "transfer_ownership", prev_owner, account=other)

    class TestAssertViewCall:
        @pytest.mark.parametrize("entrypoint", ["eth_call", "eth_estimate_gas"])
        async def test_should_raise_when_tx_view_entrypoint(self, kakarot, entrypoint):
            evm_account = await get_eoa()
            calldata = bytes.fromhex("6001")
            tx_hash = await invoke(
                "kakarot",
                entrypoint,
                0,  # nonce
                int(evm_account.signer.public_key.to_address(), 16),  # origin
                {"is_some": False, "value": 0},  # to
                10,  # gas_limit
                10,  # gas_price
                10,  # value
                list(calldata),  # data
                {},  # access_list
            )
            receipt = await RPC_CLIENT.get_transaction_receipt(tx_hash)
            assert receipt.execution_status.name == "REVERTED"
            assert "Only view call" in receipt.revert_reason

    class TestEthRPCEntrypoints:
        async def test_should_return_native_balance_of(self, new_eoa):
            eoa = await new_eoa(0x1234 / 1e18)
            balance = (
                await call("kakarot", "eth_get_balance", int(eoa.address, 16))
            ).balance
            assert balance == 0x1234

        async def test_should_return_transaction_count(self, new_eoa):
            eoa = await new_eoa(1)
            tx_count = (
                await call("kakarot", "eth_get_transaction_count", int(eoa.address, 16))
            ).tx_count
            assert tx_count == 0

            weth9 = await get_solidity_contract(
                "WETH",
                "WETH9",
                address=get_deployments()["WETH9"]["address"],
            )
            await weth9.functions["deposit()"](
                caller_eoa=eoa.starknet_contract, value=1
            )

            tx_count = (
                await call("kakarot", "eth_get_transaction_count", int(eoa.address, 16))
            ).tx_count
            assert tx_count == 1

        async def test_should_return_chain_id(self):
            chain_id = (await call("kakarot", "eth_chain_id")).chain_id
            assert chain_id == NETWORK["chain_id"].chain_id
