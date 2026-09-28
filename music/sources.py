"""Turn user input into Track objects and Track objects into playable audio.

A song that isn't in memory yet (like the first one requested) streams straight
from YouTube so it starts right away. Meanwhile upcoming songs are downloaded
and encoded to MP3 entirely in memory (never touching disk), as many as the
memory budget allows, played from those buffers, and freed as soon as each ends.
"""

import asyncio
import concurrent.futures
import io
import logging
import multiprocessing
import os
import re
import shlex
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import discord
import yt_dlp

import fastaudio

from . import spotify

log = logging.getLogger(__name__)

YOUTUBE_URL_RE = re.compile(r'https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/', re.I)

MAX_PLAYLIST_TRACKS = 100
# How many YouTube results to weigh when matching a Spotify track.
SPOTIFY_SEARCH_RESULTS = 5
# Words that usually mean "not the studio recording" unless the song itself uses them.
UNWANTED_WORDS = ('live', 'cover', 'karaoke', 'instrumental', 'remix', 'sped up', 'slowed', 'nightcore', '8d', 'reverb')

# At 192 kbps an MP3 is ~1.4 MB/minute.
MP3_BITRATE = '192k'
MP3_BYTES_PER_S = 192_000 // 8
# Longer songs (and live streams) always stream instead of loading into memory.
MAX_MEMORY_TRACK_S = 60 * 60
MAX_MP3_BYTES = 100 * 1024 * 1024
# Size guess for songs whose length we don't know yet.
UNKNOWN_DURATION_ESTIMATE_S = 6 * 60
# How much memory in-memory MP3s may use in total, across every server. The
# service is capped at 512 MB; the bot itself idles around 125 MB and each
# ffmpeg download takes some too, so songs get the rest (~4 hours of music).
AUDIO_MEMORY_BYTES = int(os.getenv('AUDIO_MEMORY_MB', '350')) * 1024 * 1024
DOWNLOAD_CHUNK = 64 * 1024

AUDIO_FORMAT = 'bestaudio/best'
# YouTube stream URLs expire after ~6h; look up again well before that.
STREAM_URL_TTL_S = 60 * 60
STREAM_BEFORE_OPTIONS = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'

# Loudness normalization, so every song plays at about the same volume.
# Songs loaded into memory are measured (EBU R128) while they download and get
# one fixed gain, applied in C. -14 LUFS is what Spotify and YouTube use.
TARGET_LUFS = -14.0
TRUE_PEAK_CEILING_DB = -1.0
# How far a boost may push peaks past the ceiling; fastaudio's soft limiter
# rounds those off, letting quiet, dynamic songs (classical, acoustic) get
# closer to the target without harsh clipping.
LIMITER_HEADROOM_DB = 3.0
MAX_BOOST_DB = 12.0
# Streamed songs can't be measured in advance, so ffmpeg evens them out on the
# fly instead; p=0.8 lands typical songs within ~1.5 dB of TARGET_LUFS.
STREAM_NORMALIZE_FILTER = 'dynaudnorm=f=500:g=31:p=0.8:m=10'
LOUDNESS_RE = re.compile(r'Integrated loudness:\s+I:\s+(-?[\d.]+) LUFS')
TRUE_PEAK_RE = re.compile(r'True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS')

# yt-dlp is heavy pure-Python work. Run inside the bot it would hog the GIL and
# starve discord.py's audio thread (heard as cuts), so it runs in worker
# processes instead. Two, so a /play lookup never waits behind a background one.
LOOKUP_WORKERS = 2
# Background MP3 encoding runs at lower CPU priority than playback.
DOWNLOAD_NICENESS = 10
# Decoded audio kept queued in front of the player, to ride out network or CPU
# hiccups. 20 ms per frame, so 750 frames = 15 s (~2.9 MB).
READ_AHEAD_FRAMES = 750

YDL_BASE = {'quiet': True, 'no_warnings': True, 'noprogress': True, 'socket_timeout': 15}


class TrackError(Exception):
    pass


