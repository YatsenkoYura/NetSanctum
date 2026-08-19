from fastapi import HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.module_types import ShareAsset, ShareRoute
from app.core.responses import serve_media_stream, serve_storage_file_chunked
from app.modules.music.models import Playlist, PlaylistSong, Song

MAX_SHARED_SONGS = 500


def _is_allowed(share, song_id: int) -> bool:
    return share.selection_mode == "all" or song_id in share.selector.get("song_ids", [])


async def _get_scoped_song(db: AsyncSession, share, song_id: int) -> Song:
    if not _is_allowed(share, song_id):
        raise HTTPException(status_code=404, detail="Shared content not found")
    song = await db.get(Song, song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Shared content not found")
    return song


async def _selected_songs(db: AsyncSession, share) -> list[Song]:
    if share.selection_mode == "all":
        result = await db.execute(select(Song).order_by(Song.created_at.desc()))
        return list(result.scalars().all())

    song_ids = share.selector.get("song_ids", [])
    result = await db.execute(select(Song).where(Song.id.in_(song_ids)))
    songs = {song.id: song for song in result.scalars().all()}
    return [songs[song_id] for song_id in song_ids if song_id in songs]


def _serialize_song(song: Song, share) -> dict:
    api_prefix = f"/s/{share.id}/api/music"
    return {
        "id": song.id,
        "title": song.title,
        "author": song.author,
        "original_artist": song.original_artist,
        "youtube_url": song.youtube_url,
        "audio_url": f"{api_prefix}/audio/{song.id}",
        "cover_url": f"{api_prefix}/cover/{song.id}" if song.cover_file_id else None,
        "cover_offset_x": song.cover_offset_x,
        "cover_offset_y": song.cover_offset_y,
        "created_at": song.created_at,
    }


async def _shared_playlists(db: AsyncSession, share) -> list[dict]:
    songs = await _selected_songs(db, share)
    song_map = {song.id: song for song in songs}
    if not song_map:
        return []

    result = await db.execute(
        select(Playlist, PlaylistSong.song_id)
        .join(PlaylistSong, Playlist.id == PlaylistSong.playlist_id)
        .where(PlaylistSong.song_id.in_(song_map))
        .order_by(Playlist.created_at.desc(), PlaylistSong.position.asc())
    )
    playlists: dict[int, dict] = {}
    for playlist, song_id in result.all():
        item = playlists.setdefault(
            playlist.id,
            {
                "id": playlist.id,
                "name": playlist.name,
                "description": playlist.description,
                "created_at": playlist.created_at,
                "songs": [],
                "cover_song_id": None,
                "cover_url": None,
            },
        )
        item["songs"].append(song_id)
        if playlist.cover_song_id == song_id and song_map[song_id].cover_file_id:
            item["cover_song_id"] = song_id

    api_prefix = f"/s/{share.id}/api/music"
    for item in playlists.values():
        cover_song_id = item["cover_song_id"]
        if cover_song_id is None:
            cover_song_id = next(
                (song_id for song_id in item["songs"] if song_map[song_id].cover_file_id),
                None,
            )
            item["cover_song_id"] = cover_song_id
        if cover_song_id is not None:
            item["cover_url"] = f"{api_prefix}/cover/{cover_song_id}"
    return list(playlists.values())


class MusicShareProvider:
    async def catalog(self, db: AsyncSession) -> list[dict]:
        songs_result = await db.execute(select(Song).order_by(Song.created_at.desc()))
        relations_result = await db.execute(
            select(PlaylistSong.song_id, Playlist.id, Playlist.name)
            .join(Playlist, Playlist.id == PlaylistSong.playlist_id)
            .order_by(Playlist.name.asc())
        )
        playlists_by_song: dict[int, list[dict]] = {}
        for song_id, playlist_id, playlist_name in relations_result.all():
            playlists_by_song.setdefault(song_id, []).append({"id": playlist_id, "name": playlist_name})
        return [
            {
                "id": song.id,
                "title": song.title,
                "subtitle": song.author or song.original_artist or "Unknown artist",
                "author": song.author,
                "original_artist": song.original_artist,
                "channel_name": song.author or song.original_artist or "Unknown artist",
                "has_cover": bool(song.cover_file_id),
                "playlists": playlists_by_song.get(song.id, []),
            }
            for song in songs_result.scalars().all()
        ]

    async def selection(
        self,
        db: AsyncSession,
        selection_mode: str,
        selector: dict,
    ) -> dict:
        if selection_mode == "all":
            return {}
        if selection_mode != "selected":
            raise HTTPException(status_code=422, detail="Invalid selection mode")

        song_ids = selector.get("song_ids")
        if not isinstance(song_ids, list) or not song_ids:
            raise HTTPException(status_code=422, detail="Select at least one song")
        if len(song_ids) > MAX_SHARED_SONGS:
            raise HTTPException(
                status_code=422,
                detail=f"A share may contain at most {MAX_SHARED_SONGS} songs",
            )

        normalized_ids: list[int] = []
        for song_id in song_ids:
            if isinstance(song_id, bool):
                raise HTTPException(status_code=422, detail="Invalid song ID")
            if isinstance(song_id, str):
                if not song_id.isascii() or not song_id.isdigit():
                    raise HTTPException(status_code=422, detail="Invalid song ID")
                song_id = int(song_id)
            if not isinstance(song_id, int) or song_id < 1:
                raise HTTPException(status_code=422, detail="Invalid song ID")
            if song_id not in normalized_ids:
                normalized_ids.append(song_id)

        result = await db.execute(select(Song.id).where(Song.id.in_(normalized_ids)))
        found_ids = set(result.scalars().all())
        if missing := set(normalized_ids) - found_ids:
            missing_list = ", ".join(str(song_id) for song_id in sorted(missing))
            raise HTTPException(status_code=422, detail=f"Songs not found: {missing_list}")
        return {"song_ids": normalized_ids}

    async def entities(
        self,
        request: Request,
        share,
        db: AsyncSession,
        route: ShareRoute,
        params: dict,
    ):
        if route.name != "songs":
            raise HTTPException(status_code=404, detail="Shared entity route not found")

        songs = await _selected_songs(db, share)
        search = (request.query_params.get("search") or "").strip()
        if search:
            search_pattern = f"%{search}%"
            scoped_ids = [song.id for song in songs]
            result = await db.execute(
                select(Song)
                .where(
                    Song.id.in_(scoped_ids),
                    or_(
                        Song.title.ilike(search_pattern),
                        Song.author.ilike(search_pattern),
                        Song.original_artist.ilike(search_pattern),
                    ),
                )
                .order_by(Song.created_at.desc())
            )
            songs = list(result.scalars().all())
        return [_serialize_song(song, share) for song in songs]

    async def relations(
        self,
        request: Request,
        share,
        db: AsyncSession,
        route: ShareRoute,
        params: dict,
    ):
        if route.name == "playlists":
            return await _shared_playlists(db, share)
        if route.name == "playlist_songs":
            try:
                playlist_id = int(params["playlist_id"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=404, detail="Shared playlist not found")
            playlist = next(
                (item for item in await _shared_playlists(db, share) if item["id"] == playlist_id),
                None,
            )
            if playlist is None:
                raise HTTPException(status_code=404, detail="Shared playlist not found")
            songs = {song.id: song for song in await _selected_songs(db, share)}
            return [
                _serialize_song(songs[song_id], share) for song_id in playlist["songs"] if song_id in songs
            ]
        raise HTTPException(status_code=404, detail="Shared relation route not found")

    async def asset(
        self,
        request: Request,
        share,
        db: AsyncSession,
        asset: ShareAsset,
        params: dict,
    ):
        try:
            song_id = int(params["song_id"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=404, detail="Shared content not found")
        song = await _get_scoped_song(db, share, song_id)

        if asset.name == "audio":
            response = serve_media_stream(request, song.audio_file_id)
        elif asset.name == "cover" and song.cover_file_id:
            response = serve_storage_file_chunked(song.cover_file_id)
        else:
            raise HTTPException(status_code=404, detail="Shared content not found")

        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


PROVIDER = MusicShareProvider()
