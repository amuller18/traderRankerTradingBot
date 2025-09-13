import base64
import time
from typing import Any, Dict, List
from decimal import Decimal

import requests
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed, Finalized, Processed
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
import json

from spl.token.instructions import (
    get_associated_token_address,
    create_associated_token_account,
)
from solana.transaction import Transaction


class JupiterLimit:
    """
    Drop-in replacement for JupiterSwap that creates / cancels *limit* orders
    via https://api.jup.ag/limit/v2.
    """

    SOL = "So11111111111111111111111111111111111111112"

    def __init__(self, private_key: str, rpc_endpoint: str):
        self.rpc = rpc_endpoint
        self.client = Client(self.rpc)
        self.keypair = Keypair.from_base58_string(private_key.replace(":", ""))
        self.pubkey = str(self.keypair.pubkey())
        #self._ensure_wsol_ata()

    # ----------------------------------------------------------------
    #                       helper for __init__
    # ----------------------------------------------------------------
    def _ensure_wsol_ata(self) -> None:
        """
        Make sure an associated-token-account (ATA) for the wSOL mint exists.
        Safe to run on every bot start – it does nothing if the account is
        already present.
        """
        # --- convert types so spl-token helpers are happy -----------------
        mint_pub   = Pubkey.from_string(self.SOL)

        # spl.token.instructions wants a *solana.publickey.PublicKey*
        ata = get_associated_token_address(self.pubkey, mint_pub)

        if self.client.get_account_info(ata).value is not None:
            return  # already created – we're done

        # We need a signer object that solana-py's Transaction understands.
        # Build one from the same secret key you loaded with *solders*.
        payer_kp = self.keypair

        tx = Transaction().add(
            create_associated_token_account(
                payer=self.pubkey,   # fee payer / signer
                owner=self.pubkey,             # token account owner
                mint=mint_pub,               # wSOL mint
            )
        )

        sig = self.client.send_transaction(
            tx,
            payer_kp,                                    # signer list
            opts=TxOpts(skip_preflight=True,
                        preflight_commitment=Processed),
        ).value

        # Wait until the network sees it so later limit-order txs don’t race
        self._confirm(sig)

    # ----------  low-level helpers  ----------

        # ----------  public REST helpers  ----------
    def get_quote(
        self, input_mint: str, output_mint: str, amount: str, slippage_bps: str
    ) -> dict: 
        print('getting quote')
        url = f'https://quote-api.jup.ag/v6/quote?inputMint={str(input_mint)}&outputMint={str(output_mint)}&amount={str(amount)}&slippageBps={str(slippage_bps)}'
        print(url)
        r = requests.get(url)
        r.raise_for_status()

        return r.json()


    def _post(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"accept": "application/json", "content-type": "application/json"}
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()

    def _confirm(self, sig: str, commitment="confirmed") -> bool:
        desired = {"confirmed": Confirmed, "finalized": Finalized}[commitment]
        for _ in range(30):
            s = self.client.get_signature_statuses([sig], True).value[0]
            if s and s.confirmation_status in ("confirmed", "finalized"):
                return True
            time.sleep(1)
        return False

    def _sign_and_send(self, b64_tx: str) -> str:
        raw = VersionedTransaction.from_bytes(base64.b64decode(b64_tx))
        sig = self.keypair.sign_message(to_bytes_versioned(raw.message))
        signed = VersionedTransaction.populate(raw.message, [sig])
        opts = TxOpts(skip_preflight=False, preflight_commitment=Processed)
        return self.client.send_raw_transaction(bytes(signed), opts=opts).value

    # ----------  public API  ----------

    def create_limit_order(self, quote: dict, price_multiplyer) -> str:

        multiplier    = Decimal(str(price_multiplyer))         
        raw_amount    = Decimal(quote["outAmount"]) * multiplier 
        taking_amount = str(int(raw_amount)) 
        headers  = {"accept": "application/json", "content-type": "application/json"}
        payload  = {
            "inputMint":  str(quote["inputMint"]),   
            "outputMint": str(quote["outputMint"]), 
            "maker":  str(self.pubkey),
            "payer":  str(self.pubkey),
            "params": {
                "makingAmount": str(quote['inAmount']), 
                "takingAmount": taking_amount,
            },
            "computeUnitPrice": "auto"
        }
                    
        resp = requests.post(
            "https://api.jup.ag/limit/v2/createOrder",
            headers=headers,
            json=payload,
        )
        print('Create order response:')
        print(resp.text)
        print(resp.status_code)
        print(resp.json())

        resp.raise_for_status()      
                        # will throw if 400/500
        tx_b64 = resp.json()["tx"]                  # unsigned v0 transaction blob

        # --- 3. Decode → sign with your keypair -------------------------------------
        unsigned_tx      = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        msg              = unsigned_tx.message
        signed_tx        = VersionedTransaction(msg, [self.keypair])       # ⬅️ signs here

        # --- 4. Serialize and send ---------------------------------------------------
        tx_sig = self.client.send_raw_transaction(
            bytes(signed_tx),
            opts=TxOpts(skip_preflight=True)         # set skip_preflight=False once happy
        )
        print("✅  Transaction signature:", tx_sig.value)
        resp = json.loads(resp.text)              # or r.json()
        if resp.get("code") == 0:
            return resp["order"]               # <- hand the PDA upstream
        print("Create-order failed: %s", resp)
        return None

    def cancel_limit_orders(self, order_pubkeys: List[str] | None = None) -> str:
        """
        Cancel specific orders (list) or **all** open orders for this wallet.

        Returns the tx signature of the cancel-all / batch-cancel transaction.
        """
        body = {
            "maker": self.pubkey,
            "computeUnitPrice": "auto",
        }
        if order_pubkeys:
            body["orders"] = order_pubkeys

        res = self._post("https://api.jup.ag/limit/v2/cancelOrders", body)
        tx_b64 = res["txs"][0]          # API can batch; we send the first chunk

        sig = self._sign_and_send(tx_b64)
        ok = self._confirm(sig)
        if not ok:
            raise RuntimeError("Cancel-order txn not confirmed")
        return sig
    
    def _post(self, url: str, body: dict):
        r = requests.post(url, json=body, timeout=12)
        if r.status_code != 200:
            print("LO 400 → %s", r.text)     #  <-- keep the body
            r.raise_for_status()
        return r.json()
