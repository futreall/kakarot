import os

import pytest
from eth_abi import decode
from eth_utils import keccak
from web3 import Web3

from kakarot_scripts.utils.kakarot import (
    eth_balance_of,
    eth_get_code,
    eth_get_transaction_count,
    eth_send_transaction,
    fund_address,
    get_contract,
)
from tests.utils.errors import evm_error


@pytest.mark.asyncio(scope="package")
@pytest.mark.PlainOpcodes
class TestPlainOpcodes:
    class TestStaticCall:
        async def test_should_return_counter_count(self, counter, plain_opcodes):
            assert await plain_opcodes.opcodeStaticCall() == await counter.count()

        async def test_should_revert_when_trying_to_modify_state(self, plain_opcodes):
            success, error = await plain_opcodes.opcodeStaticCall2()
            assert not success
            assert error == b""

    class TestCall:
        async def test_should_increase_counter(self, counter, plain_opcodes):
            count_before = await counter.count()
            await plain_opcodes.opcodeCall()
            count_after = await counter.count()
            assert count_after - count_before == 1

    class TestTimestamp:
        async def test_should_return_starknet_timestamp(
            self, plain_opcodes, block_timestamp
        ):
            assert pytest.approx(
                await plain_opcodes.opcodeTimestamp(), abs=20
            ) == await block_timestamp("pending")

    class TestBlockhash:
        @pytest.mark.xfail(reason="Need to fix blockhash on real Starknet network")
        async def test_should_return_blockhash_with_valid_block_number(
            self, plain_opcodes, block_number, block_hash
        ):
            blockhash = await plain_opcodes.opcodeBlockHash(
                await block_number("latest")
            )

            assert int.from_bytes(blockhash, byteorder="big") == await block_hash()

        async def test_should_return_zero_with_invalid_block_number(
            self, plain_opcodes, block_number
        ):
            blockhash_invalid_number = await plain_opcodes.opcodeBlockHash(
                await block_number("latest") + 10
            )

            assert int.from_bytes(blockhash_invalid_number, byteorder="big") == 0

        async def test_should_return_zero_for_last_10_blocks(
            self, plain_opcodes, block_number
        ):
            last_10_block_hashes = [
                await plain_opcodes.opcodeBlockHash(await block_number("latest") - i)
                for i in range(10)
            ]
            # assert all blockhashes are zero
            assert all(
                int.from_bytes(blockhash, byteorder="big") == 0
                for blockhash in last_10_block_hashes
            )

    class TestAddress:
        async def test_should_return_self_address(self, plain_opcodes):
            address = await plain_opcodes.opcodeAddress()

            assert int(plain_opcodes.address, 16) == int(address, 16)

    class TestExtCodeCopy:
        @pytest.mark.parametrize("offset, size", [[0, 32], [32, 32], [0, None]])
        async def test_should_return_counter_code(
            self, plain_opcodes, counter, offset, size
        ):
            """
            The counter.bytecode is indeed the structured as follows.

                constructor bytecode      contract bytecode       calldata
            |------------------------FE|----------------------|---------------|

            When deploying a contract, the constructor bytecode is run but not
            stored eventually,
            """
            deployed_bytecode = counter.bytecode[counter.bytecode.index(0xFE) + 1 :]
            size = len(deployed_bytecode) if size is None else size
            bytecode = await plain_opcodes.opcodeExtCodeCopy(offset=offset, size=size)
            assert bytecode == deployed_bytecode[offset : offset + size]

    class TestLog:
        @pytest.fixture
        def event(self):
            return {
                "owner": Web3.to_checksum_address(f"{10:040x}"),
                "spender": Web3.to_checksum_address(f"{11:040x}"),
                "value": 10,
            }

        async def test_should_emit_log0_with_no_data(self, plain_opcodes, owner):
            receipt = (
                await plain_opcodes.opcodeLog0(caller_eoa=owner.starknet_contract)
            )["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert events["Log0()"] == [{}]

        async def test_should_emit_log0_with_data(self, plain_opcodes, owner, event):
            receipt = (
                await plain_opcodes.opcodeLog0Value(caller_eoa=owner.starknet_contract)
            )["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert events["Log0Value(uint256)"] == [{"value": event["value"]}]

        async def test_should_emit_log1(self, plain_opcodes, owner, event):
            receipt = (
                await plain_opcodes.opcodeLog1(caller_eoa=owner.starknet_contract)
            )["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert events["Log1(uint256)"] == [{"value": event["value"]}]

        async def test_should_emit_log2(self, plain_opcodes, owner, event):
            receipt = (
                await plain_opcodes.opcodeLog2(caller_eoa=owner.starknet_contract)
            )["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            del event["spender"]
            assert events["Log2(address,uint256)"] == [event]

        async def test_should_emit_log3(self, plain_opcodes, owner, event):
            receipt = (
                await plain_opcodes.opcodeLog3(caller_eoa=owner.starknet_contract)
            )["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert events["Log3(address,address,uint256)"] == [event]

        async def test_should_emit_log4(self, plain_opcodes, owner, event):
            receipt = (
                await plain_opcodes.opcodeLog4(caller_eoa=owner.starknet_contract)
            )["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert events["Log4(address,address,uint256)"] == [event]

    class TestCreate:
        @pytest.mark.parametrize("count", [1, 2])
        async def test_should_create_counters(
            self, plain_opcodes, counter, owner, count
        ):
            nonce_initial = await eth_get_transaction_count(plain_opcodes.address)

            receipt = (
                await plain_opcodes.create(
                    bytecode=counter.constructor().data_in_transaction,
                    count=count,
                    caller_eoa=owner.starknet_contract,
                )
            )["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert len(events["CreateAddress(address)"]) == count
            for create_event in events["CreateAddress(address)"]:
                deployed_counter = await get_contract(
                    "PlainOpcodes", "Counter", address=create_event["_address"]
                )
                assert await deployed_counter.count() == 0

            nonce_final = await eth_get_transaction_count(plain_opcodes.address)
            assert nonce_final == nonce_initial + count

        @pytest.mark.parametrize("bytecode", ["0x", "0x6000600155600160015500"])
        async def test_should_create_empty_contract_when_creation_code_has_no_return(
            self, plain_opcodes, owner, bytecode
        ):
            receipt = (
                await plain_opcodes.create(
                    bytecode=bytecode,
                    count=1,
                    caller_eoa=owner.starknet_contract,
                )
            )["receipt"]

            events = plain_opcodes.events.parse_events(receipt)
            assert len(events["CreateAddress(address)"]) == 1
            assert b"" == await eth_get_code(
                events["CreateAddress(address)"][0]["_address"]
            )

        async def test_should_create_counter_and_call_in_the_same_tx(
            self, plain_opcodes
        ):
            receipt = (await plain_opcodes.createCounterAndCall())["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            address = events["CreateAddress(address)"][0]["_address"]
            counter = await get_contract("PlainOpcodes", "Counter", address=address)
            assert await counter.count() == 0

        async def test_should_create_counter_and_invoke_in_the_same_tx(
            self, plain_opcodes
        ):
            receipt = (await plain_opcodes.createCounterAndInvoke())["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            address = events["CreateAddress(address)"][0]["_address"]
            counter = await get_contract("PlainOpcodes", "Counter", address=address)
            assert await counter.count() == 1

    class TestCreate2:
        async def test_should_collision_after_selfdestruct_different_tx(
            self, plain_opcodes, owner
        ):
            contract_with_selfdestruct = await get_contract(
                "PlainOpcodes", "ContractWithSelfdestructMethod"
            )
            salt = 12345
            receipt = (
                await plain_opcodes.create2(
                    bytecode=contract_with_selfdestruct.constructor().data_in_transaction,
                    salt=salt,
                    caller_eoa=owner.starknet_contract,
                )
            )["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert len(events["Create2Address(address)"]) == 1
            contract_with_selfdestruct = await get_contract(
                "PlainOpcodes",
                "ContractWithSelfdestructMethod",
                address=events["Create2Address(address)"][0]["_address"],
            )
            pre_code = await eth_get_code(contract_with_selfdestruct.address)
            assert pre_code
            await contract_with_selfdestruct.kill()
            post_code = await eth_get_code(contract_with_selfdestruct.address)
            assert pre_code == post_code

            receipt = (
                await plain_opcodes.create2(
                    bytecode=contract_with_selfdestruct.constructor().data_in_transaction,
                    salt=salt,
                    caller_eoa=owner.starknet_contract,
                )
            )["receipt"]

            events = plain_opcodes.events.parse_events(receipt)

            # There should be a create2 collision which returns zero
            assert events["Create2Address(address)"] == [
                {"_address": "0x0000000000000000000000000000000000000000"}
            ]

        async def test_should_deploy_bytecode_at_address(
            self, plain_opcodes, counter, owner
        ):
            nonce_initial = await eth_get_transaction_count(plain_opcodes.address)

            salt = 1234
            receipt = (
                await plain_opcodes.create2(
                    bytecode=counter.constructor().data_in_transaction,
                    salt=salt,
                    caller_eoa=owner.starknet_contract,
                )
            )["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert len(events["Create2Address(address)"]) == 1

            deployed_counter = await get_contract(
                "PlainOpcodes",
                "Counter",
                address=events["Create2Address(address)"][0]["_address"],
            )
            assert await deployed_counter.count() == 0
            assert await eth_get_transaction_count(deployed_counter.address) == 1
            assert (
                await eth_get_transaction_count(plain_opcodes.address)
                == nonce_initial + 1
            )

    class TestRequire:
        async def test_should_revert_when_value_is_zero(self, plain_opcodes):
            with evm_error("ZERO_VALUE"):
                await plain_opcodes.requireNotZero(0)

        @pytest.mark.parametrize("value", [2**127, 2**128])
        async def test_should_not_revert_when_value_is_not_zero(
            self, plain_opcodes, value
        ):
            await plain_opcodes.requireNotZero(value)

    class TestExceptionHandling:
        async def test_calling_context_should_propagate_revert_from_sub_context_on_create(
            self, plain_opcodes, owner
        ):
            with evm_error("FAIL"):
                await plain_opcodes.newContractConstructorRevert(
                    caller_eoa=owner.starknet_contract
                )

        async def test_should_revert_via_call(self, plain_opcodes, owner):
            receipt = (
                await plain_opcodes.contractCallRevert(
                    caller_eoa=owner.starknet_contract
                )
            )["receipt"]

            reverting_contract = await get_contract(
                "PlainOpcodes", "ContractRevertsOnMethodCall"
            )

            assert reverting_contract.events.parse_events(receipt) == {
                "PartyTime(bool)": []
            }

    class TestOriginAndSender:
        async def test_should_return_owner_as_origin_and_sender(
            self, plain_opcodes, owner
        ):
            origin, sender = await plain_opcodes.originAndSender(
                caller_eoa=owner.starknet_contract
            )
            assert origin == sender == owner.address

        async def test_should_return_owner_as_origin_and_caller_as_sender(
            self, plain_opcodes, owner, caller
        ):
            receipt = (
                await caller.call(
                    target=plain_opcodes.address,
                    payload=plain_opcodes.encodeABI("originAndSender"),
                    caller_eoa=owner.starknet_contract,
                )
            )["receipt"]
            events = caller.events.parse_events(receipt)
            assert len(events["Call(bool,bytes)"]) == 1
            assert events["Call(bool,bytes)"][0]["success"]
            decoded = decode(
                ["address", "address"], events["Call(bool,bytes)"][0]["returnData"]
            )
            assert int(owner.address, 16) == int(decoded[0], 16)  # tx.origin
            assert int(caller.address, 16) == int(decoded[1], 16)  # msg.sender

    class TestLoop:
        @pytest.mark.parametrize("steps", [0, 1, 2, 10])
        async def test_loop_should_write_to_storage(self, plain_opcodes, steps):
            value = await plain_opcodes.loop(steps)
            assert value == steps

    class TestTransfer:
        async def test_send_some_should_send_to_eoa(self, plain_opcodes, owner, other):
            amount = 1
            await fund_address(plain_opcodes.address, amount)

            receiver_balance_before = await eth_balance_of(other.address)
            sender_balance_before = await eth_balance_of(plain_opcodes.address)

            await plain_opcodes.sendSome(
                other.address, amount, caller_eoa=owner.starknet_contract
            )

            receiver_balance_after = await eth_balance_of(other.address)
            sender_balance_after = await eth_balance_of(plain_opcodes.address)

            assert receiver_balance_after - receiver_balance_before == amount
            assert sender_balance_before - sender_balance_after == amount

        async def test_send_some_should_revert_when_amount_exceed_balance(
            self, plain_opcodes, owner, other
        ):
            amount = 1
            await fund_address(plain_opcodes.address, amount)

            sender_balance_before = await eth_balance_of(plain_opcodes.address)
            receipt = (
                await plain_opcodes.sendSome(
                    other.address,
                    sender_balance_before + 1,
                    caller_eoa=owner.starknet_contract,
                )
            )["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert events["SentSome(address,uint256,bool)"] == [
                {
                    "to": other.address,
                    "amount": sender_balance_before + 1,
                    "success": False,
                }
            ]
            assert sender_balance_before == await eth_balance_of(plain_opcodes.address)

    class TestMapping:
        async def test_should_emit_event_and_increase_nonce(self, plain_opcodes):
            receipt = (await plain_opcodes.incrementMapping())["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            prev_nonce = events["NonceIncreased(uint256)"][0]["nonce"]
            receipt = (await plain_opcodes.incrementMapping())["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert events["NonceIncreased(uint256)"][0]["nonce"] - prev_nonce == 1

    class TestFallbackFunctions:
        @pytest.mark.parametrize(
            "data,value,message", (("", 1234, "receive"), ("0x00", 0, "fallback"))
        )
        async def test_should_revert_on_fallbacks(
            self, revert_on_fallbacks, data, value, message, other
        ):
            receipt, response, success, gas_used = await eth_send_transaction(
                to=revert_on_fallbacks.address,
                gas=200_000,
                data=data,
                value=value,
                caller_eoa=other.starknet_contract,
            )
            assert not success
            assert (
                f"reverted on {message}".encode() in bytes(response)
                if response
                else True
            )

    class TestMulmod:
        async def test_should_return_0(self, plain_opcodes):
            assert 0 == await plain_opcodes.mulmodMax()

    class TestAddmod:
        async def test_should_return_0(self, plain_opcodes):
            assert 0 == await plain_opcodes.addmodMax()

    class TestKeccak:
        @pytest.mark.parametrize(
            "input_length",
            [
                20000,
                pytest.param(
                    272000, marks=pytest.mark.xfail(reason="input length too big")
                ),
            ],
        )
        async def test_should_emit_keccak_hash(self, plain_opcodes, input_length):
            input_bytes = os.urandom(input_length)
            receipt = (await plain_opcodes.computeHash(input_bytes))["receipt"]
            events = plain_opcodes.events.parse_events(receipt)
            assert events["HashComputed(address,bytes32)"][0]["hash"] == keccak(
                input_bytes
            )
