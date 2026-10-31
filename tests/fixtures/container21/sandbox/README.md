# container21 / sandbox

A self-contained, standard-library-only project whose tests assert the sandbox controls **from inside** the
container executor (v2.1, increment 1). `tests/test_controls.py` passes only when every control is in force;
`tests/test_sleep.py`, `tests/test_memory.py` and `tests/test_pids.py` exercise the timeout, the memory limit and
the pids limit and are selected by dotted name. Nothing here tries to escape the runtime: every test checks a
defensive control and fails when it is absent.
