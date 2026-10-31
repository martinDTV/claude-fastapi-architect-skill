"""Spawns more processes than the pids limit allows: the kernel must refuse (EAGAIN), the suite reports it."""

import os
import subprocess
import unittest


class Forking(unittest.TestCase):
    def test_process_creation_is_bounded(self):
        children = []
        refused = None
        try:
            for _ in range(200):
                try:
                    children.append(subprocess.Popen(["sleep", "30"]))
                except (BlockingIOError, OSError) as exc:
                    refused = exc
                    break
        finally:
            for child in children:
                try:
                    child.kill()
                except OSError:
                    pass
            for child in children:
                try:
                    child.wait(timeout=10)
                except Exception:
                    pass
        self.assertIsNotNone(refused, f"{len(children)} processes started without the limit biting")
        self.assertLess(len(children), 200)
        self.assertNotEqual(os.getuid(), 0)
