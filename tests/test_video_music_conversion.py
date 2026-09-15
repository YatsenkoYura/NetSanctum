import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.core.module_types import IntegrationContext
from app.modules.music import integrations, tasks
from app.modules.music.router import get_conversion_status
from app.modules.video_archiver.capabilities import resolve_entity
from app.modules.video_archiver.router import convert_playlist_to_music


class FakeStorage:
    def __init__(self, files):
        self.files = files

    def get_file_stream(self, path):
        return io.BytesIO(self.files[path])


class FakeProcess:
    def __init__(self, command, **kwargs):
        self.command = command
        self.stdout = iter(["out_time_us=5000000\n", "progress=end\n"])
        Path(command[-1]).write_bytes(b"mp3")

    def wait(self):
        return 0


class VideoMusicConversionTests(unittest.TestCase):
    def test_video_entity_exposes_internal_archive_resource(self):
        video = SimpleNamespace(
            id="upload-1",
            title="Local video",
            description="Description",
            channel_name="Local uploads",
            duration=10,
            platform="upload",
            thumbnail_path="video_archiver/thumbnails/upload-1.jpg",
            file_path="video_archiver/videos/upload-1.mkv",
            status="completed",
        )
        db = SimpleNamespace(get=AsyncMock(return_value=video))

        entity = asyncio.run(resolve_entity(db, "video", video.id))

        assert entity is not None
        self.assertEqual(video.file_path, entity["storage_path"])
        self.assertEqual(video.thumbnail_path, entity["thumbnail_storage_path"])
        self.assertEqual("Local uploads", entity["author"])

    def test_audio_import_dispatches_local_ffmpeg_conversion(self):
        entity = {
            "title": "Archived track",
            "author": "Channel",
            "duration": 30,
            "storage_path": "video_archiver/videos/video.mkv",
            "thumbnail_storage_path": "video_archiver/thumbnails/video.jpg",
            "source_url": "https://www.youtube.com/watch?v=abcdefghijk",
        }
        registry = SimpleNamespace(resolve_entity=AsyncMock(return_value=entity))
        context = IntegrationContext(session=None, user=None, registry=registry)
        dispatched = SimpleNamespace(id="conversion-task")

        with patch.object(
            integrations,
            "dispatch_tracked_async",
            AsyncMock(return_value=dispatched),
        ) as dispatch:
            result = asyncio.run(
                integrations.import_entity_audio(
                    integrations.ImportEntityAudioRequest(entity_type="video", entity_id="video-1"),
                    context,
                )
            )

        self.assertEqual("/music/api/conversions/conversion-task", result.status_url)
        self.assertIs(tasks.convert_archived_video_task, dispatch.call_args.args[0])
        self.assertEqual("music_convert", dispatch.call_args.args[2])
        self.assertEqual(entity["storage_path"], dispatch.call_args.kwargs["kwargs"]["storage_path"])

    def test_ffmpeg_extraction_reports_media_progress(self):
        progress = []
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "audio.mp3"
            with (
                patch.object(tasks, "get_storage", return_value=FakeStorage({"video.mkv": b"video"})),
                patch.object(tasks.subprocess, "Popen", side_effect=FakeProcess) as popen,
            ):
                tasks._extract_archived_audio("video.mkv", output_path, 10, progress.append)

            self.assertEqual(b"mp3", output_path.read_bytes())

        command = popen.call_args.args[0]
        self.assertIn("libmp3lame", command)
        self.assertIn("-progress", command)
        self.assertEqual([50], progress)

    def test_ffmpeg_probes_duration_when_archive_metadata_is_missing(self):
        progress = []
        probe = SimpleNamespace(stdout="10.0\n")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "audio.mp3"
            with (
                patch.object(tasks, "get_storage", return_value=FakeStorage({"video.mkv": b"video"})),
                patch.object(tasks.subprocess, "run", return_value=probe) as run,
                patch.object(tasks.subprocess, "Popen", side_effect=FakeProcess),
            ):
                tasks._extract_archived_audio("video.mkv", output_path, 0, progress.append)

        self.assertEqual("ffprobe", run.call_args.args[0][0])
        self.assertEqual([50], progress)

    def test_conversion_status_returns_persisted_result(self):
        payload = {
            "task_id": "task-1",
            "state": "completed",
            "progress": "100%",
            "song_id": 7,
            "audio_url": "/music/audio/7",
        }
        client = SimpleNamespace(get=AsyncMock(return_value=json.dumps(payload)))
        with patch("app.modules.music.router.redis_client", client):
            result = asyncio.run(get_conversion_status("task-1", user=object()))
        self.assertEqual(payload, result)

    def test_conversion_status_rejects_invalid_task_id(self):
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(get_conversion_status("../secret", user=object()))
        self.assertEqual(404, raised.exception.status_code)

    def test_playlist_conversion_queues_each_archived_file(self):
        playlist = SimpleNamespace(id=4)
        videos = [
            SimpleNamespace(id="video-1", status="completed", file_path="one.mp4"),
            SimpleNamespace(id="video-2", status="completed", file_path="two.mkv"),
            SimpleNamespace(id="pending", status="downloading", file_path=None),
        ]
        db = SimpleNamespace(get=AsyncMock(return_value=playlist))
        registry = SimpleNamespace(
            invoke_integration=AsyncMock(
                side_effect=[
                    {"task_id": "task-1", "status_url": "/music/api/conversions/task-1"},
                    {"task_id": "task-2", "status_url": "/music/api/conversions/task-2"},
                ]
            )
        )
        with (
            patch(
                "app.modules.video_archiver.router.PlaylistService.get_playlist_videos",
                AsyncMock(return_value=videos),
            ),
            patch("app.modules.video_archiver.router.module_registry", registry),
        ):
            result = asyncio.run(convert_playlist_to_music(4, db=db, user=object()))

        self.assertEqual(["task-1", "task-2"], result["task_ids"])
        self.assertEqual(1, result["skipped"])
        entity_ids = [call.args[1]["entity_id"] for call in registry.invoke_integration.call_args_list]
        self.assertEqual(["video-1", "video-2"], entity_ids)


if __name__ == "__main__":
    unittest.main()
