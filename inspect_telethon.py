import inspect
from telethon import TelegramClient
print('has get_entity', hasattr(TelegramClient, 'get_entity'))
print(inspect.signature(TelegramClient.get_entity))
src = inspect.getsource(TelegramClient.get_entity)
print(src[:5000])
