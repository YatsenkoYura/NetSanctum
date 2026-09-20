import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.storage import LocalStorage
from app.modules.video_archiver import compression, router as video_router
from app.modules.video_archiver.models import (
    ArchivedVideo,
    VideoChannel,
    VideoPlaylist,
    video_playlist_association,
)


class VideoCompressionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        for table in (
            VideoChannel.__table__,
            VideoPlaylist.__table__,
            ArchivedVideo.__table__,
            video_playlist_association,
        ):
            table.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.storage_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage_directory.cleanup)
        self.storage = LocalStorage(self.storage_directory.name)
        self.session_patch = patch.object(compression, "SyncSessionLocal", self.session_factory)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def add_video(
        self,
        video_id: str,
        *,
        file_path: str | None = None,
        compression_status: str | None = None,
        compression_profile: str | None = None,
    ) -> None:
        with self.session_factory() as session:
            session.add(
                ArchivedVideo(
                    id=video_id,
                    title=video_id,
                    platform="upload",
                    channel_name="Local",
                    duration=10,
                    resolution="Original",
                    file_path=file_path,
                    status="completed",
                    compression_status=compression_status,
                    compression_profile=compression_profile,
                )
            )
            session.commit()

    def get_video(self, video_id: str) -> ArchivedVideo:
        with self.session_factory() as session:
            return session.get(ArchivedVideo, video_id)

    def test_selection_excludes_terminal_current_profile(self):
        self.add_video("new", file_path="new.mp4")
        self.add_video(
            "done",
            file_path="done.mp4",
            compression_status="completed",
            compression_profile=compression.COMPRESSION_PROFILE,
        )
        self.add_video(
            "skipped",
            file_path="skipped.mp4",
            compression_status="skipped",
            compression_profile=compression.COMPRESSION_PROFILE,
        )
        self.add_video(
            "failed",
            file_path="failed.mp4",
            compression_status="failed",
            compression_profile=compression.COMPRESSION_PROFILE,
        )
        self.add_video(
            "legacy", file_path="legacy.mp4", compression_status="completed", compression_profile="v0"
        )
        self.add_video("missing")

        with self.session_factory() as session:
            selected = set(compression.compression_candidate_ids(session))

        self.assertEqual({"new", "failed", "legacy"}, selected)

    def test_ffmpeg_uses_fixed_profile_and_optional_audio(self):
        captured = []

        class Process:
            returncode = 0

            def poll(self):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mkv"
            output = Path(directory) / "output.mp4"
            with patch.object(
                compression.subprocess,
                "Popen",
                side_effect=lambda command, **kwargs: captured.append(command) or Process(),
            ):
                compression.run_ffmpeg_compression(source, output, lambda: False)

        command = captured[0]
        self.assertIn(
            ["-map", "0:v:0", "-map", "0:a?"], [command[index : index + 4] for index in range(len(command))]
        )
        self.assertIn(
            ["-crf", "27", "-preset", "slow"], [command[index : index + 4] for index in range(len(command))]
        )
        self.assertIn(
            ["-c:a", "aac", "-b:a", "128k"], [command[index : index + 4] for index in range(len(command))]
        )
        self.assertIn("yuv420p", command)
        self.assertIn("+faststart", command)

    def test_smaller_output_switches_path_and_deletes_original(self):
        original_path = "video_archiver/videos/original.mp4"
        original = b"a" * 100
        self.storage.save_file(original, original_path)
        self.add_video("smaller", file_path=original_path)

        def write_smaller(input_path: Path, output_path: Path, should_cancel):
            output_path.write_bytes(b"b" * 40)

        with (
            patch.object(compression, "run_ffmpeg_compression", side_effect=write_smaller),
            patch.object(compression, "_probe_media", return_value=(10.0, True)),
        ):
            result = compression.optimize_video("smaller", storage=self.storage)

        video = self.get_video("smaller")
        self.assertEqual("completed", result)
        self.assertEqual("completed", video.compression_status)
        self.assertEqual(compression.COMPRESSION_PROFILE, video.compression_profile)
        self.assertEqual(40, video.file_size)
        self.assertEqual(hashlib.sha256(b"b" * 40).hexdigest(), video.sha256)
        self.assertNotEqual(original_path, video.file_path)
        self.assertFalse(self.storage.file_exists(original_path))
        self.assertTrue(self.storage.file_exists(video.file_path))

    def test_larger_output_keeps_original_and_marks_skipped(self):
        original_path = "video_archiver/videos/original.mp4"
        original = b"a" * 50
        self.storage.save_file(original, original_path)
        self.add_video("larger", file_path=original_path)

        def write_larger(input_path: Path, output_path: Path, should_cancel):
            output_path.write_bytes(b"b" * 50)

        with (
            patch.object(compression, "run_ffmpeg_compression", side_effect=write_larger),
            patch.object(compression, "_probe_media", return_value=(10.0, True)),
        ):
            result = compression.optimize_video("larger", storage=self.storage)

        video = self.get_video("larger")
        self.assertEqual("skipped", result)
        self.assertEqual(original_path, video.file_path)
        self.assertEqual("skipped", video.compression_status)
        self.assertEqual(50, video.file_size)
        self.assertEqual(hashlib.sha256(original).hexdigest(), video.sha256)
        self.assertTrue(self.storage.file_exists(original_path))

    def test_ffmpeg_error_preserves_original_and_records_error(self):
        original_path = "video_archiver/videos/original.mp4"
        self.storage.save_file(b"original", original_path)
        self.add_video("error", file_path=original_path)

        with (
            patch.object(compression, "run_ffmpeg_compression", side_effect=RuntimeError("encoder failed")),
            patch.object(compression, "_probe_media", return_value=(10.0, True)),
        ):
            result = compression.optimize_video("error", storage=self.storage)

        video = self.get_video("error")
        self.assertEqual("failed", result)
        self.assertEqual(original_path, video.file_path)
        self.assertEqual("failed", video.compression_status)
        self.assertIn("encoder failed", video.compression_error)
        self.assertTrue(self.storage.file_exists(original_path))

    def test_concurrent_path_change_prevents_switch_and_deletion(self):
        original_path = "video_archiver/videos/original.mp4"
        self.storage.save_file(b"a" * 100, original_path)
        self.add_video("raced", file_path=original_path)

        def race_with_update(input_path: Path, output_path: Path, should_cancel):
            output_path.write_bytes(b"b" * 20)
            with self.session_factory() as session:
                video = session.get(ArchivedVideo, "raced")
                video.file_path = "video_archiver/videos/replacement.mp4"
                session.commit()

        with (
            patch.object(compression, "run_ffmpeg_compression", side_effect=race_with_update),
            patch.object(compression, "_probe_media", return_value=(10.0, True)),
        ):
            result = compression.optimize_video("raced", storage=self.storage)

        self.assertEqual("stale", result)
        self.assertEqual("video_archiver/videos/replacement.mp4", self.get_video("raced").file_path)
        self.assertTrue(self.storage.file_exists(original_path))
        optimized = self.storage._full_path("video_archiver/videos/optimized")
        self.assertFalse(optimized.exists() and any(optimized.iterdir()))

    def test_redelivery_finishes_committed_storage_switch(self):
        old_path = "video_archiver/videos/original.mp4"
        new_path = "video_archiver/videos/optimized/new.mp4"
        self.storage.save_file(b"original", old_path)
        self.storage.save_file(b"optimized", new_path)
        self.add_video(
            "switching",
            file_path=new_path,
            compression_status="switching",
            compression_profile=compression.COMPRESSION_PROFILE,
        )
        with self.session_factory() as session:
            video = session.get(ArchivedVideo, "switching")
            video.compression_error = f"old_path:{old_path}"
            session.commit()

        result = compression.optimize_video("switching", storage=self.storage)

        video = self.get_video("switching")
        self.assertEqual("completed", result)
        self.assertEqual("completed", video.compression_status)
        self.assertIsNone(video.compression_error)
        self.assertFalse(self.storage.file_exists(old_path))
        self.assertTrue(self.storage.file_exists(new_path))

    def test_finalization_db_error_keeps_both_objects(self):
        old_path = "video_archiver/videos/original.mp4"
        new_path = "video_archiver/videos/optimized/new.mp4"
        self.storage.save_file(b"original", old_path)
        self.storage.save_file(b"optimized", new_path)
        self.add_video(
            "finalize-error",
            file_path=new_path,
            compression_status="switching",
            compression_profile=compression.COMPRESSION_PROFILE,
        )
        with self.session_factory() as session:
            video = session.get(ArchivedVideo, "finalize-error")
            video.compression_error = f"old_path:{old_path}"
            session.commit()

        failing_session = self.session_factory()
        with (
            patch.object(compression, "SyncSessionLocal", return_value=failing_session),
            patch.object(failing_session, "commit", side_effect=RuntimeError("database unavailable")),
            self.assertRaisesRegex(RuntimeError, "database unavailable"),
        ):
            compression._finish_switch("finalize-error", new_path, old_path, self.storage)

        self.assertTrue(self.storage.file_exists(old_path))
        self.assertTrue(self.storage.file_exists(new_path))

    def test_owned_output_is_not_deleted_when_finalization_fails(self):
        old_path = "video_archiver/videos/original.mp4"
        self.storage.save_file(b"a" * 100, old_path)
        self.add_video("owned", file_path=old_path)

        def write_smaller(input_path: Path, output_path: Path, should_cancel):
            output_path.write_bytes(b"b" * 20)

        with (
            patch.object(compression, "run_ffmpeg_compression", side_effect=write_smaller),
            patch.object(compression, "_probe_media", return_value=(10.0, True)),
            patch.object(compression, "_finish_switch", side_effect=RuntimeError("commit failed")),
        ):
            result = compression.optimize_video("owned", storage=self.storage)

        video = self.get_video("owned")
        self.assertEqual("failed", result)
        self.assertEqual("switching", video.compression_status)
        self.assertNotEqual(old_path, video.file_path)
        self.assertTrue(self.storage.file_exists(old_path))
        self.assertTrue(self.storage.file_exists(video.file_path))

    def test_each_encode_attempt_uses_a_unique_output_path(self):
        old_path = "video_archiver/videos/original.mp4"
        self.storage.save_file(b"a" * 100, old_path)
        self.add_video("unique", file_path=old_path)
        saved_paths = []
        save_file_from_path = self.storage.save_file_from_path

        def record_save(source_path, storage_path):
            saved_paths.append(storage_path)
            return save_file_from_path(source_path, storage_path)

        def write_smaller(input_path: Path, output_path: Path, should_cancel):
            output_path.write_bytes(b"b" * 20)

        with (
            patch.object(self.storage, "save_file_from_path", side_effect=record_save),
            patch.object(compression, "run_ffmpeg_compression", side_effect=write_smaller),
            patch.object(compression, "_probe_media", return_value=(10.0, True)),
            patch.object(compression, "_set_result", return_value=False),
        ):
            self.assertEqual("stale", compression.optimize_video("unique", storage=self.storage))
            self.assertEqual("stale", compression.optimize_video("unique", storage=self.storage))

        self.assertEqual(2, len(saved_paths))
        self.assertNotEqual(saved_paths[0], saved_paths[1])


