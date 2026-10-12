import asyncio
import unittest
from unittest.mock import Mock, patch

from backend.jobs import jvm_refresh_job
from backend.jobs import runner


class JvmRefreshJobTests(unittest.TestCase):
    def test_refresh_runs_existing_cli_and_reports_success(self):
        with patch.object(runner, "update_phase") as update_phase, \
             patch.object(jvm_refresh_job, "_run_refresh_cli",
                          return_value=(0, "dump complete")) as refresh:
            result = asyncio.run(jvm_refresh_job.run_jvm_refresh_job("job-1", None, {}))
        self.assertEqual(result, {"ok": True})
        update_phase.assert_called_once_with("job-1", "refreshing_snapshot")
        script, root = refresh.call_args.args
        self.assertEqual(script.name, "jvm_debug_direct.py")
        self.assertEqual(script.parent.name, "scripts")
        self.assertEqual(script.parent.parent, root)

    def test_refresh_job_cannot_be_cancelled(self):
        task = Mock()

        class _Store:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def get_job(self, _job_id):
                return {"kind": "jvm_refresh", "status": "running"}

        with patch.object(runner, "TASKS", {"job-1": task}), \
             patch.object(runner, "Store", _Store):
            self.assertFalse(runner.cancel("job-1"))
        task.cancel.assert_not_called()

    def test_failed_refresh_preserves_cli_output_in_job_error(self):
        with patch.object(runner, "update_phase"), \
             patch.object(jvm_refresh_job, "_run_refresh_cli",
                          return_value=(1, "Gradle failed\nsnapshot not published")):
            with self.assertRaisesRegex(RuntimeError, "Gradle failed\nsnapshot not published"):
                asyncio.run(jvm_refresh_job.run_jvm_refresh_job("job-1", None, {}))


if __name__ == "__main__":
    unittest.main()
