from io import BytesIO

from PIL import Image, ImageFilter, ImageOps

from app.core.storage import get_storage

GRID_SIZE = 3
TILE_SIZE = 160


def regenerate_playlist_cover(playlist, source_paths: list[str]) -> str | None:
    """Build a square WebP mosaic from the playlist's available song covers."""
    storage = get_storage()
    images = []
    for path in source_paths:
        try:
            with storage.get_file_stream(path) as stream:
                with Image.open(BytesIO(stream.read())) as image:
                    images.append(image.convert("RGB"))
        except (FileNotFoundError, OSError):
            continue

    cover_path = f"music/playlist-covers/{playlist.id}.webp"
    if not images:
        storage.delete_file(cover_path)
        return None

    cover = Image.new("RGB", (GRID_SIZE * TILE_SIZE, GRID_SIZE * TILE_SIZE), "#09090b")
    for index in range(GRID_SIZE * GRID_SIZE):
        tile = ImageOps.fit(
            images[index % len(images)], (TILE_SIZE, TILE_SIZE), method=Image.Resampling.LANCZOS
        )
        tile = tile.filter(ImageFilter.GaussianBlur(radius=0.65))
        cover.paste(
            Image.blend(tile, Image.new("RGB", tile.size, "black"), 0.32),
            ((index % GRID_SIZE) * TILE_SIZE, (index // GRID_SIZE) * TILE_SIZE),
        )

    output = BytesIO()
    cover.save(output, format="WEBP", quality=85, method=6)
    return storage.save_file(output.getvalue(), cover_path)
