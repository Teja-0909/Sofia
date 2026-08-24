import logging

from app import bot, config, db


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
    )
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set — copy .env.example to .env and fill it in")
    if not config.ALLOWED_USER_ID:
        raise SystemExit(
            "ALLOWED_TELEGRAM_USER_ID is not set — the bot is single-user by design "
            "(spec Section 9) and refuses to start without it"
        )

    import asyncio

    asyncio.run(db.init())

    app = bot.build_application()
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
