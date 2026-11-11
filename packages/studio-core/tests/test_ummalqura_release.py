"""Calendar parity and imports in the release's dependency-free Python layout."""

import hashlib
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from studio_core.religious import ummalqura

ROOT = Path(__file__).resolve().parents[3]


def test_full_supported_range_matches_upstream_250_reference():
    # Computed once with unmodified hijridate 2.5.0 across all 55,991 supported days.
    reference = "e8d98313b14d342bece3414b0332e27d5384c435f49d562b34eca2e8f01d22c8"
    current, end = date(1924, 8, 1), date(2077, 11, 16)
    digest = hashlib.sha256()
    while current <= end:
        year, month, day = ummalqura.convert(current)
        digest.update(f"{year},{month},{day}\n".encode())
        current += timedelta(days=1)
    assert digest.hexdigest() == reference


def test_isolated_venv_has_calendar_without_external_packages():
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", directory], check=True)  # noqa: S603  # fixed argv, temporary venv
        python = Path(directory) / "bin/python"
        probe = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from datetime import date; from studio_core.religious.hadith_daily import hijri; "
            "assert hijri(date(2024,3,11)) == (1445,9,1); "
            "import importlib.util; assert importlib.util.find_spec('hijridate') is None"
        )
        subprocess.run(  # noqa: S603  # fixed isolated interpreter and probe
            [str(python), "-I", "-c", probe, str(ROOT / "packages/studio-core")], check=True
        )


def test_changed_or_missing_calendar_table_fails_closed():
    for changed in (b"{}", None):
        ummalqura.table.cache_clear()
        try:
            options = {"return_value": changed} if changed else {"side_effect": FileNotFoundError}
            with (
                patch.object(Path, "read_bytes", **options),
                pytest.raises(ValueError, match="UMM_AL_QURA_TABLE_INVALID"),
            ):
                ummalqura.convert(date(2024, 3, 11))
        finally:
            ummalqura.table.cache_clear()
