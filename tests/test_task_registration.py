"""A Celery decorator that lands on the wrong function must fail loudly here.

The Vault video download stopped being queued because `@celery_app.task(bind=True)`
had been attached to a plain helper above it instead of to the task, leaving
`download_vault_video_task` an ordinary function. The dispatcher swallowed the
`AttributeError` and only logged a warning, so the card silently stayed "not
downloaded" with no visible failure anywhere.

This walks every task name the modules dispatch and asserts each one is a real
registered task.

Only bundled modules are listed. A private module is installed per deployment
and is not in the repository, so naming one here made this test depend on a
checkout it cannot assume: it passed on the machine that still had the module
and failed in the image built from a clean one.
"""

import importlib
import unittest

DISPATCHED = {
    "app.modules.alllib.tasks": ["download_lib_task"],
    "app.modules.music.tasks": ["process_youtube_url_task", "convert_archived_video_task"],
    "app.modules.vault.tasks": ["download_vault_video_task"],
    "app.modules.video_archiver.tasks": [
        "download_video_task",
        "process_video_url_task",
        "sync_video_metadata_task",
        "sync_all_videos_task",
        "youtube_oauth2_task",
    ],
}


class DispatchedTasksAreRegisteredTests(unittest.TestCase):
    def test_every_dispatched_name_is_importable(self):
        """A module that cannot be imported would otherwise check nothing."""
        broken = []
        for module_name in DISPATCHED:
            try:
                importlib.import_module(module_name)
            except ImportError as error:
                broken.append(f"{module_name}: {error}")
        self.assertEqual([], broken, "\n".join(broken))

    def test_every_dispatched_name_is_a_celery_task(self):
        broken = []
        for module_name, attributes in DISPATCHED.items():
            module = importlib.import_module(module_name)
            for attribute in attributes:
                target = getattr(module, attribute, None)
                if not hasattr(target, "apply_async"):
                    kind = type(target).__name__
                    broken.append(f"{module_name}.{attribute} is a {kind}, not a task")
        self.assertEqual([], broken, "\n".join(broken))

    def test_a_task_carries_the_name_it_was_registered_under(self):
        from app.modules.vault.tasks import download_vault_video_task

        self.assertEqual("app.modules.vault.tasks.download_vault_video_task", download_vault_video_task.name)

    def test_a_plain_helper_is_not_turned_into_a_task(self):
        """The bug's other half: a helper must not pick up the decorator."""
        from app.modules.vault.tasks import _collection_is_sealed

        self.assertFalse(hasattr(_collection_is_sealed, "apply_async"))

    def test_a_bound_task_is_callable_without_a_self_argument(self):
        """`bind=True` hands the task instance in, so `self` is not part of the
        public signature. Losing the decorator changes both of these."""
        import inspect

        from app.modules.vault.tasks import download_vault_video_task

        parameters = list(inspect.signature(download_vault_video_task.run).parameters)
        self.assertNotIn("self", parameters)
        for expected in ("item_id", "url", "quality", "title"):
            self.assertIn(expected, parameters)


if __name__ == "__main__":
    unittest.main()
