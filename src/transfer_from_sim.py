import logging
import json
from web3 import AsyncWeb3, AsyncHTTPProvider
from web3.contract import Contract
from web3.exceptions import BadFunctionCallOutput
from src.storage_overrides import StorageOverrides, StorageType

node_url = "http://127.0.0.1:8545"  # rpc url
w3 = AsyncWeb3(
    AsyncHTTPProvider(node_url, request_kwargs={"timeout": 60 * 60 * 2})
)


with open("./abis/erc20.json", "r", encoding="utf-8") as file:
    ABI = json.load(file)


class TransferFromSim:
    logger = logging.getLogger(__name__)

    def __init__(
        self,
        token_address: str,
        from_address: str,
        to_address: str,
        amount: int,
    ):
        self.token_address = token_address
        self.from_address = from_address
        self.to_address = to_address  # spender is recipient and msg.sender
        self.amount = amount

        self.token_contract: Contract = w3.eth.contract(
            address=token_address, abi=ABI
        )

    async def get_overrides(self) -> dict:
        (
            balance_target_contract,
            balance_override,
        ) = await StorageOverrides.get_storage_overrides(
            self.token_address,
            StorageType.BALANCE,
            owner_address=self.from_address,
        )

        (
            allowance_target_contract,
            allowance_override,
        ) = await StorageOverrides.get_storage_overrides(
            self.token_address,
            StorageType.ALLOWANCE,
            owner_address=self.from_address,
            spender_address=self.to_address,
        )

        if (
            allowance_target_contract is None
            or balance_target_contract is None
        ):
            return {}

        if allowance_target_contract == balance_target_contract:
            overrides = {
                balance_target_contract: {
                    "stateDiff": balance_override | allowance_override
                }
            }
        else:
            overrides = {
                balance_target_contract: {"stateDiff": balance_override},
                allowance_target_contract: {"stateDiff": allowance_override},
            }

        return overrides

    async def simulate(self) -> dict:
        overrides = await self.get_overrides()

        if overrides == {}:
            # I deem a token complex if the transferFrom fails with correctly
            # set overrides. An example is LDO, where the transfer depends on
            # other state variables for which we have not provided an override
            # other than the balance and the allowance.
            return {self.token_address: {"complex": True}}

        try:
            result = await self.token_contract.functions.transferFrom(
                self.from_address, self.to_address, self.amount
            ).call({"from": self.to_address}, state_override=overrides)
            output = {"complex": not result}
        except BadFunctionCallOutput:
            self.logger.debug(
                f"{self.token_address}->BadFunctionCallOutputError: Could not "
                f"decode contract function call to transferFrom"
            )
            output = {"complex": True}
        except Exception as error:
            self.logger.debug(
                f"{self.token_address}->{error.args[0]['message']}"
            )
            output = {"complex": True}

        return {self.token_address: output}
