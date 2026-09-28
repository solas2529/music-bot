# Music Bot

Discord music bot: plays YouTube and Spotify links (songs, albums, playlists) or YouTube searches in voice channels. Python + a small C extension.

The first song streams straight from YouTube so it starts right away. While it plays, upcoming songs are downloaded and encoded to MP3 **in memory** (never written to disk), front of the queue first, as many as fit. From then on songs play from memory, and each buffer is freed as soon as its song ends, making room for the next ones.

- The service is capped at **512 MB** total. The bot itself uses ~125 MB, so songs get **350 MB** (~4 hours of music at 192 kbps). Change it with `AUDIO_MEMORY_MB` in `.env`.
- Songs over 1 hour and live streams always stream, start to finish.
- If you skip to a song before it's loaded, that one streams too.
- **Even volume:** songs loaded into memory are measured (EBU R128) while they download and brought to -14 LUFS (the Spotify/YouTube level) by a per-song gain in the C code, with a soft limiter so boosted peaks round off instead of clipping. Streamed songs are evened out live by ffmpeg.
- **No cuts:** yt-dlp runs in separate worker processes so it can't stall the audio thread, downloads run at low CPU priority, and up to 15 s of decoded audio is buffered ahead of the player.

Spotify doesn't let bots stream its audio, so Spotify links are read for their track list and each song is matched to the best YouTube upload (title similarity + duration, scored in C).

## Commands

| Command | |
|---|---|
| `/play <link or search>` | Queue a YouTube/Spotify song, album, or playlist (up to 100 tracks) |
| `/skip` | Skip the current song |
| `/pause` / `/resume` | Pause or resume |
| `/stop` | Clear the queue and leave |
| `/queue [page]` | Show upcoming songs |
| `/nowplaying` | Show the current song |
| `/volume <0-200>` | Set volume (default 50) |
| `/shuffle` | Shuffle the queue |
| `/remove <position>` | Remove a song from the queue |

The bot leaves after 5 minutes with an empty queue, or 1 minute after everyone leaves the channel.

## Setup

Needs `ffmpeg`, `gcc`, and Python 3.11+ headers (`python3-dev`).

```bash
pip install --user --break-system-packages -r requirements.txt
python3 setup.py build_ext --inplace     # builds fastaudio (the C extension)
cp .env.example .env                     # then fill in the token
python3 bot.py
```

### Discord application

1. At https://discord.com/developers/applications create an application → **Bot** → Reset Token → paste into `.env`.
2. No privileged intents are needed.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions **View Channels, Send Messages, Embed Links, Connect, Speak**. Open the URL to invite it.
4. Put your server ID in `DISCORD_GUILD_ID` so slash commands appear instantly (right-click the server → Copy Server ID, with Developer Mode on).

### Run at boot

Before installing the service, edit `User` and `WorkingDirectory` in
`music-bot.service` to match the deployment account and repository path.

```bash
sudo cp music-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now music-bot
journalctl -u music-bot -f               # logs
```

## Files

- `bot.py`: entry point, slash-command sync, shutdown handling
- `music/commands.py`: slash commands and auto-leave
- `music/player.py`: per-server queue, stream-or-memory playback, loading ahead within the memory budget
- `music/sources.py`: link/search → tracks, streaming, MP3 download to memory, Spotify→YouTube matching
- `music/spotify.py`: reads Spotify track lists (no API key needed)
- `fastaudio.c`: per-frame volume + loudness gain with a soft limiter, and title-similarity scoring

## Keeping it working

YouTube changes often. If downloads start failing, update yt-dlp first:
`pip install --user --break-system-packages -U yt-dlp`

## License

MIT
