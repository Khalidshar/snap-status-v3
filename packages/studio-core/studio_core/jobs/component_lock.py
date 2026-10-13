"""Per-app cooperative fencing for use, repair and maintenance of a component.

Expiry is UNKNOWN, never permission to steal a lock from a running process.
Only its exact holder can release it. A crashed holder needs an external,
reviewed reconciliation; this module cannot kill, repair, or replay work.
Every participant must use this lock; it is not an OS sandbox.
"""

import math
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from studio_core.observability.agent_inventory import APPLICATIONS


class ComponentBusy(RuntimeError):
    pass


class ComponentOwnershipLost(RuntimeError):
    pass


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS component_locks(
        app TEXT NOT NULL, component TEXT NOT NULL, token TEXT NOT NULL,
        purpose TEXT NOT NULL, acquired_at REAL NOT NULL, deadline REAL NOT NULL,
        PRIMARY KEY(app,component))""")


@dataclass(frozen=True)
class ComponentLease:
    app: str
    component: str
    token: str
    deadline: float


class ComponentLocks:
    def __init__(self, repo):
        if repo.config.app not in APPLICATIONS:
            raise ValueError("COMPONENT_APP_INVALID")
        self.repo = repo
        with repo.connect() as db:
            if db.execute(
                "SELECT 1 FROM jobs WHERE app != ? LIMIT 1", (repo.config.app,)
            ).fetchone():
                raise ValueError("COMPONENT_DATABASE_SCOPE_INVALID")

    def acquire(self, component, purpose, seconds=30):
        if (
            not isinstance(component, str)
            or not re.fullmatch("[a-z][a-z0-9_.-]{0,63}", component)
            or purpose not in ("USE", "REPAIR", "MAINTENANCE")
            or type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not 0 < seconds <= 1200
        ):
            raise ValueError("COMPONENT_REQUEST_INVALID")
        with self.repo.transaction() as db:
            initialize(db)
            if db.execute(
                "SELECT 1 FROM component_locks WHERE app=? AND component=?",
                (self.repo.config.app, component),
            ).fetchone():
                raise ComponentBusy("COMPONENT_BUSY_OR_UNKNOWN")
            now = self.repo.clock()
            lease = ComponentLease(self.repo.config.app, component, uuid.uuid4().hex, now + seconds)
            db.execute(
                "INSERT INTO component_locks VALUES(?,?,?,?,?,?)",
                (lease.app, component, lease.token, purpose, now, lease.deadline),
            )
            return lease

    def assert_holder(self, lease):
        if not isinstance(lease, ComponentLease) or lease.app != self.repo.config.app:
            raise ComponentOwnershipLost("COMPONENT_OWNER_INVALID")
        with self.repo.connect() as db:
            row = db.execute(
                "SELECT token,deadline FROM component_locks WHERE app=? AND component=?",
                (lease.app, lease.component),
            ).fetchone()
        if not row or row["token"] != lease.token or row["deadline"] <= self.repo.clock():
            raise ComponentOwnershipLost("COMPONENT_OWNER_EXPIRED_OR_LOST")

    def release(self, lease):
        # A returning holder can attest it is done even after its deadline.
        # The token is never exposed by the monitor and is never reassigned.
        if not isinstance(lease, ComponentLease) or lease.app != self.repo.config.app:
            raise ComponentOwnershipLost("COMPONENT_OWNER_INVALID")
        with self.repo.transaction() as db:
            count = db.execute(
                "DELETE FROM component_locks WHERE app=? AND component=? AND token=?",
                (lease.app, lease.component, lease.token),
            ).rowcount
            if count != 1:
                raise ComponentOwnershipLost("COMPONENT_OWNER_LOST")

    @contextmanager
    def hold(self, component, purpose, seconds=30):
        lease = self.acquire(component, purpose, seconds)
        try:
            self.assert_holder(lease)
            yield lease
            self.assert_holder(lease)
        finally:
            self.release(lease)
