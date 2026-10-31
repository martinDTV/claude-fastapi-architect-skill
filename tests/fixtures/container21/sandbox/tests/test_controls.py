"""Defensive probes: each test asserts one restriction the container executor promises, from inside the sandbox.
Standard library only; outside a sandbox most of them fail, which is the point."""

import errno
import os
import socket
import subprocess
import tempfile
import unittest


def status():
    out = {}
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            key, _sep, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


class SandboxControls(unittest.TestCase):
    def test_not_root(self):
        self.assertNotEqual(os.getuid(), 0)
        self.assertNotEqual(os.geteuid(), 0)
        self.assertNotEqual(os.getgid(), 0)

    def test_root_filesystem_is_read_only(self):
        for directory in ("/usr/local", "/etc", "/usr/lib"):
            with self.assertRaises(OSError) as ctx:
                fd, path = tempfile.mkstemp(dir=directory)
                os.close(fd)
                os.unlink(path)
            self.assertIn(ctx.exception.errno, (errno.EROFS, errno.EACCES, errno.EPERM), directory)

    def test_no_docker_socket(self):
        for path in ("/var/run/docker.sock", "/run/docker.sock"):
            self.assertFalse(os.path.exists(path), path)

    def test_home_without_credentials(self):
        home = os.environ.get("HOME", "")
        self.assertTrue(home.startswith("/tmp"), home)
        for name in (".ssh", ".aws", ".docker", ".kube", ".gnupg", ".netrc", ".config/gcloud"):
            self.assertFalse(os.path.exists(os.path.join(home, name)), name)
        suspicious = [k for k in os.environ if k.startswith(("AWS_", "GITHUB_", "DOCKER_", "SSH_")) or "TOKEN" in k or "SECRET" in k]
        self.assertEqual(suspicious, [])

    def test_no_new_privileges_and_no_capabilities(self):
        st = status()
        self.assertEqual(st.get("NoNewPrivs"), "1")
        self.assertEqual(st.get("CapBnd", "").strip("0"), "")
        self.assertEqual(st.get("CapEff", "").strip("0"), "")
        self.assertEqual(st.get("Seccomp"), "2")

    def test_network_is_disabled(self):
        interfaces = sorted(os.listdir("/sys/class/net"))
        self.assertEqual([i for i in interfaces if i != "lo"], [], interfaces)
        with self.assertRaises(OSError):
            socket.create_connection(("1.1.1.1", 80), timeout=2)

    def test_cgroup_limits_are_present(self):
        with open("/sys/fs/cgroup/memory.max", encoding="utf-8") as handle:
            memory = handle.read().strip()
        with open("/sys/fs/cgroup/pids.max", encoding="utf-8") as handle:
            pids = handle.read().strip()
        self.assertNotEqual(memory, "max")
        self.assertNotEqual(pids, "max")
        self.assertGreater(int(memory), 0)
        self.assertGreater(int(pids), 0)

    def test_tmp_is_writable_but_noexec(self):
        fd, path = tempfile.mkstemp(dir="/tmp", suffix=".sh")
        with os.fdopen(fd, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        try:
            with self.assertRaises(PermissionError):
                subprocess.run([path], check=False, timeout=5)
        finally:
            os.unlink(path)

    def test_workspace_is_the_working_directory_and_only_expected_mounts_exist(self):
        self.assertEqual(os.getcwd(), "/workspace")
        self.assertTrue(os.path.isdir("/evidence"))
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            points = [line.split()[4] for line in handle if len(line.split()) > 5]
        unexpected = [p for p in points if p.startswith(("/host", "/Users", "/home")) or p in ("/var/run/docker.sock", "/run/docker.sock")]
        self.assertEqual(unexpected, [])
        self.assertIn("/tmp", points)
        self.assertIn("/workspace", points)

    def test_privilege_escalation_tools_do_not_work(self):
        # with every capability dropped and no-new-privileges, a setuid binary cannot raise privileges;
        # the probe only checks that the id does not change when one is invoked, when present
        for candidate in ("/usr/bin/sudo", "/bin/su", "/usr/bin/su"):
            if os.path.exists(candidate):
                proc = subprocess.run([candidate, "-c", "id -u"] if candidate.endswith("su") else [candidate, "-n", "id", "-u"], capture_output=True, text=True, timeout=10, input="")
                self.assertNotEqual(proc.stdout.strip(), "0", candidate)
        self.assertNotEqual(os.geteuid(), 0)


if __name__ == "__main__":
    unittest.main()
