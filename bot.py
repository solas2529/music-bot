import asyncio
import logging
import os
import signal
import sys

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
# Optional: sync commands to one server so they appear instantly. Without it
# they sync globally, which can take up to an hour to show up.
DISCORD_GUILD_ID = os.getenv('DISCORD_GUILD_ID')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
log = logging.getLogger('music-bot')

if not DISCORD_BOT_TOKEN:
    log.error('Missing DISCORD_BOT_TOKEN in .env — cannot start.')
    sys.exit(1)

try:
    import fastaudio  # noqa: F401
except ImportError:
    log.error('C extension not built. Run: python3 setup.py build_ext --inplace')
    sys.exit(1)

from music import sources  # noqa: E402  (reads settings from .env, so import after load_dotenv)


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

    async def setup_hook(self):
        self.tree.error(on_app_command_error)
        await self.load_extension('music.commands')
        await sources.warm_up()

        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info('Synced %d slash commands to guild %s.', len(synced), DISCORD_GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info('Synced %d slash commands globally.', len(synced))

    async def on_ready(self):
        log.info('Logged in as %s', self.user)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name='/play'))


async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.error('Error executing /%s', interaction.command.name if interaction.command else '?', exc_info=error)
    message = 'Something went wrong running that command.'
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def main():
    # Built here rather than at import: the lookup worker processes re-import this
    # file, and shouldn't each construct a Discord client.
    bot = MusicBot()
    loop = asyncio.get_running_loop()

    def shutdown(sig):
        log.info('Received %s, shutting down.', sig.name)
        asyncio.create_task(bot.close())  # also disconnects from voice

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown, sig)

    async with bot:
        # A failed login (bad token, DNS not up yet at boot) raises here and exits
        # non-zero so the service manager restarts us.
        await bot.start(DISCORD_BOT_TOKEN)


if __name__ == '__main__':
    asyncio.run(main())
