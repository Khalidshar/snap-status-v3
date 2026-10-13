import json, subprocess, sys, tempfile, time, unittest
from dataclasses import replace
from pathlib import Path
from studio_core.config import Config
from studio_core.jobs import Repository, LeaseLost

class OwnershipTests(unittest.TestCase):
    def fixture(self,temp,application='snap-bot-v2'):
        config=replace(Config.load(Path(__file__).parent/'fixtures/foundation.toml'),data_root=Path(temp),app=application)
        repo=Repository(config)
        job,_=repo.enqueue('ownership-test','owner','weather','NORMAL','input',time.time()+600)
        return config,repo,job
    def expire(self,repo,job):
        with repo.connect() as db:db.execute('UPDATE jobs SET lease_until=? WHERE id=?',(time.time()-1,job['id']))
    def test_attempt_monotonic_recovered_phase_and_both_apps(self):
        for application in ('snap-bot-v2','status-bot-v2'):
            with self.subTest(application=application),tempfile.TemporaryDirectory() as temp:
                config,repo,job=self.fixture(temp,application)
                first=repo.claim();self.assertEqual(first['attempt'],1);self.assertIsNone(first['recovered_from'])
                repo.transition(job['id'],first['lease_token'],'RESEARCHING')
                self.expire(repo,job)
                second=Repository(config).claim()
                self.assertEqual(second['attempt'],2);self.assertEqual(second['recovered_from'],'RESEARCHING')
                details=[json.loads(e['details']) for e in repo.timeline(job['id']) if e['event']=='job.claimed']
                self.assertEqual([d['attempt'] for d in details],[1,2])
    def test_old_token_and_epoch_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            _,repo,job=self.fixture(temp);first=repo.claim();self.expire(repo,job);second=repo.claim()
            with self.assertRaises(LeaseLost):repo.assert_owner(job['id'],first['lease_token'],attempt=1)
            with self.assertRaises(LeaseLost):repo.assert_owner(job['id'],second['lease_token'],attempt=1)
            with self.assertRaises(LeaseLost):repo.transition(job['id'],first['lease_token'],'RESEARCHING')
            self.assertEqual(repo.assert_owner(job['id'],second['lease_token'],attempt=2)['attempt'],2)
    def test_transition_and_resource_rollback_together(self):
        with tempfile.TemporaryDirectory() as temp:
            _,repo,job=self.fixture(temp);claim=repo.claim()
            with self.assertRaisesRegex(RuntimeError,'injected'):
                with repo.owned_transaction(job['id'],claim['lease_token'],attempt=1) as db:
                    db.execute('CREATE TABLE test_resource(value INTEGER)')
                    db.execute('INSERT INTO test_resource VALUES(1)')
                    repo.transition(job['id'],claim['lease_token'],'RESEARCHING',db=db)
                    raise RuntimeError('injected')
            self.assertEqual(repo.get(job['id'])['state'],'RUNNING')
            self.assertFalse(any(json.loads(e['details']).get('state')=='RESEARCHING' for e in repo.timeline(job['id'])))
            with repo.connect() as db:self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='test_resource'").fetchone())
    def test_two_real_claimant_processes_have_one_winner(self):
        with tempfile.TemporaryDirectory() as temp:
            _,repo,job=self.fixture(temp);repo.claim();self.expire(repo,job)
            script='''import json,sys
from dataclasses import replace
from pathlib import Path
from studio_core.config import Config
from studio_core.jobs import Repository
config=replace(Config.load(Path(sys.argv[1])),data_root=Path(sys.argv[2]))
claim=Repository(config).claim()
print(json.dumps({'attempt':claim['attempt'] if claim else None}))
'''
            args=[sys.executable,'-c',script,str(Path(__file__).parent/'fixtures/foundation.toml'),temp]
            children=[subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
            attempts=[]
            for child in children:
                out,err=child.communicate(timeout=20);self.assertEqual(child.returncode,0,err);attempts.append(json.loads(out)['attempt'])
            self.assertEqual(attempts.count(2),1);self.assertEqual(attempts.count(None),1)
            claims=[json.loads(e['details']) for e in repo.timeline(job['id']) if e['event']=='job.claimed']
            self.assertEqual([e['attempt'] for e in claims],[1,2])

    def test_stage_pointer_fenced_by_current_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            _,repo,job=self.fixture(temp);first=repo.claim()
            self.assertIsNone(repo.stage_result(job['id'],'design'))
            repo.record_stage(job['id'],first['lease_token'],1,'design','a'*64,f"renders/{job['id']}/attempts/1/output/result.json")
            self.expire(repo,job);second=repo.claim()
            repo.record_stage(job['id'],second['lease_token'],2,'design','b'*64,f"renders/{job['id']}/attempts/2/output/result.json")
            with self.assertRaises(LeaseLost):repo.record_stage(job['id'],first['lease_token'],1,'design','c'*64,f"renders/{job['id']}/attempts/1/output/late.json")
            with self.assertRaises(LeaseLost):repo.record_stage(job['id'],second['lease_token'],1,'design','c'*64,f"renders/{job['id']}/attempts/1/output/late.json")
            self.assertEqual(repo.stage_result(job['id'],'design')['sha256'],'b'*64)
            with self.assertRaises(ValueError):repo.record_stage(job['id'],second['lease_token'],2,'design','b'*64,'../outside')

    def test_slow_owned_transaction_rolls_back_at_context_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            _,repo,job=self.fixture(temp);claim=repo.claim()
            with self.assertRaises(TimeoutError):
                with repo.owned_transaction(job['id'],claim['lease_token'],max_seconds=0.01) as db:
                    repo.transition(job['id'],claim['lease_token'],'RESEARCHING',db=db)
                    time.sleep(0.02)
            self.assertEqual(repo.get(job['id'])['state'],'RUNNING')

    def test_writer_wait_does_not_consume_body_budget(self):
        import threading
        with tempfile.TemporaryDirectory() as temp:
            _,repo,job=self.fixture(temp);claim=repo.claim();ready=threading.Event()
            def writer():
                with repo.transaction():ready.set();time.sleep(0.5)
            thread=threading.Thread(target=writer);thread.start();self.assertTrue(ready.wait(2))
            try:repo.record_stage(job['id'],claim['lease_token'],1,'design','a'*64,f"renders/{job['id']}/attempts/1/output/result.json")
            finally:thread.join(timeout=2)
            self.assertEqual(repo.stage_result(job['id'],'design')['sha256'],'a'*64)
    def test_owned_stage_read_and_immutable_same_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            _,repo,job=self.fixture(temp);first=repo.claim();p=f"renders/{job['id']}/attempts/1/output/result.json"
            repo.record_stage(job['id'],first['lease_token'],1,'design','a'*64,p)
            with self.assertRaises(ValueError):repo.record_stage(job['id'],first['lease_token'],1,'design','b'*64,p)
            self.expire(repo,job);second=repo.claim()
            with self.assertRaises(LeaseLost):repo.stage_result(job['id'],'design',token=first['lease_token'],attempt=1)
            self.assertEqual(repo.stage_result(job['id'],'design',token=second['lease_token'],attempt=2)['sha256'],'a'*64)
    def test_verify_stage_hash_namespace_and_mutation(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            _,repo,job=self.fixture(temp);claim=repo.claim();p=f"renders/{job['id']}/attempts/1/output/result.json"
            artifact=Path(temp)/p;artifact.parent.mkdir(parents=True);artifact.write_bytes(b'synthetic artifact')
            digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
            repo.record_stage(job['id'],claim['lease_token'],1,'design',digest,p)
            self.assertEqual(repo.verify_stage(job['id'],'design',Path(temp),token=claim['lease_token'],attempt=1)['sha256'],digest)
            artifact.write_bytes(b'changed')
            with self.assertRaises(ValueError):repo.verify_stage(job['id'],'design',Path(temp),token=claim['lease_token'],attempt=1)
            for bad in ('.','a//b','x/./y',f"renders/{job['id']}/attempts/2/output/result.json"):
                with self.subTest(path=bad),self.assertRaises(ValueError):repo.record_stage(job['id'],claim['lease_token'],1,'quality',digest,bad)

    def test_noncanonical_attempt_names_and_old_path_adoption_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            _,repo,job=self.fixture(temp);first=repo.claim()
            for alias in ('01','001','١'):
                with self.subTest(alias=alias),self.assertRaises(ValueError):repo.record_stage(job['id'],first['lease_token'],1,'design','a'*64,f"renders/{job['id']}/attempts/{alias}/output/result.json")
            self.expire(repo,job);second=repo.claim()
            with self.assertRaises(ValueError):repo.record_stage(job['id'],second['lease_token'],2,'design','b'*64,f"renders/{job['id']}/attempts/1/output/result.json")
