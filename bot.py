from pathlib import Path
from aiogram.methods import GetUpdates
import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from config import BOT_TOKEN
from aiogram.fsm.storage.memory import SimpleEventIsolation

proxy_url = os.getenv("TELEGRAM_PROXY_URL", "").strip()
if proxy_url:
    bot = Bot(token=BOT_TOKEN, session=AiohttpSession(proxy=proxy_url))
else:
    bot = Bot(token=BOT_TOKEN)

@bot.session.middleware()
async def _coliseum_polling_heartbeat(make_request, bot, method):
    result = await make_request(bot, method)

    if isinstance(method, GetUpdates):
        try:
            db_path = Path(
                os.getenv("STUDY_BOT_DB_PATH", "/data/database.db")
            )
            (db_path.parent / ".polling_heartbeat").touch()
        except OSError:
            pass

    return result


dp = Dispatcher(events_isolation=SimpleEventIsolation())