import asyncio
import time
import os
import json
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
from src.transfer_from_sim import TransferFromSim
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


async def main(skip_search=False, force_sim=False):
    t1 = t0 = time.time()
    tokens_url = os.getenv("TOKEN_LIST_URL")
    token_holders_url = os.getenv("TOKEN_HOLDERS_URL")

    if skip_search:
        tokens_list = []
    else:
        tokens_raw = get(tokens_url).json()
        token_holders = get(token_holders_url).json()
        tokens_list = list(tokens_raw.keys())

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

    # tokens_list = ["0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32"]

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

        balance_updated = await asyncio.gather(*balance_coroutines)
        allowance_updated = await asyncio.gather(*allowance_coroutines)

        # Add symbol information to the db befor calling archive. `db` is shared
        # with BalanceStorage and AllowanceStorage through the parent class
        # TokenStorageBase
        for key in TokenStorageBase.db:
            if "symbol" in TokenStorageBase.db[key]:
                continue
            TokenStorageBase.db[key]["symbol"] = tokens_raw[key]["symbol"]

        # Archive shared db
        if any(balance_updated) or any(allowance_updated):
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

    missing = set(missing_balance) | set(missing_allowance)

    missing_str = "\n" + "\n".join(missing)

    logger.warning(f"Tokens missing a slot: {missing_str}")

    coverage_pct = (
        (len(TokenStorageBase.db) - len(missing))
        / len(TokenStorageBase.db)
        * 100
    )

    logger.info(
        f"Token coverage: {len(TokenStorageBase.db) - len(missing)} "
        f"out of {len(TokenStorageBase.db)} "
        f"({format(coverage_pct,'.2f')}%)"
    )

    # Validate that transferFrom can be succesfully simulated with the given
    # overrides.
    with open(TokenStorageBase.db_file_path, "r", encoding="utf-8") as file:
        db = json.load(file)

    tokens = list(db.keys())
    token_chunks = chunks(tokens, 30)

    owner = "0xcdb90E5A0D06F35a71e96467e46aE1822510071E"
    recipient = "0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b"
    amount = 10**18

    ti = time.time()

    for i, chunk in enumerate(token_chunks):
        coroutines = []
        for token in chunk:
            if token in SKIPS:
                continue

            if (
                force_sim
                or "complex" not in db[token]
                or ("complex" in db[token] and db[token]["complex"])
            ):
                coroutines.append(
                    TransferFromSim(token, owner, recipient, amount).simulate()
                )

        results = await asyncio.gather(*coroutines)
        results = {t: d for r in results for t, d in r.items()}

        for t, d in db.items():
            if t in results:
                d["complex"] = results[t]["complex"]

    t1 = time.time()

    logger.info(f"transferFrom() sim took {t1-ti} secs")

    # Log proportion of complex tokens
    complex_tokens = []
    for token, data in db.items():
        if data["complex"]:
            complex_tokens.append(token)

    complex_pct = (len(complex_tokens)) / len(db) * 100

    logger.info(
        f"Complex tokens: {len(complex_tokens)} "
        f"out of {len(TokenStorageBase.db)} "
        f"({format(complex_pct,'.2f')}%)"
    )

    with open(TokenStorageBase.db_file_path, "w", encoding="utf-8") as file:
        file.write(json.dumps(db, indent=4))


if __name__ == "__main__":
    asyncio.run(main())
