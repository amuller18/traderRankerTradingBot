import asyncio
from TelegramToDiscordBot import TelegramToDiscordBot
from threading import Thread
import time
import requests
from config import Config


# Function to run the Telegram bot with a new event loop in a separate thread
def start_telegram_bot():

    # Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)  # Set the loop for this thread
    bot = TelegramToDiscordBot()
    try:
        loop.run_until_complete(bot.start_bot())
    except Exception as e:
        bot.send_to_discord(f"Error in Telegram bot: {e}")
        print(f"Error in Telegram bot: {e}")
    finally:
        loop.close()

#send to discord
def send_to_discord(content: str) -> None:
        data = {"content": content}
        response = requests.post(Config.DISCORD_WEBHOOK_URL, json=data, timeout=10)

def main():
    send_to_discord("Telegram copy trader started...")
    print("Welcome to the telegram copy trader...")

    # Create and start a new thread for the Telegram bot
    try:
        telegram_thread = Thread(target=start_telegram_bot, daemon=True)
        telegram_thread.start()  # Start the thread

        # Keep the main thread alive
        while telegram_thread.is_alive():
            time.sleep(1)  # Sleep to reduce CPU usage
        else:
            
            print("Telegram bot thread has terminated.")
            main()
    except Exception as e:
        print(e)
        print("Failed to start the Telegram bot.")

if __name__ == "__main__":
    main()