class VideoCompressionApiTests(unittest.TestCase):
    def test_duplicate_batch_is_rejected_before_dispatch(self):
        class Redis:
            async def set(self, key, value, **kwargs):
                return False

            async def get(self, key):
                return "existing-task"

        class Task:
            def apply_async(self, **kwargs):
                raise AssertionError("duplicate task must not be dispatched")

        with (
            patch.object(video_router, "redis_client", Redis()),
            patch.object(video_router, "compress_all_videos_task", Task()),
            self.assertRaises(HTTPException) as raised,
        ):
            asyncio.run(video_router.compress_all(user=object()))

        self.assertEqual(409, raised.exception.status_code)

    def test_same_task_id_can_exchange_reservation_only_once(self):
        class Redis:
            lock_value = "task-id"

            def eval(self, script, key_count, lock_key, task_id, lease_token, ttl):
                if self.lock_value == task_id or self.lock_value is None:
                    self.lock_value = lease_token
                    return 1
                return 0

        redis = Redis()

        first_lease = compression._acquire_compression_lease("task-id", redis)
        second_lease = compression._acquire_compression_lease("task-id", redis)

        self.assertIsNotNone(first_lease)
        self.assertTrue(first_lease.startswith("task-id:"))
        self.assertIsNone(second_lease)
        self.assertEqual(first_lease, redis.lock_value)

    def test_duplicate_delivery_does_not_overwrite_tracker(self):
        class Redis:
            def eval(self, *args):
                return 0

            def setex(self, *args):
                raise AssertionError("duplicate delivery must not update the active tracker")

        result = compression.run_compression_batch("task-id", Redis())

        self.assertEqual("Another delivery already owns the video optimization lease.", result)
        self.assertEqual(compression.COMPRESSION_LOCK_TTL, compression.COMPRESSION_TRACKER_TTL)


if __name__ == "__main__":
    unittest.main()
