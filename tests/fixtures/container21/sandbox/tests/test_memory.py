"""Allocates more memory than the policy allows: the cgroup must kill the process (OOM), never the host."""

import unittest


class Hungry(unittest.TestCase):
    def test_allocates_beyond_the_limit(self):
        blocks = []
        for _ in range(64):  # 64 x 16 MiB = 1 GiB, far beyond a 128 MiB limit
            blocks.append(bytearray(16 * 1024 * 1024))
            blocks[-1][::4096] = b"x" * len(blocks[-1][::4096])
        self.assertGreater(len(blocks), 0)
