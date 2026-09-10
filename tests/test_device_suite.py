"""Run the on-device suite on the host too.

``test_st25dv.py`` is written to run on a board (copy it to ``code.py``), where
it prints results and counts failures. Executing it here under the stubs keeps
one set of assertions rather than two, and means CI fails if the on-device
suite would.
"""
import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_on_device_suite_passes_on_host(capsys):
    source = io.open(os.path.join(REPO, "test_st25dv.py"),
                     encoding="utf-8").read()
    namespace = {"__name__": "__main__"}
    exec(compile(source, "test_st25dv.py", "exec"), namespace)  # noqa: S102
    out = capsys.readouterr().out
    assert namespace["failed"] == 0, out
    assert namespace["passed"] > 0