class AudioMemory:
    """Byte budget for in-memory MP3s. Only touched from the event loop, so no locking."""

    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    def reserve(self, size):
        if self.used + size > self.limit:
            return False
        self.used += size
        return True

    def free(self, size):
        self.used = max(0, self.used - size)


audio_memory = AudioMemory(AUDIO_MEMORY_BYTES)


def _ydl(**options):
    return yt_dlp.YoutubeDL({**YDL_BASE, **options})


def format_duration(seconds):
    if not seconds:
        return '?:??'
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02}:{secs:02}' if hours else f'{minutes}:{secs:02}'


@dataclass
class Track:
    title: str
    requester: str
    url: str | None = None          # YouTube page URL, once known
    duration: float | None = None   # seconds
    thumbnail: str | None = None
    # Set for Spotify tracks, which are matched to a YouTube upload lazily.
    spotify_artist: str | None = None
    spotify_duration_ms: int | None = None

    mp3: bytes | None = field(default=None, repr=False)
    stream_url: str | None = field(default=None, repr=False)
    _user_agent: str = field(default='Mozilla/5.0', repr=False)
    _looked_up_at: float = field(default=0.0, repr=False)
    _lookup_task: asyncio.Task | None = field(default=None, repr=False)
    _download_task: asyncio.Task | None = field(default=None, repr=False)
    _reserved: int = field(default=0, repr=False)   # bytes held against audio_memory
    download_failed: bool = field(default=False, repr=False)
    gain_db: float = 0.0            # loudness correction, once measured
    is_live: bool = False

    @property
    def display_title(self):
        return f'{self.spotify_artist} - {self.title}' if self.spotify_artist else self.title

    async def lookup(self):
        """Ensure a fresh stream URL (and full metadata). Raises TrackError."""
        if self.stream_url and time.monotonic() - self._looked_up_at < STREAM_URL_TTL_S:
            return
        if self._lookup_task is None or self._lookup_task.done():
            self._lookup_task = asyncio.create_task(self._lookup())
            self._lookup_task.add_done_callback(_ignore_unawaited_error)
        # Shielded so cancelling one waiter (e.g. a background download) doesn't
        # kill a lookup that playback is also waiting on.
        await asyncio.shield(self._lookup_task)

    @property
    def stream_only(self):
        return self.is_live or (self.duration or 0) > MAX_MEMORY_TRACK_S

    @property
    def downloading(self):
        return self._download_task is not None and not self._download_task.done()

    @property
    def in_memory(self):
        """Loaded, or loading, into memory (and so counted against the budget)."""
        return self.mp3 is not None or self.downloading

    def start_download(self):
        """Start downloading into memory. Returns False if the memory budget has no room."""
        if self.in_memory:
            return True
        estimate = int((self.duration or UNKNOWN_DURATION_ESTIMATE_S) * MP3_BYTES_PER_S)
        if not audio_memory.reserve(estimate):
            return False
        self._reserved = estimate
        self._download_task = asyncio.create_task(self._download())
        self._download_task.add_done_callback(self._download_finished)
        return True

    def _download_finished(self, task):
        if task is not self._download_task or task.cancelled():
            return  # cancel_download already cleaned up
        if task.exception() is not None:
            # Don't retry in the background; when it comes up it'll stream (and report errors).
            self.download_failed = True
            self._free_memory()

    def _free_memory(self):
        audio_memory.free(self._reserved)
        self._reserved = 0

    def cancel_download(self):
        if self.downloading:
            self._download_task.cancel()
            self._free_memory()
        self._download_task = None

    def release(self):
        """Free the in-memory MP3 and stop any lookup or download in progress."""
        self.cancel_download()
        self.mp3 = None
        self._free_memory()
        if self._lookup_task and not self._lookup_task.done():
            self._lookup_task.cancel()
        self._lookup_task = None

    async def _lookup(self):
        info = await _in_worker(_lookup_info, self.url, self.spotify_artist, self.title, self.spotify_duration_ms)
        self.apply_info(info)

    def apply_info(self, info):
        """Take metadata and the audio stream URL from a full yt-dlp info dict."""
        self.is_live = bool(info.get('is_live'))
        self.url = info.get('webpage_url') or self.url
        self.duration = info.get('duration') or self.duration
        self.thumbnail = info.get('thumbnail') or self.thumbnail
        if not self.spotify_artist:
            self.title = info.get('title') or self.title
        self.stream_url = info['url']
        self._user_agent = (info.get('http_headers') or {}).get('User-Agent', self._user_agent)
        self._looked_up_at = time.monotonic()

    async def _download(self):
        await self.lookup()
        if self.stream_only:
            # Only discovered now (e.g. a playlist entry with no listed length).
            raise TrackError('Too long to hold in memory; it will stream instead.')
        # One pass does both jobs: the audio is split, one copy is encoded to the
        # MP3 on stdout and the other runs through the loudness meter, whose
        # summary lands on stderr.
        process = await asyncio.create_subprocess_exec(
            # Lower priority than playback, so encoding never steals CPU from what's playing.
            'nice', '-n', str(DOWNLOAD_NICENESS),
            'ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-loglevel', 'info',
            '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5',
            '-user_agent', self._user_agent, '-i', self.stream_url,
            '-filter_complex', '[0:a]asplit=2[mp3][meter];[meter]ebur128=peak=true:framelog=quiet[metered]',
            '-map', '[mp3]', '-c:a', 'libmp3lame', '-b:a', MP3_BITRATE, '-f', 'mp3', 'pipe:1',
            '-map', '[metered]', '-f', 'null', os.devnull,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Drain stderr alongside stdout so a chatty ffmpeg can't fill the pipe and stall.
        stderr_task = asyncio.create_task(process.stderr.read())
        buffer = bytearray()
        try:
            while chunk := await process.stdout.read(DOWNLOAD_CHUNK):
                buffer += chunk
                if len(buffer) > MAX_MP3_BYTES:
                    raise TrackError('Audio is too large to hold in memory.')
            stderr = (await stderr_task).decode(errors='replace')
            if await process.wait() != 0 or not buffer:
                errors = [line for line in stderr.splitlines() if 'error' in line.lower()]
                raise TrackError(f"Download failed: {errors[-1] if errors else 'no audio received'}")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            stderr_task.cancel()

        self.gain_db = _normalizing_gain_db(stderr)

        self.mp3 = bytes(buffer)
        del buffer
        # Swap the size estimate for the real size.
        audio_memory.free(self._reserved)
        audio_memory.used += len(self.mp3)
        self._reserved = len(self.mp3)

    def audio_source(self, volume):
        """Play from the in-memory MP3 if it's ready, otherwise stream straight from YouTube."""
        if self.mp3 is not None:
            pcm = discord.FFmpegPCMAudio(
                io.BytesIO(self.mp3), pipe=True, before_options='-f mp3', options='-vn -loglevel error'
            )
            return VolumeSource(ReadAheadSource(pcm), volume, gain=10 ** (self.gain_db / 20))
        if self.stream_url:
            pcm = discord.FFmpegPCMAudio(
                self.stream_url,
                before_options=f'{STREAM_BEFORE_OPTIONS} -user_agent {shlex.quote(self._user_agent)}',
                options=f'-vn -af {STREAM_NORMALIZE_FILTER} -loglevel error',
            )
            return VolumeSource(ReadAheadSource(pcm), volume)
        raise TrackError('Track has no audio loaded yet.')


def _normalizing_gain_db(ebur128_log):
    """Gain that brings a song to TARGET_LUFS, limited by how far its peaks can go."""
    loudness = LOUDNESS_RE.search(ebur128_log)
    if not loudness:
        return 0.0  # couldn't measure; play it as-is
    gain = TARGET_LUFS - float(loudness.group(1))

    peak = TRUE_PEAK_RE.search(ebur128_log)
    if peak and peak.group(1) != '-inf':
        gain = min(gain, TRUE_PEAK_CEILING_DB + LIMITER_HEADROOM_DB - float(peak.group(1)))
    return max(min(gain, MAX_BOOST_DB), -30.0)


def _ignore_unawaited_error(task):
    # Errors are reported by whoever awaits the task; don't warn if nobody did.
    if not task.cancelled():
        task.exception()


class ReadAheadSource(discord.AudioSource):
    """Decodes ahead of playback on its own thread.

    ffmpeg's pipe only holds ~0.3 s of audio, so without this any stall in the
    network or CPU is heard immediately as a cut. With it, playback draws from
    a buffer of up to READ_AHEAD_FRAMES while the decoder catches up.
    """

    def __init__(self, original):
        self.original = original
        self._frames = deque()
        self._cond = threading.Condition()
        self._done = False
        threading.Thread(target=self._fill, name='audio-read-ahead', daemon=True).start()

    def _fill(self):
        try:
            while True:
                frame = self.original.read()
                with self._cond:
                    if not frame or self._done:
                        break
                    self._frames.append(frame)
                    self._cond.notify_all()
                    while len(self._frames) >= READ_AHEAD_FRAMES and not self._done:
                        self._cond.wait()
        except Exception:
            if not self._done:  # errors after cleanup() are just the killed ffmpeg
                log.exception('Audio decoder failed')
        finally:
            with self._cond:
                self._done = True
                self._cond.notify_all()

    def read(self):
        with self._cond:
            while not self._frames and not self._done:
                self._cond.wait()
            if not self._frames:
                return b''
            frame = self._frames.popleft()
            self._cond.notify_all()
            return frame

    def cleanup(self):
        with self._cond:
            self._done = True
            self._frames.clear()
            self._cond.notify_all()
        self.original.cleanup()


class VolumeSource(discord.AudioSource):
    """Like discord.PCMVolumeTransformer, but the per-frame scaling runs in C.

    volume is the /volume setting; gain is the song's loudness correction.
    """

    def __init__(self, original, volume, gain=1.0):
        self.original = original
        self.volume = volume
        self.gain = gain

    def read(self):
        frame = self.original.read()
        factor = self.volume * self.gain
        if not frame or factor == 1.0:
            return frame
        return fastaudio.scale_pcm(frame, factor)

    def cleanup(self):
        self.original.cleanup()


_worker_pool = None


def _pool():
    global _worker_pool
    if _worker_pool is None:
        # forkserver, not fork: forking a process that has live threads (discord.py
        # has several) can deadlock the child.
        context = multiprocessing.get_context('forkserver')
        context.set_forkserver_preload(['music.sources'])
        _worker_pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=LOOKUP_WORKERS, mp_context=context
        )
    return _worker_pool


