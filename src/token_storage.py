import logging
import json
import os
from json import JSONDecodeError
from enum import Enum
from web3 import AsyncWeb3, AsyncHTTPProvider, Web3
from web3.contract import Contract
from web3.method import Method
from eth_abi import encode
from hexbytes import HexBytes
from collections import Counter

node_url = os.getenv("RPC_URL_FORK")
w3 = AsyncWeb3(
    AsyncHTTPProvider(node_url, request_kwargs={"timeout": 60 * 60 * 2})
)

# This is a custom RPC method available in a forked chain in ganache
w3.eth.attach_methods({"set_storage_at": Method("evm_setAccountStorageAt")})


class EvmLang(Enum):
    SOLIDITY = "solidity"
    VYPER = "vyper"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        """Represent as string."""
        return self.value


class TokenStorageVariable(Enum):
    BALANCE = "balance"
    ALLOWANCE = "allowance"

    def __str__(self) -> str:
        """Represent as string."""
        return self.value


with open("./abis/erc20.json", "r", encoding="utf-8") as file:
    ABI = json.load(file)

# Prefixes are used to identify compilers
PREFIXES = {
    "6004361015": EvmLang.VYPER,
    "341561000a": EvmLang.VYPER,
    "6060604052": EvmLang.SOLIDITY,
    "6080604052": EvmLang.SOLIDITY,
}

MAX_SLOT = 310  # max number of slots that we'll try
SKIPS = ["0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"]


class TokenStorageBase:
    logger = logging.getLogger(__name__)
    db_file_path = "db/storage_finder_db.json"
    try:
        with open(db_file_path, "r", encoding="utf-8") as file:
            db = json.load(file)
    except (FileNotFoundError, JSONDecodeError):
        db = {}

    def __init__(self, token_address: str):
        self.token: Contract = w3.eth.contract(address=token_address, abi=ABI)
        if token_address not in self.db:
            self.db[token_address] = {}
            self.db[token_address]["contract"] = self.token

    @staticmethod
    def _get_search_range(db, var: TokenStorageVariable):
        """
        Returns list of slots used by tokens in the db sorted by frequency
        """
        common_slots = [
            d[var.value]["slot"]
            for _, d in db.items()
            if var.value in d and d[var.value]["slot"] is not None
        ]
        common_slots = [
            slot
            for slots, c in Counter(common_slots).most_common()
            for slot in [slots] * c
        ]
        unique_slots = []
        for i in common_slots:
            if i not in unique_slots:
                unique_slots.append(i)

        find_range = set(range(MAX_SLOT))
        remaining_slots = find_range.difference(set(unique_slots))
        return unique_slots + list(remaining_slots)

    @classmethod
    def archive(cls):
        db = cls.db.copy()
        for _, d in db.items():
            # do not archive web3 contract instances
            if "contract" in d:
                del d["contract"]

        with open(cls.db_file_path, "w", encoding="utf-8") as file:
            file.write(json.dumps(db, indent=4))
        cls.logger.info("db was archived")

    async def _bytecode(self):
        try:
            self.token.bytecode = await w3.eth.get_code(self.token.address)
            self.logger.debug(f"bytecode fetched for {self.token.address}")
        except Exception as error:
            self.logger.error(f"{self.token.address} {error=}")

    async def _detect_compiler(self) -> EvmLang:
        if (
            "compiler" in self.db[self.token.address]
            and self.db[self.token.address]["compiler"] is not None
        ):
            return EvmLang(self.db[self.token.address]["compiler"])
        bytecode = await self._bytecode()
        if bytecode is None:
            return EvmLang.UNKNOWN
        if bytecode[-53:-51] == b"\xa2\x64":
            return EvmLang.SOLIDITY
        elif bytecode[-13:-11] == b"\xa1\x65":
            return EvmLang.VYPER

        for prefix in PREFIXES:
            if bytecode.startswith(bytes.fromhex(prefix)):
                return PREFIXES[prefix]

        return EvmLang.UNKNOWN

    async def _check(self, storage_address, slot, compiler, function_name):
        # to be overriden by each class corresponding to a state variable
        pass

    async def _find(self, storage_address, compiler, function_name) -> int:
        for slot in self.search_range:
            try:
                if await self._check(
                    storage_address, slot, compiler, function_name
                ):
                    return slot
            except ValueError as error:
                if error.args[0]["name"] == "CallError":
                    self.logger.debug(
                        f"{function_name} {self.token.address} {error=}"
                    )
                    break
            except Exception as error:
                self.logger.debug(f"{self.token.address} {error=}")
                continue

    async def _find_iterate(self, storage_address, compiler, function_name):
        if compiler == EvmLang.UNKNOWN:
            compiler = EvmLang.SOLIDITY
            slot = await self._find(storage_address, compiler, function_name)
            if slot is None:
                compiler = EvmLang.VYPER
                slot = await self._find(
                    storage_address,
                    EvmLang.VYPER,
                    function_name,
                )
        else:
            slot = await self._find(storage_address, compiler, function_name)
        return slot, compiler

    @staticmethod
    def _mapping_key(
        slot: bytes,
        account: str,
        compiler: EvmLang,
    ) -> HexBytes:
        if compiler == EvmLang.SOLIDITY:
            return Web3.keccak(encode(["address"], [account]) + slot)
        elif compiler == EvmLang.VYPER:
            return Web3.keccak(slot + encode(["address"], [account]))

    @staticmethod
    async def _set_storage(
        contract: str,
        storage_key: str,
        storage_value: str,
    ) -> bool:
        return await w3.eth.set_storage_at(
            contract, storage_key, storage_value
        )

    @staticmethod
    async def _get_storage(
        contract: str,
        storage_key: str,
    ) -> HexBytes:
        return await w3.eth.get_storage_at(contract, storage_key)


