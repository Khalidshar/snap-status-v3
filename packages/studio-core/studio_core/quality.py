from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class Check:
    name: str
    verdict: Verdict
    detail: str = ""


@dataclass
class QualityReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def verdict(self) -> Verdict:
        if any(c.verdict == Verdict.FAIL for c in self.checks):
            return Verdict.FAIL
        if any(c.verdict == Verdict.WARN for c in self.checks):
            return Verdict.WARN
        return Verdict.PASS

    def require_publishable(self) -> None:
        if self.verdict == Verdict.FAIL:
            raise RuntimeError("Quality Gate blocked publication")
