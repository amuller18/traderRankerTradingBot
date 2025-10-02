import base64
import json
import time
import requests
from typing import Any, Dict, Optional, Union, Tuple

from solana.rpc.api import Client
from solana.rpc.commitment import Processed
from solana.rpc.types import TxOpts
from solana.transaction import Signature
from solders.keypair import Keypair  # type: ignore
from solders.message import to_bytes_versioned  # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore
import base58
from solders.rpc.errors import SendTransactionPreflightFailureMessage  # type: ignore
from solana.rpc.commitment import Confirmed, Finalized, Processed
import time
from solders.transaction_status import (
    TransactionConfirmationStatus as TCS
)

class JupiterSwap:
    """
    JupiterSwap class for handling Solana token swaps via Jupiter aggregator.
    """

    # Constant for SOL token address
    SOL = "So11111111111111111111111111111111111111112"

    def __init__(self, private_key: str, rpc_endpoint: str):
        """
        Initialize JupiterSwap with private key and RPC endpoint.

        Args:
            private_key (str): Base58 encoded private key
            rpc_endpoint (str): Solana RPC endpoint URL
        """
        self.rpc = rpc_endpoint
        self.client = Client(self.rpc)
        self.keypair = Keypair.from_base58_string(private_key)

        private_key_bytes = base58.b58decode(private_key)
        keypair = Keypair.from_bytes(private_key_bytes)
        self.pubkey = keypair.pubkey()

    def _find_data(self, data: Union[dict, list], field: str) -> Optional[str]:
        """
        Recursively search for a field in nested data structures.

        Args:
            data (Union[dict, list]): Data structure to search
            field (str): Field name to find

        Returns:
            Optional[str]: Field value if found, None otherwise
        """
        if isinstance(data, dict):
            if field in data:
                return data[field]
            else:
                for value in data.values():
                    result = self._find_data(value, field)
                    if result is not None:
                        return result
        elif isinstance(data, list):
            for item in data:
                result = self._find_data(item, field)
                if result is not None:
                    return result
        return None

    def get_token_balance_lamports(self, mint_str: str) -> Optional[int]:
        """
        Get token balance in lamports for the specified mint.

        Args:
            mint_str (str): Token mint address

        Returns:
            Optional[int]: Token balance in lamports if successful, None otherwise
        """
        try:
            headers = {"accept": "application/json", "content-type": "application/json"}
            payload = {
                "id": 1,
                "jsonrpc": "2.0",
                "method": "getTokenAccountsByOwner",
                "params": [
                    str(self.pubkey),
                    {"mint": mint_str},
                    {"encoding": "jsonParsed"}
                ],
            }

            response = requests.post(self.rpc, json=payload, headers=headers)
            ui_amount = self._find_data(response.json(), "amount")
            return int(ui_amount) if ui_amount else 0
        except Exception as e:
            print(f"Error getting token balance: {e}")
            return None

    def confirm_txn(
        self,
        txn_sig: str,
        commitment: str = "confirmed",      # "processed" | "confirmed" | "finalized"
        retry_interval: float = 1.0,
        max_retries: int = 30,
    ) -> bool:
        """
        Poll get_signature_statuses until the signature reaches the requested
        commitment level.  Uses the enum returned directly (no wrappers).
        """
        target_enum = {
            "processed":  TCS.Processed,
            "confirmed":  TCS.Confirmed,
            "finalized":  TCS.Finalized,
        }[commitment.lower()]

        for attempt in range(1, max_retries + 1):
            try:
                resp   = self.client.get_signature_statuses(
                            [txn_sig], search_transaction_history=True)
                entry  = resp.value[0]           # TransactionStatus | None

                if entry is None:
                    print(f"Awaiting status… try {attempt}/{max_retries}")

                else:
                    cs_enum = entry.confirmation_status   # None | enum

                    if cs_enum is None:
                        print(f"Status: None… try {attempt}")

                    elif cs_enum in (target_enum, TCS.Finalized):
                        print(f"Transaction {cs_enum} (slot {entry.slot})")

                        return True

                    else:
                        print(f"Status: {cs_enum}… try {attempt}")

            except Exception as err:
                print(f"Error while polling status (try {attempt}): {err}")

            time.sleep(retry_interval)

        print("Max retries reached – giving up.")
        return False

    def get_quote(
        self, input_mint: str, output_mint: str, amount: str, slippage_bps: str
    ) -> dict: 
        print('getting quote')
        url = f'https://lite-api.jup.ag/swap/v1/quote?inputMint={str(input_mint)}&outputMint={str(output_mint)}&amount={str(amount)}&slippageBps={str(slippage_bps)}'
        print(url)
        r = requests.get(url)
        r.raise_for_status()

        return r.json()


    # ─── main call ────────────────────────────────────────────────────────────────
    def get_swap(self, quote_response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Ask Jupiter for a signed v0 swap transaction (still needs your signature).
        """
        url = "https://lite-api.jup.ag/swap/v1/swap"

        raw_payload: Dict[str, Any] = {
            "userPublicKey": str(self.pubkey),                # may be Pubkey → stringify
            "quoteResponse": quote_response,

            # quality-of-life flags
            "createAssociatedTokenAccount": True,
            "useSharedAccounts": False,
            "asLegacyTransaction": False,
            "wrapAndUnwrapSol": True,

            # explicit, even if zero
            "feeBps": 0,
        }

        payload = (raw_payload)             # ← fixes the TypeError

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
            return resp.json()

        except SendTransactionPreflightFailureMessage as err:
            if "insufficient lamports" in err.message:
                print("⛔  Swap failed during simulation: "
                    "wallet ran out of SOL for rent/fees.")
            else:
                print("Swap simulation failed:", err.message)
            return False


        except requests.HTTPError as err:
            print("Jupiter /swap HTTP %s: %s",
                        err.response.status_code, err.response.text)
        except requests.RequestException as err:
            print("Error contacting Jupiter /swap: %s", err)

        return None


    def swap(self, input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int) -> Tuple[bool, str]:
        """
        Execute token swap.

        Args:
            input_mint (str): Input token mint address
            output_mint (str): Output token mint address
            amount_lamports (int): Amount in lamports to swap
            slippage_bps (int): Slippage tolerance in basis points

        Returns:
            bool: True if swap successful, False otherwise
        """
        quote_response = self.get_quote(input_mint, output_mint, amount_lamports, slippage_bps)
        if not quote_response:
            print("No quote response.")
            return False

        swap_transaction = self.get_swap(quote_response)
        if not swap_transaction:
            print("No swap transaction response.")
            return False
        print("Swap transaction:", json.dumps(swap_transaction, indent=4), "\n")

        raw_transaction = VersionedTransaction.from_bytes(
            base64.b64decode(swap_transaction['swapTransaction'])
        )
        signature = self.keypair.sign_message(to_bytes_versioned(raw_transaction.message))
        signed_txn = VersionedTransaction.populate(raw_transaction.message, [signature])
        opts = TxOpts(skip_preflight=False, preflight_commitment=Processed)

        try:
            txn_sig = self.client.send_raw_transaction(
                txn=bytes(signed_txn), opts=opts
            ).value
            print("Transaction Signature:", txn_sig)

            print("Confirming transaction...")
            confirmed = self.confirm_txn(txn_sig)
            print("Transaction confirmed:", confirmed)

            return confirmed, txn_sig

        except Exception as e:
            print(f"Failed to send transaction: {e}")
            txn_sig = None
            return False, txn_sig
