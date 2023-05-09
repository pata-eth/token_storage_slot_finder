import logging
import json
from json import JSONDecodeError
from enum import Enum
from web3 import Web3
from eth_abi import encode
from hexbytes import HexBytes
from typing import Tuple

# 2^95-1 encoded
BALANCE_OVERRIDE = (
    "0x00000000000000000000000000000000000000007fffffffffffffffffffffff"
)


class EvmLang(Enum):
    SOLIDITY = "solidity"
    VYPER = "vyper"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        """Represent as string."""
        return self.value


class StorageType(Enum):
    BALANCE = "balance"
    ALLOWANCE = "allowance"

    def __str__(self) -> str:
        """Represent as string."""
        return self.value


class StorageOverrides:
    logger = logging.getLogger(__name__)
    db_file_path = "db/storage_finder_db.json"
    try:
        with open(db_file_path, "r", encoding="utf-8") as file:
            db = json.load(file)
    except (FileNotFoundError, JSONDecodeError):
        db = {}

    @classmethod
    async def get_storage_overrides(
        cls,
        token_address: str,
        type: StorageType,
        owner_address: str,
        spender_address: str = None,
        encoded_override_val: str = BALANCE_OVERRIDE,
    ) -> Tuple[str, dict]:
        if token_address not in cls.db:
            cls.logger.debug(f"{token_address} does not exist in db")
            return None, None
        token_data = cls.db[token_address]

        if token_data == {}:
            cls.logger.debug(f"{token_address} dictionary is empty")
            return None, None

        if "compiler" in token_data and token_data["compiler"] is not None:
            compiler = token_data["compiler"]
        else:
            cls.logger.debug(f"{token_address} compiler is missing")
            return None, None

        slot_data = token_data[type.value]

        target = slot_data["target"]
        slot = slot_data["slot"]

        if slot is None:
            cls.logger.debug(f"{token_address} {type.value} slot is missing")
            return None, None

        encoded_slot = encode(["uint"], [slot])

        if type == StorageType.BALANCE:
            storage_key = await cls.storage_key(
                encoded_slot, owner_address, EvmLang(compiler)
            )
        elif type == StorageType.ALLOWANCE:
            if spender_address is None:
                cls.logger.debug(
                    f"{token_address} {type.value} spender is missing"
                )
                return None, None
            storage_key = await cls.storage_key(
                encoded_slot, owner_address, EvmLang(compiler)
            )
            storage_key = await cls.storage_key(
                storage_key, spender_address, EvmLang(compiler)
            )

        return target, {storage_key.hex(): encoded_override_val}

    @staticmethod
    async def storage_key(
        slot: bytes,
        account: str,
        compiler: EvmLang,
    ) -> HexBytes:
        if compiler == EvmLang.SOLIDITY:
            return Web3.keccak(encode(["address"], [account]) + slot)
        elif compiler == EvmLang.VYPER:
            return Web3.keccak(slot + encode(["address"], [account]))
