"""Legacy заглушка раннего кода доступа к БД (Phase 3: не вызывается).

В production/тестах/CLI нет ни одного вызова create_connection; живой
модуль доступа к БД -- database.db (SQLite/aiosqlite, STUDY_BOT_DB_PATH).
Файл оставлен по явному решению Phase 3; удаление требует owner review,
т.к. статический анализ не доказывает отсутствие динамического использования.
"""
import aiosqlite


DB_PATH = "database/database.db"


async def create_connection():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("SELECT 1")
        await db.commit()