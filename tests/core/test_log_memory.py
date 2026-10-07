"""The UI log history must not retain exception frames or logging arguments."""

from __future__ import annotations

import gc
import logging
import os
import sys
import unittest
import weakref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.core.log import FerretFormatter, LogEmitter, RingBufferHandler


class _Payload:
    def __init__(self) -> None:
        self.data = bytearray(1024 * 1024)


class LogMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ring = RingBufferHandler(LogEmitter(), maxlen=3)
        self.ring.setFormatter(FerretFormatter("%(name)s: %(message)s"))
        # Isolated logger deliberately bypasses the global registry/handlers.
        self.logger = logging.Logger("ferret.memory-test")  # noqa: LOG001
        self.logger.addHandler(self.ring)

    def _fail_and_log(self, *, exception_argument: bool):
        payload = _Payload()
        reference = weakref.ref(payload)
        try:
            raise RuntimeError("task failed")
        except RuntimeError as error:
            if exception_argument:
                self.logger.warning("argument: %s", error, extra={"payload": payload})
            else:
                self.logger.exception("exception", extra={"payload": payload})
        return reference

    def test_exception_frames_arguments_and_extra_release_while_log_is_retained(self):
        for argument in (False, True):
            with self.subTest(exception_argument=argument):
                reference = self._fail_and_log(exception_argument=argument)
                gc.collect()
                self.assertIsNone(reference())
                self.assertIn("task failed", self.ring.recent()[-1].message)

    def test_original_record_is_unchanged_for_other_handlers(self):
        try:
            raise RuntimeError("disk failure")
        except RuntimeError:
            record = self.logger.makeRecord(
                self.logger.name,
                logging.ERROR,
                __file__,
                1,
                "failed %s",
                ("job",),
                sys.exc_info(),
            )
        original = dict(record.__dict__)
        self.ring.handle(record)
        self.assertEqual(record.__dict__, original)
        text = logging.Formatter("%(message)s").format(record)
        self.assertIn("failed job", text)
        self.assertIn("RuntimeError: disk failure", text)
        self.assertIn("RuntimeError: disk failure", self.ring.recent()[0].message)


if __name__ == "__main__":
    unittest.main()
