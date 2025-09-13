import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class Config:
    API_ID = os.getenv("TELEGRAM_API_ID")
    API_HASH = os.getenv("TELEGRAM_API_HASH")
    DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
    SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    WALLET_FILE = os.getenv("WALLET_FILE")
    BIRDEYE_API_ALEXMULLER = os.getenv("BIRDEYE_API_ALEXMULLER")
    BIRDEYE_API_ALEXJM = os.getenv('BIRDEYE_API_ALEXJM')
    BIRDEYE_API_KEYS = os.getenv("BIRDEYE_API_KEYS", "").split(",")
    
    SOLANA_ADDRESS_PATTERN = r'[1-9A-HJ-NP-Za-km-z]{44}'  # Solana address regex                            
    CHANNELS_TO_TRACK = [
        -1002049726578, #Marcell
        -1001835320066, #Levis
        -1002007485926, #FOMO
        -1002461618969, #Test Channel
        -1001500884162, #Exys Lab
        -1001650441782, #CTM Call
        -1002166830181, #PRIMAERE
        -1002045925410, #Exys Degen
        -1002246179846,  #Fomo Gambling
        -1002049726578,  #Marcell's Trenches
        -1002885276091,  #Shah's Gambles
        ]  

    AWS_REGION = os.getenv("AWS_REGION")
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

    SOLANA_TRADING_WALLET_PRIVATE_KEY = os.getenv("SOLANA_TRADING_WALLET_PRIVATE_KEY")

