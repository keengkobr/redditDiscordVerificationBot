"""One-off manual utility: prints the relay webhook's URL directly to this
terminal, and nowhere else.

discord_bot.py deliberately never logs the full URL (it's a live credential
-- journalctl history persists in backups/log shipping even though reading
it directly needs sudo). Run this by hand whenever you actually need the
URL, e.g. to fill in the Devvit app's `discordWebhookUrl` setting. Its output goes
to your terminal's stdout only, not to any persistent log.

Usage:
    python3 get_relay_webhook_url.py
"""

import asyncio

import discord

import config
from discord_bot import RELAY_WEBHOOK_NAME


async def main() -> None:
    config.validate(require_discord=True)

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            channel = client.get_channel(config.VERIFY_RELAY_CHANNEL_ID)
            if not channel:
                print(f"Could not find channel {config.VERIFY_RELAY_CHANNEL_ID} -- "
                      "check VERIFY_RELAY_CHANNEL_ID in .env.")
                return

            webhooks = await channel.webhooks()
            existing = next((w for w in webhooks if w.name == RELAY_WEBHOOK_NAME), None)
            if not existing:
                print(
                    "No relay webhook found yet -- start discord_bot.py first, "
                    "it creates one automatically on startup."
                )
                return

            print(f"Relay webhook URL (id={existing.id}):")
            print(existing.url)
        finally:
            await client.close()

    await client.start(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
