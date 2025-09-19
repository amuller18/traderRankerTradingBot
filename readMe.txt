# TraderRanker Trading Bot

A sophisticated Telegram-based trading bot that automatically copies trades from Telegram channels and executes them on Solana using Jupiter DEX, with intelligent market cap-based sell strategies.

## Features

- **Real-time Telegram Monitoring**: Monitors multiple Telegram channels for trading signals
- **Automatic Trade Execution**: Executes trades instantly when valid contract addresses are detected
- **Intelligent Sell Strategies**: Market cap-based dynamic ladder selling system
- **Non-blocking Architecture**: Zero impact on buy performance with background order monitoring
- **Robust Error Handling**: Comprehensive retry logic and critical alert system
- **Discord Integration**: Real-time notifications and status updates
- **Database Logging**: Tracks all calls and trades in AWS DynamoDB

## Dependencies

```
base58
requests
dotenv
telethon
solana==0.30.0
asyncio
boto3
aiohttp
```

## Market Cap Strategy Breakdown

The bot uses intelligent market cap-based strategies to determine sell ladder configurations:

| Market Cap | Strategy | Key Targets | Risk Level | Description |
|------------|----------|-------------|------------|-------------|
| < 100k | Ultra Aggressive | 1.5x - 100x | Very High | Maximum aggression for micro caps |
| 100k - 1M | Aggressive | 2x - 100x | High | High risk, high reward approach |
| 1M - 5M | Moderate | 2x - 50x | Medium | Balanced strategy for mid caps |
| 5M+ | Conservative | 2x - 25x | Low | Conservative approach for large caps |

### Strategy Details

#### Ultra Aggressive (< 100k MC)
- **Targets**: 1.5x, 2x, 3x, 5x, 8x, 15x, 25x, 50x, 100x
- **Allocation**: 15%, 20%, 20%, 15%, 10%, 8%, 6%, 4%, 2%
- **Use Case**: Micro cap tokens with high volatility potential

#### Aggressive (100k - 1M MC)
- **Targets**: 2x, 3x, 5x, 8x, 15x, 25x, 50x, 100x
- **Allocation**: 20%, 25%, 20%, 15%, 10%, 5%, 3%, 2%
- **Use Case**: Small cap tokens with growth potential

#### Moderate (1M - 5M MC)
- **Targets**: 2x, 3x, 5x, 8x, 15x, 25x, 50x
- **Allocation**: 25%, 25%, 20%, 15%, 10%, 3%, 2%
- **Use Case**: Mid cap tokens with established presence

#### Conservative (5M+ MC)
- **Targets**: 2x, 3x, 5x, 8x, 15x, 25x
- **Allocation**: 30%, 25%, 20%, 15%, 7%, 3%
- **Use Case**: Large cap tokens with lower volatility

## Configuration

### Environment Variables Required

```env
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
DISCORD_WEBHOOK_URL=your_discord_webhook
SOLANA_RPC_URL=your_solana_rpc_url
SOLANA_TRADING_WALLET_PRIVATE_KEY=your_private_key
AWS_REGION=your_aws_region
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
```

### Trading Parameters

- **Buy Amount**: 0.001 SOL (configurable)
- **Slippage**: 1000 bps (10%)
- **Max Retries**: 5 attempts for failed orders
- **Balance Check Interval**: 30 seconds
- **Order Timeout**: 5 minutes

## Architecture

### Core Components

1. **TelegramToDiscordBot**: Main bot class handling all operations
2. **JupiterSwap**: Handles instant token swaps
3. **JupiterLimit**: Manages limit order creation and execution
4. **SolanaDex**: Provides token information and market cap data

### Key Features

- **Non-blocking Buy Operations**: Instant trade execution with zero delays
- **Background Order Monitoring**: Continuous balance checking and ladder placement
- **Parallel Order Processing**: All limit orders created simultaneously
- **Intelligent Retry Logic**: Automatic retry with exponential backoff
- **Critical Alert System**: Discord notifications for persistent failures

## Usage

1. Install dependencies: `pip install -r requirements.txt`
2. Configure environment variables in `.env` file
3. Run the bot: `python main.py`

## Database Schema

### Calls Table
- Username: Telegram username
- CA: Contract address
- Timestamp: Unix timestamp

### Trades Table
- Username: Telegram username
- CA: Contract address
- Timestamp: Unix timestamp
- MC: Market cap at time of trade

## Error Handling

- **DynamoDB Failures**: Graceful degradation with error logging
- **Network Issues**: Automatic retry with exponential backoff
- **Order Failures**: Individual order retry without affecting others
- **Critical Alerts**: Discord notifications for persistent issues

## Performance

- **Buy Speed**: Lightning-fast execution with no blocking operations
- **Memory Efficient**: Automatic cleanup of completed orders
- **Resource Optimized**: Single monitoring task per token
- **Fault Tolerant**: Individual failures don't affect other operations

## Monitoring

The bot provides comprehensive monitoring through:
- **Discord Notifications**: Real-time status updates
- **Log Files**: Detailed logging in `bot.log`
- **Database Tracking**: Complete trade and call history
- **Strategy Notifications**: Shows which strategy is being used for each token