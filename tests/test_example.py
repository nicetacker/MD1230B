from contextlib import redirect_stdout
import io
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import example
from md1230b import PortAddress


class ExampleTests(unittest.TestCase):
    def run_example(self, destination, fail=False):
        command_logger = logging.getLogger("md1230b.commands")

        def connected():
            command_logger.info("TX analyzer:5000 *IDN?")

        def disconnected(*args):
            command_logger.info("TX analyzer:5000 :PORT:OWNership:CLEar")
            return False

        output = io.StringIO()
        with redirect_stdout(output), patch.object(example.logging, "basicConfig"), \
                patch.object(example, "Md1230b") as session, \
                patch.object(example, "bidirectional_test", return_value={}) as scenario:
            session.return_value.__enter__.side_effect = connected
            session.return_value.__exit__.side_effect = disconnected
            if fail:
                scenario.side_effect = RuntimeError("scenario failure")
            argv = ["analyzer", "--unit", "1", "--module", "2", "--port-a", "3",
                    "--port-b", "4", "--command-log", str(destination)]
            if fail:
                with self.assertRaisesRegex(RuntimeError, "scenario failure"):
                    example.main(argv)
            else:
                example.main(argv)
            session.assert_called_once_with("analyzer", {
                "PortA": PortAddress(1, 2, 3), "PortB": PortAddress(1, 2, 4),
            }, tcp_port=5000)
        return output.getvalue()

    def test_command_history_goes_to_stdout(self):
        output = self.run_example("-")
        self.assertIn("TX analyzer:5000 *IDN?", output)
        self.assertIn("TX analyzer:5000 :PORT:OWNership:CLEar", output)

    def test_file_history_appends_and_includes_cleanup_after_failure(self):
        logger = logging.getLogger("md1230b.commands")
        previous = (list(logger.handlers), logger.level, logger.propagate)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "コマンド.log"
            for fail in (False, True):
                output = self.run_example(path, fail=fail)
                self.assertNotIn("TX analyzer", output)
                self.assertEqual((logger.handlers, logger.level, logger.propagate), previous)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("TX analyzer:5000 *IDN?"), 2)
            self.assertEqual(text.count("TX analyzer:5000 :PORT:OWNership:CLEar"), 2)


if __name__ == "__main__":
    unittest.main()
