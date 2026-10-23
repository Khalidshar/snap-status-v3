"""Snap's maintenance command: the engine lives in studio_core.repair (batch 4).

Only the command-line entry stays here, because it owns the runner: the Snap
service that re-runs a fact job through the full agent chain, never a delivery.
"""

import argparse
import json
import time
from pathlib import Path

from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.repair.maintenance import (
    LABELS,
    LOOKBACK_SECONDS,
    MAX_REPAIRS_PER_DAY,
    MAX_REPAIRS_PER_LINEAGE,
    REPAIR_WINDOW_SECONDS,
    REPAIRABLE_COMMANDS,
    RETRYABLE_CODES,
    RULE_VERSION,
    apply,
    delivery_evidence,
    diagnose,
    failure_code,
    initialize,
    lineage,
    retry_fresh_job,
    run_repair,
)

__all__ = [
    "LABELS",
    "LOOKBACK_SECONDS",
    "MAX_REPAIRS_PER_DAY",
    "MAX_REPAIRS_PER_LINEAGE",
    "REPAIRABLE_COMMANDS",
    "REPAIR_WINDOW_SECONDS",
    "RETRYABLE_CODES",
    "RULE_VERSION",
    "apply",
    "delivery_evidence",
    "diagnose",
    "failure_code",
    "initialize",
    "lineage",
    "main",
    "retry_fresh_job",
    "run_repair",
]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Diagnose and, with --apply, repair inside the red lines; never sends"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    repo = Repository(config)
    plan = diagnose(repo, now=time.time())
    if args.apply:
        import os
        import tomllib

        from .service import execute

        settings = tomllib.loads(args.config.read_text())["slice"]
        secret = os.environ["STUDIO_GATEWAY_SECRET"]
        # execute() itself refuses any delivery for did_you_know: a preview only.
        output = apply(
            repo, plan, runner=lambda claim: execute(config, settings, secret, claim=claim)
        )
    else:
        output = {"plan": plan}
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
