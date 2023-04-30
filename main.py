import asyncio
import time
import os
import logging
from web3 import Web3
from src.token_storage import (
    TokenStorageBase,
    BalanceStorage,
    AllowanceStorage,
    SKIPS,
    ABI,
    w3,
)
from requests import get
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logging.getLogger("web3").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def chunks(ll, n):
    for i in range(0, len(ll), n):
        yield ll[i : i + n]


async def main():
    t1 = t0 = time.time()
    tokens_url = os.getenv("TOKEN_LIST_URL")
    token_holders_url = os.getenv("TOKEN_HOLDERS_URL")
    tokens_raw = get(tokens_url).json()
    token_holders = get(token_holders_url).json()

    # `tokens_raw` is a dictionary. For example:
    # {
    #     "0xa41F142b6eb2b164f8164CAE0716892Ce02f311f": {
    #         "name": "Avocado DAO Token",
    #         "symbol": "AVG",
    #         "decimals": 18,
    #     },
    #     "0xC82E3dB60A52CF7529253b4eC688f631aad9e7c2": {
    #         "name": "ARC",
    #         "symbol": "ARC",
    #         "decimals": 18,
    #     },
    # }

    tokens_list = list(tokens_raw.keys())
    # tokens_list = ["0xC011a73ee8576Fb46F5E1c5751cA3B9Fe0af2a6F"]

    tokens = [t for t in tokens_list if t.startswith("0x")]

    token_chunks = chunks(tokens, 30)

    logger.info(
        f"Searching balance and allowance storage slots "
        f"for {len(tokens)} tokens"
    )

    spender = "0x7C8E77390e999DA2f826305844078B88DC39aB82"  # rando addy

    for i, chunk in enumerate(token_chunks):
        ti = time.time()

        balance_coroutines = []
        allowance_coroutines = []
        for token in chunk:
            if token in SKIPS:
                continue

            if token in token_holders:
                for owner in token_holders[token]:
                    owner = Web3.to_checksum_address(owner)
                    contract = w3.eth.contract(address=token, abi=ABI)
                    bal = await contract.functions.balanceOf(owner).call()
                    if bal > 0:
                        break

                if bal == 0:
                    logger.warning(f"No holder with balance for {token}")
            else:
                # Use default account
                owner = "0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b"

            balance_coroutines.append(BalanceStorage(token, owner).find())
            allowance_coroutines.append(
                AllowanceStorage(token, owner, spender).find()
            )

        await asyncio.gather(*balance_coroutines)
        await asyncio.gather(*allowance_coroutines)

        # Add symbol information to the db befor calling archive. `db` is shared
        # with BalanceStorage and AllowanceStorage through the parent class
        # TokenStorageBase
        for key in TokenStorageBase.db:
            TokenStorageBase.db[key]["symbol"] = tokens_raw[key]["symbol"]

        # Archive shared db
        TokenStorageBase.archive()

        t1 = time.time()

        logger.info(f"Chunk {i} took {t1-ti} secs")

    logger.info(f"All time ellapsed {t1-t0} secs")

    # Log tokens with missing data
    missing_balance = []
    missing_allowance = []
    for token, data in TokenStorageBase.db.items():
        if data["balance"]["slot"] is None:
            missing_balance.append(token)

        if data["allowance"]["slot"] is None:
            missing_allowance.append(token)

    logger.warning(
        f"Tokens missing a balance slot: {', '.join(missing_balance)}"
    )

    logger.warning(
        f"Tokens missing an allowance slot: {', '.join(missing_allowance)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
