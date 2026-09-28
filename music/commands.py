import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from .player import EMBED_COLOR, GuildPlayer
from .sources import TrackError, format_duration, resolve

# Leave once the bot has been alone in its voice channel this long.
ALONE_TIMEOUT_S = 60
QUEUE_PAGE_SIZE = 10


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}

    def _forget(self, player):
        # Only drop the entry if it's still this player; /play may have made a new one.
        if self.players.get(player.guild.id) is player:
            del self.players[player.guild.id]

    async def _require_same_channel(self, interaction):
        """Reply with an error and return None unless the user shares the bot's voice channel."""
        player = self.players.get(interaction.guild_id)
        if player is None or not player.voice:
            await interaction.response.send_message('Nothing is playing right now.', ephemeral=True)
            return None

        user_channel = interaction.user.voice.channel if interaction.user.voice else None
        if user_channel != player.voice.channel:
            await interaction.response.send_message(
                f'Join {player.voice.channel.mention} to control the music.', ephemeral=True
            )
            return None
        return player

    @app_commands.command(name='play', description='Play a YouTube/Spotify link (song, album, or playlist) or search YouTube')
    @app_commands.describe(query='YouTube or Spotify link, or words to search YouTube for')
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        user_voice = interaction.user.voice
        if not user_voice or not user_voice.channel:
            await interaction.response.send_message('Join a voice channel first.', ephemeral=True)
            return

        player = self.players.get(interaction.guild_id)
        if player and player.voice and player.voice.channel != user_voice.channel:
            await interaction.response.send_message(
                f"I'm already playing in {player.voice.channel.mention}.", ephemeral=True
            )
            return

        # Resolving a link can take several seconds; Discord only waits 3.
        await interaction.response.defer(thinking=True)

        try:
            playlist_name, tracks = await resolve(query, interaction.user.mention)
        except TrackError as error:
            await interaction.followup.send(f'❌ {error}')
            return

        if interaction.guild.voice_client is None:
            try:
                await user_voice.channel.connect(self_deaf=True, timeout=20)
            except (discord.ClientException, asyncio.TimeoutError) as error:
                await interaction.followup.send(f'❌ Could not join {user_voice.channel.mention}: {error}')
                return

        player = self.players.get(interaction.guild_id)
        if player is None:
            player = GuildPlayer(self.bot, interaction.guild, interaction.channel, self._forget)
            self.players[interaction.guild_id] = player

        position = len(player.queue) + (1 if player.current else 0)
        player.enqueue(tracks)

        if playlist_name or len(tracks) > 1:
            name = discord.utils.escape_markdown(playlist_name or 'playlist')
            await interaction.followup.send(f'📃 Queued **{len(tracks)}** tracks from **{name}**.')
        elif position == 0:
            await interaction.followup.send(f'▶️ Starting **{discord.utils.escape_markdown(tracks[0].display_title)}**')
        else:
            await interaction.followup.send(
                f'➕ Queued **{discord.utils.escape_markdown(tracks[0].display_title)}** '
                f'({format_duration(tracks[0].duration)}) at position **{position}**.'
            )

    @app_commands.command(name='skip', description='Skip the current song')
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        if not (player := await self._require_same_channel(interaction)):
            return
        title = player.current.display_title if player.current else None
        if player.skip():
            await interaction.response.send_message(f'⏭️ Skipped **{discord.utils.escape_markdown(title)}**.')
        else:
            await interaction.response.send_message('Nothing to skip.', ephemeral=True)

    @app_commands.command(name='pause', description='Pause playback')
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        if not (player := await self._require_same_channel(interaction)):
            return
        if player.voice.is_playing():
            player.voice.pause()
            await interaction.response.send_message('⏸️ Paused.')
        else:
            await interaction.response.send_message('Nothing is playing.', ephemeral=True)

    @app_commands.command(name='resume', description='Resume playback')
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        if not (player := await self._require_same_channel(interaction)):
            return
        if player.voice.is_paused():
            player.voice.resume()
            await interaction.response.send_message('▶️ Resumed.')
        else:
            await interaction.response.send_message("Playback isn't paused.", ephemeral=True)

    @app_commands.command(name='stop', description='Stop playback, clear the queue, and leave the voice channel')
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        if not (player := await self._require_same_channel(interaction)):
            return
        await player.destroy()
        await interaction.response.send_message('⏹️ Stopped and cleared the queue.')

    @app_commands.command(name='nowplaying', description='Show the current song')
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or not player.current:
            await interaction.response.send_message('Nothing is playing right now.', ephemeral=True)
            return
        await interaction.response.send_message(embed=player.now_playing_embed())

    @app_commands.command(name='queue', description='Show the upcoming songs')
    @app_commands.describe(page='Page number (10 songs per page)')
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
        player = self.players.get(interaction.guild_id)
        if not player or (not player.current and not player.queue):
            await interaction.response.send_message('The queue is empty.', ephemeral=True)
            return

        pages = max(1, -(-len(player.queue) // QUEUE_PAGE_SIZE))
        page = min(page, pages)
        start = (page - 1) * QUEUE_PAGE_SIZE
        upcoming = list(player.queue)[start:start + QUEUE_PAGE_SIZE]

        lines = []
        if player.current:
            lines.append(f'**Now:** {discord.utils.escape_markdown(player.current.display_title)}\n')
        lines += [
            f'`{start + i}.` {discord.utils.escape_markdown(t.display_title)} ({format_duration(t.duration)})'
            for i, t in enumerate(upcoming, start=1)
        ]
        if not upcoming:
            lines.append('*Nothing queued after this song.*')

        total_s = sum(t.duration or 0 for t in player.queue)
        embed = discord.Embed(title='🎵 Queue', color=EMBED_COLOR, description='\n'.join(lines))
        embed.set_footer(
            text=f'Page {page}/{pages} · {len(player.queue)} queued · {format_duration(total_s)} total'
            f' · Volume {round(player.volume * 100)}%'
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name='volume', description='Set the playback volume')
    @app_commands.describe(percent='0 to 200 (default is 50)')
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 200]):
        if not (player := await self._require_same_channel(interaction)):
            return
        player.set_volume(percent / 100)
        await interaction.response.send_message(f'🔊 Volume set to **{percent}%**.')

    @app_commands.command(name='shuffle', description='Shuffle the upcoming songs')
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        if not (player := await self._require_same_channel(interaction)):
            return
        if len(player.queue) < 2:
            await interaction.response.send_message('Not enough songs in the queue to shuffle.', ephemeral=True)
            return
        player.shuffle()
        await interaction.response.send_message(f'🔀 Shuffled {len(player.queue)} songs.')

    @app_commands.command(name='remove', description='Remove a song from the queue')
    @app_commands.describe(position='Position shown in /queue')
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]):
        if not (player := await self._require_same_channel(interaction)):
            return
        if position > len(player.queue):
            await interaction.response.send_message(f'The queue only has {len(player.queue)} songs.', ephemeral=True)
            return
        removed = player.remove(position - 1)
        await interaction.response.send_message(f'🗑️ Removed **{discord.utils.escape_markdown(removed.display_title)}**.')

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        player = self.players.get(member.guild.id)
        if player is None:
            return

        # Someone disconnected the bot by hand: tear the player down.
        if member.id == self.bot.user.id and after.channel is None:
            await player.destroy()
            return

        channel = player.voice.channel if player.voice else None
        if channel is None or before.channel != channel:
            return
        if any(not m.bot for m in channel.members):
            return

        await asyncio.sleep(ALONE_TIMEOUT_S)
        if self.players.get(member.guild.id) is player and player.voice and not any(
            not m.bot for m in player.voice.channel.members
        ):
            await player.send('Everyone left the voice channel — stopping.')
            await player.destroy()


async def setup(bot):
    await bot.add_cog(Music(bot))