async def _in_worker(function, *args):
    global _worker_pool
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_pool(), function, *args)
    except concurrent.futures.process.BrokenProcessPool:
        # A worker died (e.g. killed for memory); start a fresh pool and retry once.
        _worker_pool = None
        return await loop.run_in_executor(_pool(), function, *args)


async def warm_up():
    """Start the worker processes now so the first /play doesn't wait for them."""
    await asyncio.gather(*(_in_worker(os.getpid) for _ in range(LOOKUP_WORKERS)))


# Everything below the line runs inside the worker processes.

def _clean_ydl_error(error):
    return str(error).removeprefix('ERROR: ').split('\n')[0][:300]


def _score_candidate(artist, title, duration_ms, entry):
    wanted = f'{artist} {title}'
    found = f"{entry.get('channel') or entry.get('uploader') or ''} {entry.get('title') or ''}"
    score = fastaudio.match_score(wanted, found)

    wanted_lower, found_lower = wanted.lower(), found.lower()
    for word in UNWANTED_WORDS:
        if word in found_lower and word not in wanted_lower:
            score -= 0.3

    # Auto-generated "Artist - Topic" uploads are the plain studio audio.
    if found_lower.rstrip().endswith('topic') or 'official audio' in found_lower:
        score += 0.1

    if duration_ms and entry.get('duration'):
        expected = duration_ms / 1000
        off_by = abs(entry['duration'] - expected) / expected
        score -= min(off_by, 1.0) * 0.5
    return score


