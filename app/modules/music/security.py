from app.core.remote_fetch import validate_remote_url

MUSIC_SOURCE_HOSTS = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "spotify.com",
        "soundcloud.com",
    }
)
MUSIC_IMAGE_HOSTS = frozenset(
    {
        "ytimg.com",
        "googleusercontent.com",
        "ggpht.com",
        "sndcdn.com",
        "scdn.co",
    }
)


def validate_music_url(url: str, *, resolve: bool = True) -> str:
    return validate_remote_url(
        url,
        allowed_hosts=MUSIC_SOURCE_HOSTS,
        https_only=True,
        resolve=resolve,
    )
