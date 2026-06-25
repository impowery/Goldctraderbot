"""GOLD cTrader Bot — entry point."""

import asyncio
from ctrader_client import GoldBotClient


async def main():
    bot = GoldBotClient()
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
