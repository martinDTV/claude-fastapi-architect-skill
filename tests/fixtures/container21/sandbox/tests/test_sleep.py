"""Runs longer than the executor's timeout: the run must end as ``timeout`` with the container killed."""

import time
import unittest


class Sleeps(unittest.TestCase):
    def test_sleeps_past_the_timeout(self):
        time.sleep(120)
        self.assertTrue(True)
