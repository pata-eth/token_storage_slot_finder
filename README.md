# Token Storage Slot Finder

Python module that searches for a token's balance and allowance storage slots.
These are handy when running simulations, where storage overrides can be
provided.

# Algo Summary

1. It's always preferred to search using an account with a non-zero balance.
   This is because certain tokens depend on other state variables besides the
   balance mapping to return a non-zero balance. 
2. I iterate until storage slot is found.
3. The storage key depends on the compiler so I attempt to figure out the
   compiler from metadata in the bytecode. If the compiler cannot be determined
   from the bytecode, I attempt solidity's and vyper's storage keys to find the
   storage slots.
4. For certain non-standard proxy implementations (e.g., synthetix tokens), I
   traverse implementations in an attempt to find the contract where
   balances/allowances are stored. When the implementation address is not
   exposed (i.e., they are private variables), the storage slot won't be found.
   One could manually determine the implemention address, and then search for
   the slot. I was fine without storage info for these tokens.

# How to run it

Save `.env_example` as `.env` and update the token url. Disregard if your token
source info is different.

Needs a ganache fork running before `main.py` is run:

```
# written with ganache >=7.03
ganache-cli --chain.vmErrorsOnRPCResponse true --wallet.totalAccounts 0 \
--hardfork istanbul \
--fork.url <your mainnet rpc url> \
--server.port 8545 --chain.chainId 1
```

Run `main.py` with:

```
export PYTHONPATH=$(pwd); python main.py
```
