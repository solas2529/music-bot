"""One GuildPlayer per server: owns the voice connection and the queue."""

import asyncio
import logging
import random
from collections import deque

import discord

from .sources import Track, TrackError, VolumeSource, format_duration

log = logging.getLogger(__name__)

# Songs loaded into memory at the same time. Downloads run ~15x faster than
# playback, so one is plenty, and more would compete with what's playing.
MAX_PARALLEL_DOWNLOADS = 1
# Leave the voice channel after this long with nothing queued.
IDLE_TIMEOUT_S = 5 * 60
DEFAULT_VOLUME = 0.5
EMBED_COLOR = 0x1DB954


class GuildPlayer:
    def __init__(self, bot, guild, text_channel, on_destroy):
        self.bot = bot
        self.guild = guild
        self.text_channel = text_channel
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.volume = DEFAULT_VOLUME

        self._on_destroy = on_destroy
        self._destroyed = False
        self._has_tracks = asyncio.Event()
        self._track_finished = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name=f'player-{guild.id}')

    @property
    def voice(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    def enqueue(self, tracks):
        self.queue.extend(tracks)
        self._has_tracks.set()
        # Load upcoming songs into memory while this one plays. (If nothing is
        # playing, the first song streams and loading starts once it does.)
        if self.current is not None:
            self._fill_memory()

    def skip(self):
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()  # fires the after-callback, which advances the queue
            return True
        if self.current is not None:
            self.current.release()  # still looking it up: abandon it; _play moves on
            return True
        return False

    def shuffle(self):
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)
        self._fill_memory()

    def remove(self, index):
        track = self.queue[index]
        del self.queue[index]
        track.release()
        self._fill_memory()
        return track

    def set_volume(self, volume):
        self.volume = volume
        if self.voice and isinstance(self.voice.source, VolumeSource):
            self.voice.source.volume = volume

    async def destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        for track in self.queue:
            track.release()
        self.queue.clear()
        if self.current:
            self.current.release()
        if not self._task.done() and self._task is not asyncio.current_task():
            self._task.cancel()
        if self.voice:
            await self.voice.disconnect(force=True)
        self._on_destroy(self)

    def _fill_memory(self):
        """Load upcoming songs into memory, front of the queue first, until the budget is full."""
        if self._destroyed:
            return
        queue = list(self.queue)
        downloading = sum(t.downloading for t in queue)

        for position, track in enumerate(queue):
            if downloading >= MAX_PARALLEL_DOWNLOADS:
                return
            if track.in_memory or track.download_failed or track.stream_only:
                continue  # stream_only: long songs and live streams always stream

            while not track.start_download():
                # Out of room. Songs nearer the front matter more, so evict the
                # loaded song furthest back in the queue, if it's behind this one.
                victim = next((t for t in reversed(queue[position + 1:]) if t.in_memory), None)
                if victim is None:
                    return  # memory is full of songs that play sooner; wait for one to finish
                downloading -= victim.downloading
                victim.release()

            downloading += 1
            # When this one lands (or fails), start on the next.
            track._download_task.add_done_callback(lambda _: self._fill_memory())

    async def _run(self):
        try:
            while True:
                if not self.queue:
                    self._has_tracks.clear()
                    try:
                        await asyncio.wait_for(self._has_tracks.wait(), IDLE_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        await self.send('Queue has been empty for a while — leaving the voice channel.')
                        break

                if not self.voice or not self.voice.is_connected():
                    break  # kicked or disconnected; nothing to play into

                track = self.queue.popleft()
                self.current = track
                try:
                    if await self._play(track):
                        await self._track_finished.wait()
                finally:
                    # Song over (or skipped): drop its MP3 from memory right away.
                    track.release()
                    self.current = None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Player loop crashed in guild %s', self.guild.id)
            await self.send('The player hit an unexpected error and stopped.')
        finally:
            self.current = None
            if not self.bot.is_closed():
                asyncio.create_task(self.destroy())

    async def _play(self, track):
        """Start one track. Returns False if it couldn't be played."""
        try:
            if track.mp3 is None:
                # Not fully in memory yet: stream it now rather than making everyone wait.
                track.cancel_download()
                await track.lookup()
        except TrackError as error:
            await self.send(f'⚠️ Skipping **{discord.utils.escape_markdown(track.display_title)}**: {error}')
            return False
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the player itself is shutting down
            return False  # /skip abandoned the lookup

        loop = asyncio.get_running_loop()
        self._track_finished.clear()

        def after(error):
            if error:
                log.error('Playback error in guild %s: %s', self.guild.id, error)
            loop.call_soon_threadsafe(self._track_finished.set)

        if not self.voice or not self.voice.is_connected():
            return False  # disconnected during the lookup; the loop exits next pass

        self.voice.play(track.audio_source(self.volume), after=after)
        await self.send(embed=self.now_playing_embed())
        self._fill_memory()
        return True

    def now_playing_embed(self):
        track = self.current
        embed = discord.Embed(
            title='🎶 Now Playing',
            color=EMBED_COLOR,
            description=f'**[{discord.utils.escape_markdown(track.display_title)}]({track.url})**',
        )
        embed.add_field(name='Duration', value='🔴 Live' if track.is_live else format_duration(track.duration))
        embed.add_field(name='Requested by', value=track.requester)
        embed.add_field(name='Up next', value=f'{len(self.queue)} in queue')
        if track.mp3 is not None:
            embed.set_footer(text=f'Playing from memory · loudness adjusted {track.gain_db:+.1f} dB')
        else:
            embed.set_footer(text='Streaming · loudness evened out live')
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        return embed

    async def send(self, content=None, **kwargs):
        try:
            await self.text_channel.send(content, **kwargs)
        except discord.HTTPException as error:
            log.warning('Could not send to #%s: %s', self.text_channel, error)
