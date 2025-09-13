from telethon import TelegramClient, events
from config import Config
import asyncio


async def get_list_of_channels():
    client = TelegramClient("user_session", Config.API_ID, Config.API_HASH)
    await client.start()
    dialogs = client.get_dialogs()
    
    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            print(f"Channel ID: {dialog.id}, Channel Title: {dialog.title}")

asyncio.run(get_list_of_channels())