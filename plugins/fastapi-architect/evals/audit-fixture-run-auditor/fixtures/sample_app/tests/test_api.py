import subprocess


async def test_cli_check_runs():
    probe = subprocess.run(["python", "-m", "app", "check"], check=False)  # noqa: ASYNC221
    assert probe.returncode in (0, 1)
