"""CI text gate: report fixed status only, never rewrite or emit sacred text."""

import json
from contextlib import suppress

from studio_core.religious import hadith_catalog


def main() -> int:
    try:
        entries = hadith_catalog.load()
    except ValueError as error:
        pending = False
        if str(error) == "HADITH_DAILY_NOT_APPROVED":
            with suppress(OSError, ValueError):
                pending = json.loads(hadith_catalog.CATALOG.read_bytes()) == {
                    "schema": 1,
                    "approved": False,
                    "calendar": "umm_al_qura",
                    "entries": [],
                }
        if pending:
            print(json.dumps({"status": "PENDING_OWNER_APPROVAL", "entries": 0}))
            return 0
        print(json.dumps({"status": "HADITH_TEXT_CHECK_FAILED"}))
        return 1
    print(json.dumps({"status": "VERIFIED", "entries": len(entries)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
