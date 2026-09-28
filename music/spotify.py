"""Spotify link -> list of (title, artist, duration_ms).

Spotify doesn't serve audio to third parties, so we only read track metadata
here and later find the matching upload on YouTube. The public embed page
carries that metadata as JSON, so no Spotify API credentials are needed.
"""

import json
import re
import urllib.request

SPOTIFY_URL_RE = re.compile(
    r'https?://open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)'
)
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)

# The embed only lists the first 100 entries of large playlists anyway.
MAX_TRACKS = 100


class SpotifyError(Exception):
    pass


def is_spotify_url(text):
    return SPOTIFY_URL_RE.match(text) is not None


def _fetch_entity(kind, spotify_id):
    request = urllib.request.Request(
        f'https://open.spotify.com/embed/{kind}/{spotify_id}',
        headers={'User-Agent': 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko)'},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            html = response.read().decode('utf-8', 'replace')
    except OSError as error:
        raise SpotifyError(f'Could not reach Spotify: {error}') from error

    match = NEXT_DATA_RE.search(html)
    if not match:
        raise SpotifyError('Spotify page did not contain track data (is the link private or invalid?).')

    try:
        page = json.loads(match.group(1))['props']['pageProps']
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise SpotifyError('Spotify changed its page format; could not read track data.') from error

    entity = (page.get('state') or {}).get('data', {}).get('entity')
    if not entity:
        raise SpotifyError(f"Couldn't find that Spotify {kind} — check the link.")
    return entity


def resolve(url):
    """Blocking. Returns (collection_name, [(title, artist, duration_ms), ...])."""
    match = SPOTIFY_URL_RE.match(url)
    if not match:
        raise SpotifyError('Not a Spotify track, album, or playlist link.')

    kind, spotify_id = match.groups()
    entity = _fetch_entity(kind, spotify_id)

    if kind == 'track':
        artists = ', '.join(a['name'] for a in entity.get('artists') or [])
        return None, [(entity['name'], artists, entity.get('duration') or 0)]

    tracks = [
        # In track lists the artist names live in 'subtitle'.
        (item['title'], item.get('subtitle', ''), item.get('duration') or 0)
        for item in entity.get('trackList') or []
        if item.get('entityType', 'track') == 'track'
    ][:MAX_TRACKS]

    if not tracks:
        raise SpotifyError(f'That Spotify {kind} has no playable tracks.')
    return entity.get('name') or entity.get('title'), tracks
