import faulthandler
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from majd_studio_3d import logging_setup


class StreamToLoggerTests(unittest.TestCase):
    def test_forwards_complete_lines_including_carriage_returns(self):
        logger = logging.getLogger("test.stream")
        stream = logging_setup.StreamToLogger(logger, logging.INFO)
        with self.assertLogs(logger, logging.INFO) as captured:
            stream.write("first part ")
            stream.write("done\nprogress 10%\rprogress 20%\r")
            stream.writelines(["tail ", "without newline"])
            stream.flush()
        self.assertEqual([record.getMessage() for record in captured.records],
                         ["first part done", "progress 10%", "progress 20%", "tail without newline"])
        with self.assertRaises(OSError):
            stream.fileno()
        self.assertTrue(stream.writable())
        self.assertFalse(stream.closed or stream.isatty() or stream.readable() or stream.seekable())


class ConfigureLoggingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.root = Path(temp.name)
        saved = (sys.stdout, sys.stderr, sys.excepthook, threading.excepthook)
        handlers = list(logging.getLogger().handlers)
        level = logging.getLogger().level

        def restore():
            sys.stdout, sys.stderr, sys.excepthook, threading.excepthook = saved
            for handler in list(logging.getLogger().handlers):
                if handler not in handlers:
                    logging.getLogger().removeHandler(handler)
                    handler.close()
            logging.getLogger().setLevel(level)
            faulthandler.disable()
            if logging_setup._fault_file is not None:
                logging_setup._fault_file.close()
                logging_setup._fault_file = None
            temp.cleanup()
        self.addCleanup(restore)

    def read_log(self):
        for handler in logging.getLogger().handlers:
            handler.flush()
        return (self.root / "v9.log").read_text(encoding="utf-8")

    def test_routes_prints_errors_and_thread_crashes_once(self):
        logging_setup.configure_logging(self.root)
        logging_setup.configure_logging(self.root)
        self.assertEqual(sum(getattr(h, "majd_handler", False) for h in logging.getLogger().handlers), 1)
        print("studio started")
        print("problem", file=sys.stderr)
        thread = threading.Thread(target=lambda: 1 / 0, name="worker-1")
        thread.start()
        thread.join()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        text = self.read_log()
        self.assertIn("INFO [MainThread] stdout: studio started", text)
        self.assertIn("WARNING [MainThread] stderr: problem", text)
        self.assertIn("Uncaught exception in thread worker-1", text)
        self.assertIn("ZeroDivisionError", text)
        self.assertIn("RuntimeError: boom", text)
        self.assertTrue((self.root / "faults.log").exists())


if __name__ == "__main__":
    unittest.main()
