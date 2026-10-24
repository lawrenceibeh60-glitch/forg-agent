"""Regression tests for restart-safe agent task checkpoints."""
import tempfile
import unittest
from pathlib import Path

import tasks


class TaskCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = tasks._DB
        tasks._DB = Path(self.temp.name) / "tasks.sqlite3"

    def tearDown(self):
        tasks._DB = self.old_db
        self.temp.cleanup()

    def test_task_state_round_trips_between_database_connections(self):
        task = tasks.create("session-1", "Build a working application")
        task_id = task["id"]
        tasks.update(
            task_id,
            plan=["inspect", "implement", "test"],
            completed=["inspect"],
            current_step="implement",
            checkpoint={"turn": 4, "last_tool": "read_file"},
        )

        # Each read opens a fresh SQLite connection, like a later request.
        loaded = tasks.get(task_id)
        self.assertEqual(loaded["goal"], "Build a working application")
        self.assertEqual(loaded["plan"], ["inspect", "implement", "test"])
        self.assertEqual(loaded["completed"], ["inspect"])
        self.assertEqual(loaded["current_step"], "implement")
        self.assertEqual(loaded["checkpoint"], {"turn": 4, "last_tool": "read_file"})

    def test_status_validation_and_missing_task(self):
        task = tasks.create("session-2", "Test failure recovery")
        with self.assertRaises(ValueError):
            tasks.update(task["id"], status="magically_done")
        self.assertIsNone(tasks.get("missing"))

    def test_tasks_are_scoped_to_session_and_listed_newest_first(self):
        first = tasks.create("session-a", "first")
        second = tasks.create("session-a", "second")
        tasks.create("session-b", "unrelated")
        self.assertEqual(
            {item["id"] for item in tasks.list_for_session("session-a", 10)},
            {first["id"], second["id"]},
        )
        self.assertEqual(tasks.latest_for_session("session-a")["id"], second["id"])

    def test_recover_interrupted_tasks_marks_them_resumable(self):
        active = tasks.create("session-restart", "Continue after restart")
        waiting = tasks.create("session-restart", "Waiting for approval")
        tasks.update(waiting["id"], status="waiting_approval")
        recovered = tasks.recover_interrupted()
        self.assertEqual(recovered, 2)
        for task_id in (active["id"], waiting["id"]):
            loaded = tasks.get(task_id)
            self.assertEqual(loaded["status"], "blocked")
            self.assertEqual(loaded["checkpoint"]["phase"], "interrupted")
            self.assertIn("restarted", loaded["last_error"])

    def test_conversation_round_trips_as_durable_session_data(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Build the feature"},
            {"role": "assistant", "content": "I will inspect the files."},
        ]
        tasks.save_session("session-memory", messages)
        self.assertEqual(tasks.load_session("session-memory"), messages)
        self.assertIsNone(tasks.load_session("missing-session"))

    def test_recovery_can_skip_recently_updated_tasks(self):
        task = tasks.create("session-live", "Do not steal active work")
        tasks.update(task["id"], status="running", current_step="working")
        self.assertEqual(tasks.recover_interrupted(stale_after=3600), 0)
        self.assertEqual(tasks.get(task["id"])["status"], "running")

    def test_health_reports_sqlite_backend(self):
        health = tasks.health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["backend"], "sqlite")
        self.assertIn("persistent storage", health["durability_note"])


if __name__ == "__main__":
    unittest.main()
