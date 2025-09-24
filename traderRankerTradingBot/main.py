import asyncio
from TelegramToDiscordBot import TelegramToDiscordBot
from threading import Thread
import time


# Function to run the Telegram bot with a new event loop in a separate thread
def start_telegram_bot():

    # Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)  # Set the loop for this thread
    bot = TelegramToDiscordBot()
    try:
        loop.run_until_complete(bot.start_bot())
    except Exception as e:
        print(f"Error in Telegram bot: {e}")
    finally:
        loop.close()

def main():
    print("Welcome to the telegram copy trader...")

    # Create and start a new thread for the Telegram bot
    try:
        telegram_thread = Thread(target=start_telegram_bot, daemon=True)
        telegram_thread.start()  # Start the thread

        # Keep the main thread alive
        while telegram_thread.is_alive():
            time.sleep(1)  # Sleep to reduce CPU usage
    except Exception as e:
        print(e)
        print("Failed to start the Telegram bot.")

if __name__ == "__main__":
    main()