def _match_spotify_on_youtube(artist, title, duration_ms):
    query = f'{artist} - {title}'
    try:
        results = _ydl(extract_flat=True).extract_info(f'ytsearch{SPOTIFY_SEARCH_RESULTS}:{query}', download=False)
    except yt_dlp.utils.DownloadError as error:
        raise TrackError(_clean_ydl_error(error)) from error

    candidates = [e for e in results.get('entries') or [] if e and e.get('url')]
    if not candidates:
        raise TrackError(f'No YouTube match for "{query}".')

    best = max(candidates, key=lambda entry: _score_candidate(artist, title, duration_ms, entry))
    return best['url']


# Only what Track.apply_info reads; the full info dict is large to send between processes.
_INFO_KEYS = ('url', 'webpage_url', 'duration', 'thumbnail', 'title', 'is_live', 'http_headers')


def _lookup_info(url, spotify_artist, title, spotify_duration_ms):
    """Full metadata and audio stream URL for a track, matching Spotify tracks to YouTube first."""
    if url is None:
        url = _match_spotify_on_youtube(spotify_artist, title, spotify_duration_ms)
    try:
        info = _ydl(format=AUDIO_FORMAT, noplaylist=True).extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as error:
        raise TrackError(_clean_ydl_error(error)) from error
    trimmed = {key: info[key] for key in _INFO_KEYS if key in info}
    trimmed.setdefault('webpage_url', url)
    return trimmed


