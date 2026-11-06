import subprocess

import pytest

from studio_core.observability.failure_code import failure_code


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (subprocess.CalledProcessError(1, ["private-arg"], stderr=b"private"), "SUBPROCESS_FAILED"),
        (subprocess.TimeoutExpired(["private-arg"], 1), "TIMEOUT"),
        (TimeoutError("private"), "TIMEOUT"),
        (PermissionError("private"), "PERMISSION_DENIED"),
        (FileNotFoundError("private"), "FILE_MISSING"),
        (ImportError("private"), "IMPORT_FAILED"),
        (OSError("private"), "OS_ERROR"),
        (ValueError("private"), "VALIDATION_FAILED"),
        (RuntimeError("private"), "UNEXPECTED_FAILURE"),
    ],
)
def test_diagnostic_returns_only_a_fixed_code(error, expected):
    assert failure_code(error) == expected
