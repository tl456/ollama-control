"""Pure-Mock tray exit cleanup; no window, tray, HTTP or process operation."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix="v15-tray-exit-import-")
if "app.config" not in sys.modules:
    os.environ["LOCAL_AI_ROOT"] = _IMPORT_ROOT.name
os.environ["LOCAL_AI_DISABLE_GPU_PROBE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.tray import Tray


class TrayExitTests(unittest.TestCase):
    def fixture(self):
        order = []
        tray = Tray("http://127.0.0.1:1", Mock(), Mock(side_effect=lambda: order.append("on_exit")))
        tray.request = Mock(side_effect=lambda path: order.append("stop-all"))
        tray.window = Mock()
        tray.window.destroy.side_effect = lambda: order.append("window")
        tray.icon = Mock()
        tray.icon.stop.side_effect = lambda: order.append("icon")
        return tray, order

    def assert_finished_once(self, tray):
        self.assertTrue(tray.quitting)
        self.assertTrue(tray.done.is_set())
        tray.request.assert_called_once_with("/api/ui/stop-all")
        tray.window.destroy.assert_called_once_with()
        tray.icon.stop.assert_called_once_with()
        tray.on_exit.assert_called_once_with()
        tray.exit()
        tray.request.assert_called_once()
        tray.window.destroy.assert_called_once()
        tray.icon.stop.assert_called_once()
        tray.on_exit.assert_called_once()

    def test_normal_exit_order_and_duplicate_exit_are_stable(self):
        tray, order = self.fixture()
        tray.exit()
        self.assertEqual(order, ["stop-all", "window", "icon", "on_exit"])
        self.assert_finished_once(tray)

    def test_on_exit_import_failure_still_sets_done(self):
        tray, order = self.fixture()
        tray.on_exit.side_effect = ImportError("SYNTHETIC mixed-version import")
        with patch("app.tray.logging.exception") as error:
            tray.exit()
        self.assertEqual(order, ["stop-all", "window", "icon"])
        error.assert_called_once()
        self.assert_finished_once(tray)

    def test_window_failure_does_not_skip_icon_or_owned_backend(self):
        tray, order = self.fixture()
        tray.window.destroy.side_effect = RuntimeError("SYNTHETIC window cleanup failure")
        with patch("app.tray.logging.exception") as error:
            tray.exit()
        self.assertEqual(order, ["stop-all", "icon", "on_exit"])
        error.assert_called_once()
        self.assert_finished_once(tray)

    def test_icon_failure_does_not_skip_owned_backend(self):
        tray, order = self.fixture()
        tray.icon.stop.side_effect = RuntimeError("SYNTHETIC icon cleanup failure")
        with patch("app.tray.logging.exception") as error:
            tray.exit()
        self.assertEqual(order, ["stop-all", "window", "on_exit"])
        error.assert_called_once()
        self.assert_finished_once(tray)

    def test_all_cleanup_failures_still_attempt_every_step_and_finish(self):
        tray, _ = self.fixture()
        tray.request.side_effect = RuntimeError("SYNTHETIC request failure")
        tray.window.destroy.side_effect = RuntimeError("SYNTHETIC window failure")
        tray.icon.stop.side_effect = RuntimeError("SYNTHETIC icon failure")
        tray.on_exit.side_effect = ImportError("SYNTHETIC callback failure")
        with patch("app.tray.logging.exception") as error:
            tray.exit()
        self.assertEqual(error.call_count, 4)
        self.assert_finished_once(tray)

    def test_reentrant_exit_does_not_duplicate_cleanup(self):
        tray, order = self.fixture()
        def close_window():
            order.append("window")
            tray.exit()
        tray.window.destroy.side_effect = close_window
        tray.exit()
        self.assertEqual(order, ["stop-all", "window", "icon", "on_exit"])
        self.assert_finished_once(tray)


if __name__ == "__main__":
    unittest.main()
