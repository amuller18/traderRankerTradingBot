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
        self.buy_amount = 0.0244  # SOL
        self.slippage = 1000      # bps

        # State
        self.portfolio_value = 0
        self.csv_file = ".portfolio_value.csv"
        self.processed_message_ids = set()
        
        # Global portfolio-wide call limiting system
        self.token_call_counts: Dict[str, int] = {}  # Global call counts per token
        self.weekly_reset_time = datetime.now()
        self.max_calls_per_token_weekly = 3  # Global limit per token per week
        self.reset_interval_days = 7  # Weekly reset

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

        # Market cap based ladder configurations
        self.mc_ladder_configs = {
            "ultra_aggressive": {  # < 100k MC
                "multipliers": [1.5, 2, 3, 5, 8, 15, 25, 50, 100],
                "percentages": [15, 20, 20, 15, 10, 8, 6, 4, 2]
            },
            "aggressive": {  # 100k - 1M MC
                "multipliers": [2, 3, 5, 8, 15, 25, 50, 100],
                "percentages": [20, 25, 20, 15, 10, 5, 3, 2]
            },
            "moderate": {  # 1M - 5M MC
                "multipliers": [2, 3, 5, 8, 15, 25, 50],
                "percentages": [25, 25, 20, 15, 10, 3, 2]
            },
            "conservative": {  # 5M+ MC
                "multipliers": [5, 10, 15],
                "percentages": [50, 20, 20]
                
            }
        }
        '''
            test
            "conservative": {  # 5M+ MC
                "multipliers": [2, 3, 5, 8, 15, 25],
                "percentages": [30, 25, 20, 15, 7, 3]'''
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
        """Reset global token call counts weekly"""
        now = datetime.now()
        
        # Weekly reset for global token call counts
        if now - self.weekly_reset_time >= timedelta(days=self.reset_interval_days):
            self.token_call_counts.clear()
            self.weekly_reset_time = now
            logging.info("Weekly global token call counts reset")
            
            # Send reset notification to Discord
            reset_msg = f"🔄 **Weekly Reset Complete**\n" \
                       f"All token call limits have been reset.\n" \
                       f"Portfolio is ready for new token calls."
            self.send_to_discord(reset_msg)

    def can_call_token(self, token_ca: str) -> Tuple[bool, str]:
        """
        Check if a token can be called based on global portfolio limits.
        
        Args:
            token_ca: Token contract address
            
        Returns:
            Tuple of (can_call: bool, reason: str)
        """
        # Check global per-token call limit
        token_calls = self.token_call_counts.get(token_ca, 0)
        if token_calls >= self.max_calls_per_token_weekly:
            return False, f"Token call limit reached ({self.max_calls_per_token_weekly} calls this week)"
        
        return True, "OK"

    def increment_token_call_count(self, token_ca: str) -> None:
        """Increment global token call count"""
        self.token_call_counts[token_ca] = self.token_call_counts.get(token_ca, 0) + 1
        logging.info(f"Token call count updated for {token_ca[:8]}: {self.token_call_counts[token_ca]}/{self.max_calls_per_token_weekly}")

    def get_token_call_stats(self, token_ca: str) -> Dict[str, Any]:
        """Get call statistics for a token"""
        token_calls = self.token_call_counts.get(token_ca, 0)
        
        return {
            "token_calls": token_calls,
            "token_limit": self.max_calls_per_token_weekly,
            "remaining_calls": self.max_calls_per_token_weekly - token_calls,
            "next_reset": self.weekly_reset_time + timedelta(days=self.reset_interval_days)
        }

    def get_portfolio_stats(self) -> Dict[str, Any]:
        """Get overall portfolio call statistics"""
        total_tokens = len(self.token_call_counts)
        total_calls = sum(self.token_call_counts.values())
        
        return {
            "total_tokens_called": total_tokens,
            "total_calls": total_calls,
            "next_reset": self.weekly_reset_time + timedelta(days=self.reset_interval_days),
            "token_breakdown": dict(self.token_call_counts)
        }

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

    # ─────────────── Market Cap Ladder Methods ───────────────
    def _get_mc_ladder_config(self, market_cap: int) -> Dict[float, float]:
        """
        Get ladder configuration based on market cap.
        
        Args:
            market_cap: Market cap in USD
            
        Returns:
            Dictionary with multipliers as keys and percentages as values
        """
        if market_cap < 100_000:  # < 100k - Ultra aggressive
            config = self.mc_ladder_configs["ultra_aggressive"]
        elif market_cap < 1_000_000:  # 100k - 1M - Aggressive
            config = self.mc_ladder_configs["aggressive"]
        elif market_cap < 5_000_000:  # 1M - 5M - Moderate
            config = self.mc_ladder_configs["moderate"]
        else:  # 5M+ - Conservative
            config = self.mc_ladder_configs["conservative"]
        
        # Convert to the format expected by the ladder system
        ladder_cfg = {}
        for mult, pct in zip(config["multipliers"], config["percentages"]):
            ladder_cfg[mult] = pct
            
        return ladder_cfg

    def _get_mc_category(self, market_cap: int) -> str:
        """Get market cap category name for logging"""
        if market_cap < 100_000:
            return "Ultra Aggressive (<100k)"
        elif market_cap < 1_000_000:
            return "Aggressive (100k-1M)"
        elif market_cap < 5_000_000:
            return "Moderate (1M-5M)"
        else:
            return "Conservative (5M+)"

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
            username_str = username.title()  # Call the method
        else:
            username_str = str(username)
            
        try:
            self.tradesDB.put_item(
                Item={
                    'Username': str(username_str), 
                    'CA': str(ca), 
                    'Timestamp': str(timestamp), 
                    'MC': str(mc)
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
            username_str = username.title()  # Call the method
        else:
            username_str = str(username)
            
        try:
            self.callsDB.put_item(
                Item={
                    'Username': str(username_str), 
                    'CA': str(ca), 
                    'Timestamp': str(timestamp)
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
        # Fix: Extract username string properly
        if hasattr(sender, 'username') and sender.username:
            username = sender.username
        elif hasattr(sender, 'title'):
            username = sender.title
        else:
            username = "anonymous"
        self.log_trade_in_db(username, ca, unix_timestamp, mc)

        msg = f"✅ Swap successful for {ca} – txn {txn_sig} confirmed at {datetime.now().isoformat()} at {mc} MC"
        
        logging.info(msg)
        self.send_to_discord(msg)

        # ⚡ NON-BLOCKING: Dynamic ladder based on market cap
        try:
            # Get market cap and determine ladder strategy
            mc_int = int(mc) if isinstance(mc, (int, float)) and mc != "N/A" else 0
            ladder_cfg = self._get_mc_ladder_config(mc_int)
            mc_category = self._get_mc_category(mc_int)
            
            expected_balance = int(float(self.buy_amount) * LAMPORTS_PER_SOL)
            
            # Add to pending orders (instant operation)
            self.pending_orders[ca] = {
                'ladder_config': ladder_cfg,
                'expected_balance': expected_balance,
                'timestamp': datetime.now(),
                'txn_sig': txn_sig,
                'market_cap': mc_int,
                'mc_category': mc_category
            }
            
            # Queue the monitoring task (non-blocking)
            self._enqueue(2, self._monitor_single_order, ca)  # Priority 2 = lower than swaps
            
            # Send strategy notification
            strategy_msg = f"📊 **Strategy for {ca}**: {mc_category} (${mc_int:,} MC)\n" \
                          f"🎯 **Targets**: {', '.join([f'{pct}% @ {mult}x' for mult, pct in ladder_cfg.items()])}"
            self.send_to_discord(strategy_msg)
            
            # Debug: Log the ladder configuration
            logging.info(f"Ladder config for {ca}: {ladder_cfg}")
            
            logging.info(f"Queued {ca} for {mc_category} order monitoring")
            
        except Exception as e:
            logging.error(f"Error setting up dynamic ladder for {ca}: {e}")
            # Fallback to conservative ladder
            fallback_cfg = self.mc_ladder_configs["conservative"]
            ladder_cfg = {mult: pct for mult, pct in zip(fallback_cfg["multipliers"], fallback_cfg["percentages"])}
            
            expected_balance = int(float(self.buy_amount) * LAMPORTS_PER_SOL)
            self.pending_orders[ca] = {
                'ladder_config': ladder_cfg,
                'expected_balance': expected_balance,
                'timestamp': datetime.now(),
                'txn_sig': txn_sig,
                'market_cap': 0,
                'mc_category': "Fallback"
            }
            self._enqueue(2, self._monitor_single_order, ca)

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
            
            # Use the actual token balance, not the SOL amount
            balance_to_use = current_balance
            successful_orders = []
            failed_orders = []
            
            # Process orders in parallel for speed
            tasks = []
            for mult, pct in sorted(ladder_config.items()):
                making_amount = int(balance_to_use * (pct / 100))
                if making_amount == 0:
                    continue
                logging.info(f"Creating order: {pct}% @ {mult}x for {ca} with amount {making_amount} (balance: {balance_to_use})")
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
            
            # Send status update with market cap context
            if successful_orders:
                # Get market cap category from pending orders if available
                mc_category = "Unknown"
                if ca in self.pending_orders:
                    mc_category = self.pending_orders[ca].get('mc_category', 'Unknown')
                
                success_msg = f"📈 **{mc_category}** Limit orders created for {ca}:\n" + "\n".join(successful_orders)
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
        # Fix: Extract username string properly
        if hasattr(sender, 'username') and sender.username:
            username = sender.username
        elif hasattr(sender, 'title'):
            username = sender.title
        else:
            username = "anonymous"

        solana_pattern = r"[A-HJ-NP-Za-km-z1-9]{32,44}"
        processed_cas = set()  # Track processed CAs in this message
        for ca in re.findall(solana_pattern, event.raw_text):
            if ca in processed_cas:
                continue  # Skip duplicate CAs in same message
            processed_cas.add(ca)
            
            if self.check_valid_ca(ca):
                # Check if this token can be called globally
                can_call, reason = self.can_call_token(ca)
                
                if not can_call:
                    logging.warning(f"Token call blocked for {ca[:8]}: {reason}")
                    # Send limit notification to Discord
                    remaining_time = self.weekly_reset_time + timedelta(days=self.reset_interval_days) - datetime.now()
                    days_left = remaining_time.days
                    hours_left = remaining_time.seconds // 3600
                    
                    limit_msg = f"🚫 **Token Call Limit Reached**\n" \
                               f"Token: {ca[:8]}...\n" \
                               f"Reason: {reason}\n" \
                               f"⏰ Reset in: {days_left}d {hours_left}h"
                    self.send_to_discord(limit_msg)
                    continue
                
                logging.info("Valid CA %s queued (Global calls: %d/%d)", ca, self.token_call_counts.get(ca, 0) + 1, self.max_calls_per_token_weekly)
                self._enqueue(0, self.swap_tokens, ca, event)
                self.log_call_in_db(username, ca, unix_timestamp)
                self.increment_token_call_count(ca)
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