import logging
import re
import base58
import requests
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Callable, Awaitable, Tuple, Optional
from asyncio import PriorityQueue
from config import Config
import boto3
import solana_dex

from telethon import TelegramClient, events
from dataclasses import dataclass, field

from jupiter import JupiterSwap
from jupiter_limit import JupiterLimit
LAMPORTS_PER_SOL = 1_000_000_000

# ───────────────────────── Logging ────────────────────────────
logging.basicConfig(
    filename="bot.log",
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filemode='w'
)
telethon_logger = logging.getLogger("telethon")
telethon_logger.setLevel(logging.WARNING)

# ─────────────────────── Priority Item ────────────────────────
@dataclass(order=True)
class PrioritizedItem:
    priority: int
    task_id: int = field(compare=False)
    coro: Callable[..., Awaitable[Any]] = field(compare=False)
    args: Tuple[Any, ...] = field(compare=False, default_factory=tuple)

# ───────────────────── Telegram Bot Class ─────────────────────
class TelegramToDiscordBot:
    def __init__(self):
        
        # Telethon
        self.client = TelegramClient("user_session", Config.API_ID, Config.API_HASH)

        # Trading libs
        self.rpc = Config.SOLANA_RPC_URL
        self.jupiter = JupiterSwap(Config.SOLANA_TRADING_WALLET_PRIVATE_KEY, self.rpc)
        self.jupiter_limit = JupiterLimit(Config.SOLANA_TRADING_WALLET_PRIVATE_KEY, self.rpc)

        # Runtime config
        self.buy_amount = 0.001  # SOL
        self.slippage = 1000      # bps

        # State
        self.portfolio_value = 0
        self.csv_file = ".portfolio_value.csv"
        self.processed_message_ids = set()
        self.call_counts: Dict[str, int] = {}
        self.last_reset_time = datetime.now()

        # Task queue
        self.swap_queue: PriorityQueue[PrioritizedItem] = PriorityQueue()
        self._task_counter = 0
        self.worker_started = False

        # Limit order monitoring attributes
        self.pending_orders: Dict[str, Dict] = {}
        self.order_retry_counts: Dict[str, int] = {}
        self.max_retries = 5
        self.critical_retry_threshold = 3
        self.balance_check_interval = 30
        self.order_timeout = 300

        #DynamoDB setup
        self.dynamodb = boto3.resource(
            "dynamodb",
            region_name=Config.AWS_REGION,
            aws_access_key_id=Config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY
        )
        self.callsDB = self.dynamodb.Table("Calls")
        self.tradesDB = self.dynamodb.Table("trades")

    # ─────────────── Discord helper ───────────────
    def send_to_discord(self, content: str) -> None:
        data = {"content": content}
        try:
            response = requests.post(Config.DISCORD_WEBHOOK_URL, json=data, timeout=10)
            if response.status_code != 204:
                logging.error("Discord webhook error %s: %s", response.status_code, response.text)
        except requests.RequestException as exc:
            logging.error("Discord webhook exception: %s", exc)

    # ─────────────── Util helpers ───────────────
    def reset_call_counts(self) -> None:
        if datetime.now() - self.last_reset_time >= timedelta(days=1):
            self.call_counts.clear()
            self.last_reset_time = datetime.now()
            logging.info("Daily call counts reset")

    def _sol_balance(self) -> float:
        lamports = self.jupiter.client.get_balance(self.jupiter.pubkey).value
        return lamports / 1e9

    def check_valid_ca(self, ca: str) -> bool:
        try:
            decoded = base58.b58decode(ca)
            return len(decoded) == 32
        except Exception as exc:
            logging.error("Error decoding base58 address %s: %s", ca, exc)
            return False

    # ─────────────── Queue helpers ───────────────
    def _enqueue(self, priority: int, coro: Callable[..., Awaitable[Any]], *args) -> None:
        self._task_counter += 1
        self.swap_queue.put_nowait(PrioritizedItem(priority, self._task_counter, coro, args))

    async def _swap_worker(self) -> None:
        while True:
            item: PrioritizedItem = await self.swap_queue.get()
            try:
                await item.coro(*item.args)
            except Exception as exc:
                logging.exception("Task failed: %s", exc)
            finally:
                self.swap_queue.task_done()

    # ─────────────── Database logging ───────────────
    def log_trade_in_db(self, username, ca, unix_timestamp, mc) -> None:
        # Convert datetime to timestamp if needed
        if hasattr(unix_timestamp, 'timestamp'):
            timestamp = int(unix_timestamp.timestamp())
        else:
            timestamp = unix_timestamp
            
        # Extract username string if it's a Channel object
        if hasattr(username, 'username') and username.username:
            username_str = username.username
        elif hasattr(username, 'title'):
            username_str = username.title
        else:
            username_str = str(username)
            
        try:
            self.tradesDB.put_item(
                Item={
                    'Username': username_str, 
                    'CA': ca, 
                    'Timestamp': timestamp, 
                    'MC': mc
                }
            )
            logging.info("Trade logged in database")
        except Exception as e:
            logging.error(f"Failed to log trade to database: {e}")

    def log_call_in_db(self, username, ca, unix_timestamp) -> None:
        # Convert datetime to timestamp if needed
        if hasattr(unix_timestamp, 'timestamp'):
            timestamp = int(unix_timestamp.timestamp())
        else:
            timestamp = unix_timestamp
            
        # Extract username string if it's a Channel object
        if hasattr(username, 'username') and username.username:
            username_str = username.username
        elif hasattr(username, 'title'):
            username_str = username.title
        else:
            username_str = str(username)
            
        try:
            self.callsDB.put_item(
                Item={
                    'Username': username_str, 
                    'CA': ca, 
                    'Timestamp': timestamp
                }
            )
            logging.info("Call logged in database")
        except Exception as e:
            logging.error(f"Failed to log call to database: {e}")

    # ─────────────── Core trading operations ───────────────
    async def swap_tokens(self, ca: str, event) -> None:
        logging.info("Submitting swap for %s", ca)
        try:
            buy_amount_sol = Decimal(str(self.buy_amount))
        except (InvalidOperation, TypeError):
            logging.error("buy_amount %r is not numeric", self.buy_amount)
            return

        balance_sol = Decimal(str(self._sol_balance()))
        needed_sol = buy_amount_sol * Decimal("1.01")
        if balance_sol < needed_sol:
            msg = f"🚫 Not enough SOL (have {balance_sol:.4f}, need {needed_sol:.4f}) – skipping swap for {ca}"
            logging.warning(msg)
            self.send_to_discord(msg)
            return

        lamports = int(float(self.buy_amount) * LAMPORTS_PER_SOL)
        sol_mint = "So11111111111111111111111111111111111111112"

        try:
            success, txn_sig = self.jupiter.swap(sol_mint, ca, lamports, self.slippage)
        except Exception as exc:
            logging.exception("self.jupiter.swap() raised: %s", exc)
            self.send_to_discord(f"Swap call errored for {ca}: {exc}")
            return

        if not success:
            msg = f"Swap failed for {ca}"
            logging.error(msg)
            self.send_to_discord(msg)
            return

        mc = solana_dex.SolanaDex.get_token_info(ca).get("market_cap", "N/A")
        unix_timestamp = event.message.date
        sender = await event.get_sender()
        username = sender.username or "anonymous"
        self.log_trade_in_db(username, ca, unix_timestamp, mc)

        msg = f"✅ Swap successful for {ca} – txn {txn_sig} confirmed at {datetime.now().isoformat()} at {mc} MC"
        
        logging.info(msg)
        self.send_to_discord(msg)

        # ⚡ NON-BLOCKING: Just queue the monitoring task - NO WAITING!
        ladder_cfg = {3: 33, 5: 20, 10: 15, 25: 10, 50: 10, 100: 10}
        expected_balance = int(float(self.buy_amount) * LAMPORTS_PER_SOL)
        
        # Add to pending orders (instant operation)
        self.pending_orders[ca] = {
            'ladder_config': ladder_cfg,
            'expected_balance': expected_balance,
            'timestamp': datetime.now(),
            'txn_sig': txn_sig
        }
        
        # Queue the monitoring task (non-blocking)
        self._enqueue(2, self._monitor_single_order, ca)  # Priority 2 = lower than swaps
        
        logging.info(f"Queued {ca} for order monitoring")

    # ─────────────── Ladder creation (DEPRECATED - Use _place_sell_ladder_with_retry) ───────────────
    def place_sell_ladder(
        self,
        ca: str,
        ladder: Dict[float, float],
        expiry_sec: int | None = 7 * 24 * 3600,
    ) -> None:
        """
        DEPRECATED: This method is kept for backward compatibility.
        Use _place_sell_ladder_with_retry for new implementations.
        """
        bal = self.jupiter.get_token_balance_lamports(ca)
        if not bal:
            logging.warning("No balance for %s, ladder skipped", ca)
            return

        for mult, pct in sorted(ladder.items()):
            making_amount = int(bal * (pct / 100))
            if making_amount == 0:
                continue

            try:
                quote = self.jupiter_limit.get_quote(
                    input_mint=ca,
                    output_mint=self.jupiter.SOL,
                    amount=str(making_amount),
                    slippage_bps=str(100),
                )
                order_pda = self.jupiter_limit.create_limit_order(quote, mult)
                msg = (
                    f"📈 Limit‑sell {pct:.1f}% @ ×{mult} → {order_pda[:8]}…"
                )
                logging.info(msg)
                self.send_to_discord(msg)
            except Exception as err:
                logging.error("Failed to create ladder: %s", err)

    # ─────────────── New Limit Order Monitoring Methods ───────────────
    async def _monitor_single_order(self, ca: str) -> None:
        """Monitor a single order without blocking other operations"""
        if ca not in self.pending_orders:
            return
            
        order_info = self.pending_orders[ca]
        start_time = datetime.now()
        
        while ca in self.pending_orders:
            try:
                # Check timeout
                if (datetime.now() - start_time).seconds > self.order_timeout:
                    logging.warning(f"Order timeout for {ca}, removing from pending")
                    if ca in self.pending_orders:
                        del self.pending_orders[ca]
                    if ca in self.order_retry_counts:
                        del self.order_retry_counts[ca]
                    return
                
                # Check if tokens have arrived
                current_balance = self.jupiter.get_token_balance_lamports(ca)
                
                if current_balance and current_balance > 0:
                    logging.info(f"Tokens detected for {ca}, placing sell ladder")
                    
                    # Place the ladder (this runs in background)
                    success = await self._place_sell_ladder_with_retry(
                        ca, 
                        order_info['ladder_config'], 
                        order_info['expected_balance']
                    )
                    
                    if success:
                        # Remove from pending orders
                        if ca in self.pending_orders:
                            del self.pending_orders[ca]
                        if ca in self.order_retry_counts:
                            del self.order_retry_counts[ca]
                        return
                    else:
                        # Increment retry count
                        self.order_retry_counts[ca] = self.order_retry_counts.get(ca, 0) + 1
                        
                        if self.order_retry_counts[ca] >= self.critical_retry_threshold:
                            await self._send_critical_alert(ca, "Limit order placement failed repeatedly")
                            # Remove from pending to stop retrying
                            if ca in self.pending_orders:
                                del self.pending_orders[ca]
                            return
                
                # Wait before next check (non-blocking sleep)
                await asyncio.sleep(self.balance_check_interval)
                
            except Exception as e:
                logging.error(f"Error monitoring order for {ca}: {e}")
                # Remove from pending on error
                if ca in self.pending_orders:
                    del self.pending_orders[ca]
                return

    async def _place_sell_ladder_with_retry(self, ca: str, ladder_config: Dict[float, float], expected_balance: int) -> bool:
        """Place sell ladder with retry logic - optimized for speed"""
        try:
            current_balance = self.jupiter.get_token_balance_lamports(ca)
            if not current_balance or current_balance == 0:
                logging.warning(f"No token balance for {ca}, skipping ladder")
                return False
            
            balance_to_use = min(expected_balance, current_balance)
            successful_orders = []
            failed_orders = []
            
            # Process orders in parallel for speed
            tasks = []
            for mult, pct in sorted(ladder_config.items()):
                making_amount = int(balance_to_use * (pct / 100))
                if making_amount == 0:
                    continue
                tasks.append(self._create_single_order(ca, mult, pct, making_amount))
            
            # Wait for all orders to complete
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for i, result in enumerate(results):
                mult, pct = list(sorted(ladder_config.items()))[i]
                if isinstance(result, Exception):
                    failed_orders.append(f"{pct}% @ ×{mult}")
                    logging.error(f"Order failed: {result}")
                elif result:
                    successful_orders.append(f"{pct}% @ ×{mult} → {result[:8]}…")
                else:
                    failed_orders.append(f"{pct}% @ ×{mult}")
            
            # Send status update
            if successful_orders:
                success_msg = f"📈 Limit orders created for {ca}:\n" + "\n".join(successful_orders)
                self.send_to_discord(success_msg)
                
            if failed_orders:
                retry_count = self.order_retry_counts.get(ca, 0)
                if retry_count < self.max_retries:
                    retry_msg = f"⚠️ Some orders failed for {ca}, will retry: {', '.join(failed_orders)}"
                    logging.warning(retry_msg)
                    self.send_to_discord(retry_msg)
                    return False
                else:
                    error_msg = f"🚨 CRITICAL: All limit orders failed for {ca} after {retry_count} retries"
                    logging.error(error_msg)
                    self.send_to_discord(error_msg)
                    return False
                
            return len(failed_orders) == 0
            
        except Exception as e:
            logging.error(f"Error in ladder placement for {ca}: {e}")
            return False

    async def _create_single_order(self, ca: str, mult: float, pct: float, amount: int) -> Optional[str]:
        """Create a single limit order - optimized for parallel execution"""
        try:
            # Get quote
            quote = self.jupiter_limit.get_quote(
                input_mint=ca,
                output_mint=self.jupiter.SOL,
                amount=str(amount),
                slippage_bps=str(100),
            )
            
            if not quote:
                return None
                
            # Create order
            order_id = self.jupiter_limit.create_limit_order(quote, mult)
            return order_id
            
        except Exception as e:
            logging.error(f"Failed to create order {pct}% @ ×{mult} for {ca}: {e}")
            return None

    async def _send_critical_alert(self, ca: str, message: str):
        """Send critical alert to Discord"""
        critical_msg = f"🚨 **CRITICAL ALERT** 🚨\n" \
                      f"Token: {ca}\n" \
                      f"Issue: {message}\n" \
                      f"Time: {datetime.now().isoformat()}\n" \
                      f"Retry Count: {self.order_retry_counts.get(ca, 0)}"
        
        self.send_to_discord(critical_msg)
        logging.critical(critical_msg)

    # ─────────────── Telegram event handler ───────────────
    async def _on_message(self, event) -> None:
        self.reset_call_counts()

        msg_id = event.message.id
        if msg_id in self.processed_message_ids:
            return
        self.processed_message_ids.add(msg_id)

        unix_timestamp = event.message.date
        sender = await event.get_sender()
        username = sender.username or "anonymous"
        self.call_counts.setdefault(username, 0)
        if self.call_counts[username] >= 3:
            return

        solana_pattern = r"[A-HJ-NP-Za-km-z1-9]{32,44}"
        for ca in re.findall(solana_pattern, event.raw_text):
            if self.check_valid_ca(ca):
                logging.info("Valid CA %s queued", ca)
                self._enqueue(0, self.swap_tokens, ca, event)
                self.log_call_in_db(username, ca, unix_timestamp)
                self.call_counts[username] += 1
            else:
                logging.warning("Invalid CA %s", ca)

    # ─────────────── Startup ───────────────
    async def start_bot(self) -> None:
        print("running bot")
        await self.client.start()

        channels = await asyncio.gather(
            *[self.client.get_entity(ch) for ch in Config.CHANNELS_TO_TRACK]
        )
        self.client.add_event_handler(self._on_message, events.NewMessage(chats=channels))

        if not self.worker_started:
            asyncio.create_task(self._swap_worker(), name="swap_worker")
            self.worker_started = True

        logging.info("Bot is running…")
        await self.client.run_until_disconnected()

