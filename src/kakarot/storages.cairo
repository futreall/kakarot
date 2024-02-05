// SPDX-License-Identifier: MIT

%lang starknet

@storage_var
func precompiles_class_hash() -> (res: felt) {
}

@storage_var
func native_token_address() -> (res: felt) {
}

@storage_var
func contract_account_class_hash() -> (value: felt) {
}

@storage_var
func externally_owned_account_class_hash() -> (res: felt) {
}

@storage_var
func account_proxy_class_hash() -> (res: felt) {
}

@storage_var
func evm_to_starknet_address(evm_address: felt) -> (starknet_address: felt) {
}

@storage_var
func coinbase() -> (res: felt) {
}

@storage_var
func base_fee() -> (res: felt) {
}