class BalanceStorage(TokenStorageBase):
    def __init__(self, contract_address: str, owner: str):
        super().__init__(contract_address)
        assert Web3.is_checksum_address(owner)
        self.owner = owner
        self.search_range = self._get_search_range(
            self.db, TokenStorageVariable.BALANCE
        )

    async def _check(
        self, storage_address, slot, compiler, function_name
    ) -> bool:
        balance_function = self.token.get_function_by_name(function_name)
        starting_balance = await balance_function(self.owner).call()
        probing_balance = starting_balance + 1000 * 10**18
        encoded_probing_balance = (
            "0x" + encode(["uint"], [probing_balance]).hex()
        )
        storage_key = self._mapping_key(
            encode(["uint"], [slot]), self.owner, compiler
        ).hex()

        success = await self._set_storage(
            storage_address,
            storage_key,
            encoded_probing_balance,
        )
        if not success:
            raise ValueError(
                f"Setting balance storage unsuccesful "
                f"for slot {slot} for compiler {compiler} for "
                f"contract {storage_address}"
            )
        updated_balance = await balance_function(self.owner).call()
        check = updated_balance > starting_balance
        self.logger.debug(
            f"{self.token.address} -> "
            f"starting balance {starting_balance} "
            f"probing balance {probing_balance} "
            f"updated balance {updated_balance}"
        )
        return check

    async def find(self) -> bool:
        if self.token.address in SKIPS or (
            self.token.address in self.db
            and "balance" in self.db[self.token.address]
            and self.db[self.token.address]["balance"]["slot"] is not None
        ):
            self.logger.debug(f"balance skipping {self.token.address}")
            return False

        self.db[self.token.address]["balance"] = {"target": None, "slot": None}

        original_compiler = await self._detect_compiler()

        # Find balance slots
        storage_address = self.token.address

        methods = ["balanceOf", "principalBalanceOf"]  # ERC20, AAVE
        for method in methods:
            slot, compiler = await self._find_iterate(
                storage_address, original_compiler, method
            )
            if slot is not None:
                break

        if slot is None:
            # attempt to find storage slots in non standard
            # proxy implementations
            non_standard_implementations = {
                "target": ["tokenState"],  # synthetix
                "balances": [],
                "erc20Impl": ["erc20Store"],  # GUSD
            }
            for target in non_standard_implementations:
                try:
                    function = self.token.get_function_by_name(target)
                    storage_address = await function().call()
                    assert Web3.is_address(storage_address)
                    break
                except Exception:
                    continue

            if storage_address != self.token.address:
                slot, compiler = await self._find_iterate(
                    storage_address, original_compiler, "balanceOf"
                )

        if slot is None and storage_address != self.token.address:
            implementation_contract = w3.eth.contract(
                address=storage_address, abi=ABI
            )
            for target in non_standard_implementations[target]:
                try:
                    function = implementation_contract.get_function_by_name(
                        target
                    )
                    storage_address = await function().call()
                    assert Web3.is_address(storage_address)
                    break
                except Exception:
                    continue

            if storage_address != implementation_contract.address:
                slot, compiler = await self._find_iterate(
                    storage_address, original_compiler, "balanceOf"
                )

        if slot is not None:
            self.db[self.token.address]["balance"]["slot"] = slot
            self.db[self.token.address]["balance"]["target"] = storage_address
            self.db[self.token.address]["compiler"] = str(compiler)
            self.logger.info(f"balance slot found for {self.token.address}")
            return True
        else:
            self.logger.warning(
                f"balance slot not found for token {self.token.address}"
            )
            return False


