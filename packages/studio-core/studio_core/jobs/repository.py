import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from studio_core.observability import Redactor

PRIORITIES = {"CRITICAL":3,"HIGH":2,"NORMAL":1,"BACKGROUND":0}
TERMINAL = {"COMPLETED","FAILED","CANCELLED","EXPIRED"}
PHASES = ["QUEUED","RUNNING","RESEARCHING","PROCESSING","DOWNLOADING","DESIGNING","RENDERING","VALIDATING","REPAIRING","DELIVERING","COMPLETED"]
TRANSITIONS = {a:{b} for a,b in zip(PHASES,PHASES[1:])}
TRANSITIONS["VALIDATING"].add("DELIVERING")
TRANSITIONS["REPAIRING"] = {"VALIDATING"}

class Conflict(ValueError): pass
class LeaseLost(RuntimeError): pass
class GatewayLimit(RuntimeError): pass

def canonical(value): return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)

class Repository:
    def __init__(self,config,redactor=None,clock=time.time):
        self.config,self.redactor,self.clock=config,redactor or Redactor(),clock
        config.data_root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=config.data_root/"jobs.sqlite"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, app TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
              input_hash TEXT NOT NULL, actor_hash TEXT NOT NULL, command TEXT NOT NULL,
              priority TEXT NOT NULL CHECK(priority IN ('CRITICAL','HIGH','NORMAL','BACKGROUND')),
              state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','RESEARCHING','PROCESSING','DOWNLOADING','DESIGNING','RENDERING','VALIDATING','REPAIRING','DELIVERING','COMPLETED','FAILED','CANCELLED','EXPIRED')),
              created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
              lease_token TEXT, lease_until REAL, error_code TEXT,
              artifacts_terminal_at REAL, artifacts_terminal_reason TEXT);
            CREATE INDEX IF NOT EXISTS queue_index ON jobs(state,created_at);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,job_id TEXT REFERENCES jobs(id),
              at REAL NOT NULL,event TEXT NOT NULL,details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commands(idempotency_key TEXT PRIMARY KEY,actor_hash TEXT NOT NULL,
              job_id TEXT REFERENCES jobs(id),accepted_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS actor_commands ON commands(actor_hash,accepted_at);
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,job_id TEXT REFERENCES jobs(id),state TEXT NOT NULL);
            PRAGMA user_version=1;
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10,isolation_level=None)
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        try: yield db
        finally: db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try: yield db
            except BaseException: db.rollback(); raise
            else: db.commit()

    def event(self,db,job_id,event,details):
        db.execute("INSERT INTO events(job_id,at,event,details) VALUES(?,?,?,?)",
                   (job_id,self.clock(),event,canonical(self.redactor.clean(details))))

    def enqueue(self,key,actor_hash,command,priority,input_hash,expires_at):
        now=self.clock()
        if expires_at<=now: raise ValueError("Expired request")
        with self.transaction() as db:
            old=db.execute("SELECT * FROM jobs WHERE idempotency_key=?",(key,)).fetchone()
            if old:
                if old["input_hash"] != input_hash: raise Conflict("Idempotency key reused with different request")
                return dict(old),False
            count=db.execute("SELECT COUNT(*) FROM commands WHERE actor_hash=? AND accepted_at>?",(actor_hash,now-self.config.rate_window)).fetchone()[0]
            active=db.execute("SELECT COUNT(*) FROM jobs WHERE actor_hash=? AND expires_at>? AND state NOT IN ('COMPLETED','FAILED','CANCELLED','EXPIRED')",(actor_hash,now)).fetchone()[0]
            if count>=self.config.rate_limit or active>=self.config.max_active_per_actor: raise GatewayLimit("Command limit reached")
            job_id=uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,app,idempotency_key,input_hash,actor_hash,command,priority,state,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,'QUEUED',?,?,?)",
                       (job_id,self.config.app,key,input_hash,actor_hash,command,priority,now,now,expires_at))
            db.execute("INSERT INTO commands VALUES(?,?,?,?)",(key,actor_hash,job_id,now))
            self.event(db,job_id,"job.queued",{"command":command,"priority":priority})
            return dict(db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()),True

    def claim(self,job_id=None):
        now=self.clock()
        with self.transaction() as db:
            # A verified ACK may arrive after a previous sweep declared UNKNOWN.
            late=db.execute("SELECT DISTINCT j.id FROM jobs j JOIN deliveries d ON d.job_id=j.id WHERE j.state='FAILED' AND d.state='PUBLISHED'").fetchall()
            for row in late:self.reconcile_delivery(row['id'],db=db)
            # Delivery effects may be unknown even when the overall deadline expires.
            uncertain=db.execute("SELECT id FROM jobs WHERE state='DELIVERING' AND (lease_until<=? OR expires_at<=?)",(now,now)).fetchall()
            for row in uncertain:
                if self.reconcile_delivery(row['id'],db=db):continue
                db.execute("UPDATE jobs SET state='FAILED',error_code='DELIVERY_UNKNOWN',updated_at=?,lease_token=NULL,lease_until=NULL WHERE id=?",(now,row['id']))
                self.event(db,row['id'],"job.delivery_unknown",{})
            expired=db.execute("SELECT id FROM jobs WHERE expires_at<=? AND state NOT IN ('COMPLETED','FAILED','CANCELLED','EXPIRED')",(now,)).fetchall()
            for row in expired:
                db.execute("UPDATE jobs SET state='EXPIRED',updated_at=?,lease_token=NULL,lease_until=NULL,artifacts_terminal_at=?,artifacts_terminal_reason='EXPIRED' WHERE id=?",(now,now,row['id']))
                self.event(db,row['id'],"job.expired",{})
            active=db.execute("SELECT COUNT(*) FROM jobs WHERE lease_until>? AND state NOT IN ('COMPLETED','FAILED','CANCELLED','EXPIRED')",(now,)).fetchone()[0]
            if active>=self.config.max_running: return None
            query="SELECT * FROM jobs WHERE (state='QUEUED' OR (state NOT IN ('COMPLETED','FAILED','CANCELLED','EXPIRED','DELIVERING') AND lease_until<=?))"
            parameters=[now]
            if job_id is not None:
                query+=" AND id=?";parameters.append(job_id)
            candidates=db.execute(query,parameters).fetchall()
            if not candidates: return None
            chosen=max(candidates,key=lambda j:(PRIORITIES[j['priority']]+int((now-j['created_at'])/self.config.aging_seconds),-j['created_at']))
            attempt=1+db.execute("SELECT COUNT(*) FROM events WHERE job_id=? AND event='job.claimed'",(chosen['id'],)).fetchone()[0]
            recovered_from=None if chosen['state']=='QUEUED' else chosen['state']
            token=uuid.uuid4().hex
            db.execute("UPDATE jobs SET state='RUNNING',updated_at=?,lease_token=?,lease_until=? WHERE id=?",(now,token,min(now+self.config.lease_seconds,chosen['expires_at']),chosen['id']))
            self.event(db,chosen['id'],"job.claimed",{"recovered":recovered_from is not None,"attempt":attempt,"recovered_from":recovered_from})
            claimed=dict(db.execute("SELECT * FROM jobs WHERE id=?",(chosen['id'],)).fetchone())
            claimed.update(attempt=attempt,recovered_from=recovered_from)
            return claimed

    def reconcile_delivery(self,job_id,*,db=None):
        if db is None:
            with self.transaction() as connection:return self.reconcile_delivery(job_id,db=connection)
        job=db.execute("SELECT state,error_code FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not job or not (job['state']=='DELIVERING' or job['state']=='FAILED'):return False
        confirmed=db.execute("SELECT 1 FROM deliveries WHERE job_id=? AND state='PUBLISHED' LIMIT 1",(job_id,)).fetchone()
        if not confirmed:return False
        now=self.clock()
        db.execute("UPDATE jobs SET state='COMPLETED',error_code=NULL,updated_at=?,lease_token=NULL,lease_until=NULL,artifacts_terminal_at=?,artifacts_terminal_reason='PUBLISHED' WHERE id=?",(now,now,job_id))
        self.event(db,job_id,"job.delivery_confirmed_recovered",{})
        return True

    def assert_owner(self,job_id,token,*,attempt=None,db=None):
        if db is None:
            with self.transaction() as connection:
                return self.assert_owner(job_id,token,attempt=attempt,db=connection)
        now=self.clock()
        job=db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not job or job['state'] in TERMINAL or job['lease_token']!=token or job['lease_until']<=now or job['expires_at']<=now:
            raise LeaseLost("Stale or missing lease")
        epoch=db.execute("SELECT COUNT(*) FROM events WHERE job_id=? AND event='job.claimed'",(job_id,)).fetchone()[0]
        if attempt is not None and (type(attempt) is not int or attempt<1 or attempt!=epoch):
            raise LeaseLost("Stale attempt")
        owned=dict(job);owned['attempt']=epoch
        return owned

    @contextmanager
    def owned_transaction(self,job_id,token,*,attempt=None,max_seconds=0.25):
        # Keep claim() blocked until the protected local resource commit ends.
        # Callers must not perform network effects or long computations here.
        if not isinstance(max_seconds,(int,float)) or not 0<max_seconds<=1:
            raise ValueError("Invalid transaction budget")
        with self.transaction() as db:
            started=time.monotonic()
            db.set_progress_handler(lambda:time.monotonic()-started>max_seconds,1000)
            try:
                self.assert_owner(job_id,token,attempt=attempt,db=db)
                yield db
                if time.monotonic()-started>max_seconds:raise TimeoutError("Ownership transaction budget exceeded")
            finally:db.set_progress_handler(None,0)

    def record_stage(self,job_id,token,attempt,stage,sha256,relative_path):
        import re
        from pathlib import PurePosixPath
        if type(attempt) is not int or attempt<1:raise ValueError('Invalid stage attempt')
        if stage not in {'fact_source','design','quality'} or not re.fullmatch('[0-9a-f]{64}',sha256):raise ValueError("Invalid stage receipt")
        if not isinstance(relative_path,str) or len(relative_path)>512 or not relative_path or '\\' in relative_path:raise ValueError("Invalid artifact path")
        artifact=PurePosixPath(relative_path)
        if artifact.is_absolute() or '..' in artifact.parts or str(artifact)!=relative_path or relative_path=='.':raise ValueError("Invalid artifact path")
        parts=artifact.parts
        if len(parts)<6 or parts[:3]!=('renders',job_id,'attempts') or not re.fullmatch('[1-9][0-9]*',parts[3]) or parts[3]!=str(attempt):raise ValueError('Artifact outside job attempt namespace')
        # Only DB work in the guarded transaction; callers hash/fsync outside it.
        with self.owned_transaction(job_id,token,attempt=attempt) as db:
            owned=self.assert_owner(job_id,token,attempt=attempt,db=db)
            if owned['state']=='DELIVERING':raise ValueError("Cannot change stage during delivery")
            db.execute("CREATE TABLE IF NOT EXISTS stage_results(job_id TEXT NOT NULL,stage TEXT NOT NULL,attempt INTEGER NOT NULL,sha256 TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(job_id,stage))")
            prior=db.execute("SELECT * FROM stage_results WHERE job_id=? AND stage=?",(job_id,stage)).fetchone()
            if prior and prior['attempt']==attempt and (prior['sha256']!=sha256 or prior['path']!=relative_path):raise ValueError("Stage receipt immutable within attempt")
            if prior and prior['attempt']>attempt:raise LeaseLost("Stage attempt regressed")
            db.execute("INSERT INTO stage_results VALUES(?,?,?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET attempt=excluded.attempt,sha256=excluded.sha256,path=excluded.path",(job_id,stage,attempt,sha256,relative_path))
            self.event(db,job_id,"stage.recorded",{"stage":stage,"attempt":attempt,"sha256":sha256})

    def stage_result(self,job_id,stage,*,token=None,attempt=None):
        if token is not None:
            with self.owned_transaction(job_id,token,attempt=attempt) as db:return self._stage_result(db,job_id,stage)
        if attempt is not None:raise ValueError('Token required for owned read')
        with self.connect() as db:return self._stage_result(db,job_id,stage)

    def _stage_result(self,db,job_id,stage):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_results'").fetchone():return None
            row=db.execute("SELECT * FROM stage_results WHERE job_id=? AND stage=?",(job_id,stage)).fetchone()
            return dict(row) if row else None

    def verify_stage(self,job_id,stage,root,*,token,attempt):
        import os,stat
        from pathlib import Path
        receipt=self.stage_result(job_id,stage,token=token,attempt=attempt)
        if receipt is None:return None
        root=Path(root)
        if root.is_symlink():raise ValueError("Unsafe artifact root")
        artifact=root/receipt['path']
        if not artifact.resolve().is_relative_to(root.resolve()):raise ValueError("Artifact escapes root")
        cursor=root
        for part in Path(receipt['path']).parts:
            cursor=cursor/part
            if cursor.is_symlink():raise ValueError("Unsafe artifact link")
        descriptor=os.open(artifact,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(descriptor,'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=50_000_000:raise ValueError("Invalid artifact size/type")
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
            after=os.fstat(stream.fileno())
            if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError("Artifact changed during verification")
        if digest!=receipt['sha256']:raise ValueError("Artifact hash mismatch")
        self.assert_owner(job_id,token,attempt=attempt)
        return receipt

    def transition(self,job_id,token,state,details=None,*,db=None):
        if db is None:
            with self.transaction() as connection:
                return self.transition(job_id,token,state,details,db=connection)
        now=self.clock()
        job=self.assert_owner(job_id,token,db=db)
        if state not in {'FAILED','CANCELLED'} and state not in TRANSITIONS.get(job['state'],set()): raise ValueError("Invalid state transition")
        db.execute("UPDATE jobs SET state=?,updated_at=?,lease_token=?,lease_until=? WHERE id=?",
                   (state,now,None if state in TERMINAL else token,None if state in TERMINAL else min(now+self.config.lease_seconds,job['expires_at']),job_id))
        self.event(db,job_id,"job.state",{"state":state,**(details or {})})

    def get(self,job_id):
        with self.connect() as db:
            j=db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
            return dict(j) if j else None

    def heartbeat(self,job_id,token):
        now=self.clock()
        with self.transaction() as db:
            result=db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND lease_token=? AND lease_until>? AND expires_at>? AND state NOT IN ('COMPLETED','FAILED','CANCELLED','EXPIRED')",
                (min(now+self.config.lease_seconds,self.get_deadline(db,job_id)),job_id,token,now,now))
            if result.rowcount!=1: raise LeaseLost("Stale or missing lease")

    @staticmethod
    def get_deadline(db,job_id):
        row=db.execute("SELECT expires_at FROM jobs WHERE id=?",(job_id,)).fetchone()
        return row[0] if row else 0

    def timeline(self,job_id):
        with self.connect() as db:
            return [dict(x) for x in db.execute("SELECT at,event,details FROM events WHERE job_id=? ORDER BY id",(job_id,))]

    def mark_artifacts_terminal(self,job_id,reason):
        if reason not in {'PUBLISHED','MANUAL_CONFIRMED','EXPIRED','FAILED_ARCHIVED'}: raise ValueError("Unsafe retention state")
        with self.transaction() as db:
            job=db.execute("SELECT state FROM jobs WHERE id=?",(job_id,)).fetchone()
            if not job or job['state'] not in TERMINAL: raise ValueError("Job not terminal")
            pending=db.execute("SELECT COUNT(*) FROM deliveries WHERE job_id=? AND state NOT IN ('PUBLISHED','MANUAL_CONFIRMED','EXPIRED','FAILED_ARCHIVED')",(job_id,)).fetchone()[0]
            if pending: raise ValueError("Delivery still pending")
            db.execute("UPDATE jobs SET artifacts_terminal_at=?,artifacts_terminal_reason=? WHERE id=?",(self.clock(),reason,job_id))

    def backup(self,destination):
        with self.connect() as source,sqlite3.connect(destination) as target: source.backup(target)
        destination.chmod(0o600)
