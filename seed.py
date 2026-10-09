"""Синхронізує слова з data/*.csv у базу даних, не запускаючи бота.

Використання:
    python seed.py

Бере DATABASE_URL з .env або зі змінних середовища, тому так само можна оновити
базу на Render (вставте External Database URL у .env і запустіть скрипт).
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


async def main() -> None:
    pool = await db.create_pool(os.environ["DATABASE_URL"])
    try:
        await db.init(pool)
        rows = await pool.fetch(
            """SELECT learn_lang, translation_lang, count(*) AS n
               FROM words WHERE owner_id IS NULL
               GROUP BY 1, 2 ORDER BY 1, 2"""
        )
        for r in rows:
            print(f"learn={r['learn_lang']} translation={r['translation_lang']}: {r['n']} слів")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