class AllowanceStorage(TokenStorageBase):
    def __init__(self, contract_address: str, owner: str, spender: str):
        super().__init__(contract_address)
        assert Web3.is_checksum_address(owner)
        self.owner = owner
        assert Web3.is_checksum_address(spender)
        self.spender = spender
        self.search_range = self._get_search_range(
            self.db, TokenStorageVariable.ALLOWANCE
        )

    async def _check(
        self, storage_address, slot, compiler, function_name
    ) -> bool:
        allowance_function = self.token.get_function_by_name(function_name)
        starting_allowance = await allowance_function(
            self.owner, self.spender
        ).call()
        probing_allowance = starting_allowance + 1000 * 10**18
        encoded_probing_balance = (
            "0x" + encode(["uint"], [probing_allowance]).hex()
        )
        storage_key_outer_mapping = self._mapping_key(
            encode(["uint"], [slot]), self.owner, compiler
        )
        storage_key_inner_mapping = self._mapping_key(
            storage_key_outer_mapping, self.spender, compiler
        ).hex()

        success = await self._set_storage(
            storage_address,
            storage_key_inner_mapping,
            encoded_probing_balance,
        )
        if not success:
            raise ValueError(
                f"Setting allowance storage unsuccesful "
                f" for slot {slot} for compiler {compiler} for "
                f"contract {storage_address}"
            )
        updated_allowance = await self.token.functions.allowance(
            self.owner, self.spender
        ).call()
        check = probing_allowance == updated_allowance

        self.logger.debug(
            f"{self.token.address} -> "
            f"starting allowance {starting_allowance} "
            f"probing allowance {probing_allowance} "
            f"updated allowance {updated_allowance}"
        )
        return check

    async def find(self):
        if self.token.address in SKIPS or (
            self.token.address in self.db
            and "allowance" in self.db[self.token.address]
            and self.db[self.token.address]["allowance"]["slot"] is not None
        ):
            self.logger.debug(f"allowance skipping {self.token.address}")
            return False

        self.db[self.token.address]["allowance"] = {
            "target": None,
            "slot": None,
        }

        original_compiler = await self._detect_compiler()

        # Find balance slots
        storage_address = self.token.address

        methods = ["allowance"]
        for method in methods:
            slot, compiler = await self._find_iterate(
                storage_address, original_compiler, method
            )
            if slot is not None:
                break

        if slot is None:
            # attempt to find storage slots in non standard
            # proxy implementations
            non_standard_implementations = {
                "target": ["tokenState"],  # synthetix
                "allowances": [],
                "erc20Impl": ["erc20Store"],  # GUSD
            }
            for target in non_standard_implementations:
                try:
                    function = self.token.get_function_by_name(target)
                    storage_address = await function().call()
                    assert Web3.is_address(storage_address)
                    break
                except Exception:
                    continue

            if storage_address != self.token.address:
                slot, compiler = await self._find_iterate(
                    storage_address, original_compiler, "allowance"
                )

        if slot is None and storage_address != self.token.address:
            implementation_contract = w3.eth.contract(
                address=storage_address, abi=ABI
            )
            for target in non_standard_implementations[target]:
                try:
                    function = implementation_contract.get_function_by_name(
                        target
                    )
                    storage_address = await function().call()
                    assert Web3.is_address(storage_address)
                    break
                except Exception:
                    continue

            if storage_address != implementation_contract.address:
                slot, compiler = await self._find_iterate(
                    storage_address, original_compiler, "allowance"
                )

        if slot is not None:
            self.db[self.token.address]["allowance"]["slot"] = slot
            self.db[self.token.address]["allowance"][
                "target"
            ] = storage_address
            self.db[self.token.address]["compiler"] = str(compiler)
            self.logger.info(f"allowance slot found for {self.token.address}")
            return True
        else:
            self.logger.warning(
                f"allowance slot not found for token {self.token.address}"
            )
            return False