def _from_youtube(query, requester):
    """Blocking. Handles YouTube videos, YouTube playlists, and plain search text."""
    is_url = YOUTUBE_URL_RE.match(query) is not None

    try:
        if is_url:
            # A watch URL that happens to be inside a playlist plays just that video;
            # a bare /playlist URL enqueues the playlist (flat, so it's fast).
            info = _ydl(
                format=AUDIO_FORMAT, extract_flat='in_playlist', noplaylist=True, playlistend=MAX_PLAYLIST_TRACKS
            ).extract_info(query, download=False)
        else:
            # Full (not flat) extraction of the one result, so it arrives with its stream URL.
            results = _ydl(format=AUDIO_FORMAT, noplaylist=True).extract_info(f'ytsearch1:{query}', download=False)
            info = next((e for e in results.get('entries') or [] if e), None)
            if info is None:
                raise TrackError('No results found.')
    except yt_dlp.utils.DownloadError as error:
        raise TrackError(_clean_ydl_error(error)) from error

    if info.get('_type') == 'playlist':
        entries = [e for e in info.get('entries') or [] if e and e.get('url')]
        if not entries:
            raise TrackError('That playlist is empty or private.')
        tracks = [
            Track(title=e.get('title') or e['url'], requester=requester, url=e['url'], duration=e.get('duration'))
            for e in entries[:MAX_PLAYLIST_TRACKS]
        ]
        return info.get('title'), tracks

    # A single video: we already have its stream URL, so it can start playing immediately.
    track = Track(title=info.get('title') or query, requester=requester, url=info.get('webpage_url') or query)
    track.apply_info(info)
    return None, [track]


def _from_spotify(url, requester):
    try:
        name, items = spotify.resolve(url)
    except spotify.SpotifyError as error:
        raise TrackError(str(error)) from error

    tracks = [
        Track(
            title=title,
            requester=requester,
            duration=duration_ms / 1000 if duration_ms else None,
            spotify_artist=artist,
            spotify_duration_ms=duration_ms,
        )
        for title, artist, duration_ms in items
    ]
    return name, tracks


async def resolve(query, requester):
    """Returns (playlist_name_or_None, [Track, ...]). Raises TrackError."""
    query = query.strip().strip('<>')  # Discord wraps URLs in <> to suppress embeds
    resolver = _from_spotify if spotify.is_spotify_url(query) else _from_youtube
    return await _in_worker(resolver, query, requester)
