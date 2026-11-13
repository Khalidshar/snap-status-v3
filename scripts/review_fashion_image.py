"""Record an owner's visual family-safety decision; dry-run by default, append-only."""

import argparse
import json
import os
from pathlib import Path

from studio_core.config import runtime_state_root
from studio_core.production.fashion_policy import record_review


def main() -> None:
    parser = argparse.ArgumentParser(description="مراجعة صورة الأناقة بعد فحصها بصريًا")
    parser.add_argument("--app", choices=("snap", "status"), required=True)
    parser.add_argument("--image-file", type=Path, required=True)
    parser.add_argument("--image-url", required=True)
    parser.add_argument("--article-url", required=True)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--decision", choices=("approve", "reject"), required=True)
    parser.add_argument("--owner-reviewed", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.owner_reviewed:
        parser.error("يلزم أن يراجع خالد الصورة قبل تسجيل القرار")
    root = args.state_root or Path(
        os.environ.get(
            "MADAR_PRODUCTION_STATE_ROOT", str(runtime_state_root() / args.app / "production")
        )
    )
    if not root.is_absolute() or ".." in root.parts:
        parser.error("مسار الحالة يجب أن يكون مطلقًا")
    record = record_review(
        root / "fashion-image-reviews.jsonl",
        args.image_file,
        args.image_url,
        args.article_url,
        approve=args.decision == "approve",
        apply=args.apply,
    )
    print(json.dumps({"applied": args.apply, "review": record}, ensure_ascii=False))


if __name__ == "__main__":
    main()
