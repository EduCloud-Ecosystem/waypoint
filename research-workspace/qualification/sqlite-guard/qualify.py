#!/usr/bin/env python3
"""Pinned disposable writer witnesses. No existing-state argument or admission hook."""
import argparse
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
sys.path[:0] = [str(WORKSPACE), str(WORKSPACE / "tests")]
from research_workspace import operation_guard as g, recovery as r
from research_workspace.store import Store, canonical, AccessError
from test_finite_authority import FiniteAuthorityTests
from test_operation_guard import FaultDB

OBSERVATIONS = []


def record(control, classification, **evidence):
    OBSERVATIONS.append(dict(control=control, classification=classification, **evidence))


def sizes(root):
    paths = [root / "workspace.sqlite3", root / "workspace.sqlite3-wal", root / "workspace.sqlite3-shm", root / g.FILE, *sorted(root.glob(g.TEMP + "*"))]
    return {p.name: p.stat().st_size for p in paths if p.is_file()}


def settings(db):
    return {key: db.execute("PRAGMA " + key).fetchone()[0] for key in (
        "page_size", "page_count", "freelist_count", "journal_mode", "synchronous",
        "wal_autocheckpoint", "journal_size_limit", "max_page_count", "cache_size",
        "cache_spill", "temp_store", "busy_timeout")}


def fingerprint(root):
    with closing(sqlite3.connect(root / "workspace.sqlite3")) as db:
        return r.database_digest(db)


class ProtocolControls(FiniteAuthorityTests):
    """Only explicitly selected methods run; inherited tests are not rerun here."""
    def held_reader(self):
        reader = self.store.connect()
        reader.execute("BEGIN")
        reader.execute("SELECT key_hash FROM users WHERE subject='alice'").fetchone()
        return reader

    def test_held_reader_safety_wal_and_checkpoint(self):
        with closing(self.held_reader()) as reader, closing(self.store._connect()) as keeper:
            old_key = reader.execute("SELECT key_hash FROM users WHERE subject='alice'").fetchone()[0]
            before_settings = settings(keeper)
            before = sizes(self.root)
            ordinary = sum(not x['essential'] for x in self.store.guard.load()['operations'].values())
            samples = []
            for _ in range(160):
                self.store.rotate_user("alice")
                samples.append(sizes(self.root)['workspace.sqlite3-wal'])
            self.assertEqual(reader.execute("SELECT key_hash FROM users WHERE subject='alice'").fetchone()[0], old_key)
            self.assertNotEqual(keeper.execute("SELECT key_hash FROM users WHERE subject='alice'").fetchone()[0], old_key)
            passive = tuple(keeper.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
            self.assertGreater(passive[1], passive[2])
            self.assertGreater(samples[-1], before['workspace.sqlite3-wal'])
            self.assertEqual(sum(not x['essential'] for x in self.store.guard.load()['operations'].values()), ordinary)
            self.assertEqual(sum(x['essential'] for x in self.store.guard.load()['operations'].values()), 1)
            reader.rollback()
            truncated = tuple(keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            self.assertEqual(truncated, (0, 0, 0))
            self.assertEqual(sizes(self.root)['workspace.sqlite3-wal'], 0)
            after_settings = settings(keeper)
            for key in before_settings.keys() - {'page_count', 'freelist_count'}:
                self.assertEqual(before_settings[key], after_settings[key])
            record('held_reader_and_safety_wal', 'observed counterexample to row-only WAL bound',
                   writes=160, rows_preserved=True, ordinary_intents=ordinary, essential_slots=1,
                   before=before, wal_samples=samples, passive_result=passive,
                   reader_snapshot_preserved=True, after_reader_release_truncate=truncated,
                   automatic_checkpoint_threshold_crossed=passive[1] >= before_settings["wal_autocheckpoint"],
                   settings=before_settings, settings_unchanged=True,
                   note='Explicit checkpoints affect disposable fixture only. Samples are not a maximum or a production checkpoint policy.')

    def test_startup_barrier_is_a_writer(self):
        with closing(self.held_reader()), closing(self.store._connect()) as keeper:
            before = sizes(self.root)
            for _ in range(6): Store(self.root)
            after = sizes(self.root)
            self.assertGreater(after['workspace.sqlite3-wal'], before['workspace.sqlite3-wal'])
            self.assertEqual(settings(keeper)['journal_mode'], 'wal')
            record('startup_barrier', 'observed recurring writer', opens=6, before=before, after=after,
                   production_site='Guard.resume FULL-synchronous barrier', finite_lifetime_wal_bound='UNKNOWN')

    def test_transaction_rollback_and_commit_acknowledgement(self):
        outcomes = []
        for after_commit in (False, True):
            case = FiniteAuthorityTests('test_binding_and_profile_are_explicit_and_not_a_quota')
            try:
                case.setUp()
                original = case.store._connect
                # Only the business transaction connection fails. Intent registration
                # remains real; a wrapper injects either pre-commit or post-commit loss.
                calls = 0
                def connect():
                    nonlocal calls
                    calls += 1
                    db = original()
                    return FaultDB(db, commit=True, after_commit=after_commit) if calls == 2 else db
                with closing(case.store.connect()) as reader:
                    reader.execute('BEGIN'); reader.execute('SELECT active FROM users').fetchall()
                    before = sizes(case.root)
                    with patch.object(case.store, '_connect', side_effect=connect):
                        # Guard.connect was captured at construction. Replace it too
                        # so the first call is intent registration and second business.
                        with patch.object(case.store.guard, 'connect', side_effect=connect):
                            with self.assertRaises(OSError): case.store.revoke_user('alice')
                    with closing(original()) as observer:
                        active = observer.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0]
                    self.assertEqual(active, 0 if after_commit else 1)
                    self.assertEqual(case.store.operation_status()['state'], 'paused')
                    outcomes.append(dict(commit_happened=after_commit, active=active, before=before,
                                         after=sizes(case.root), hold=True, credit=0))
            finally: case.doCleanups()
        record('transaction_commit_faults', 'injected acknowledgement boundaries', outcomes=outcomes,
               power_loss_tested=False, sqlite_io_fault_tested=False)

    def full_guard(self):
        """Synthetic maximum-width valid map plus matching current SQL receipts."""
        data = self.store.guard.load()
        data['sequence'] = 2**63 - 20
        data['generation'] = 2**63 - 20
        operations = {}
        for i in range(g.MAX_OPERATIONS + 1):
            oid = f'{i:032x}'
            operations[oid] = dict(kind='initialize', phase='uncertain', resource_id='r'*80,
                                   subject='s'*80, request_key='k'*80, request_hash='a'*64,
                                   owner=g.BOOT, reason='unresolved_compute',
                                   sequence=data['sequence']-i, essential=(i==g.MAX_OPERATIONS))
        data['operations'] = operations
        # Full map shape, not a claim that 4097 real operations were executed.
        with self.store.guard.locked():
            self.store.guard.save(copy.deepcopy(data))
            data = self.store.guard.load()
            with closing(self.store._connect()) as db:
                db.execute('DELETE FROM operation_receipts')
                for oid,item in data['operations'].items():
                    db.execute('INSERT INTO operation_receipts VALUES(?,?,?,?,?)',
                               (oid,item['sequence'],self.store.guard.intent_digest(oid,item),'started',int(item['essential'])))
                db.execute('UPDATE operation_authority SET sequence=?', (data['sequence'],))
                db.commit()
            self.store.guard.proof(data)
        return data

    def test_full_guard_replacement_peak(self):
        data = self.full_guard()
        old = (self.root/g.FILE).read_bytes()
        expected = copy.deepcopy(data); expected['generation'] += 1
        planned = g.encode(expected)
        peaks = []
        replace = os.replace
        def observe(source, destination):
            if Path(destination) == self.root/g.FILE:
                self.assertEqual(Path(source).read_bytes(), planned)
                self.assertEqual(Path(destination).read_bytes(), old)
                peaks.append(dict(current_bytes=len(old), temporary_bytes=len(planned),
                                  logical_bytes=len(old)+len(planned), entries=2))
            return replace(source, destination)
        with self.store.guard.locked(), patch.object(g.os,'replace',side_effect=observe):
            self.store.guard.save(data)
        self.assertEqual(len(peaks),1)
        self.assertLessEqual(len(old),g.MAX_BYTES)
        self.assertLessEqual(len(planned),g.MAX_BYTES)
        self.assertEqual(self.store.guard.load()['operations'], expected['operations'])
        # Independent encoding upper envelope: every key is fixed 32 hex chars,
        # each item substitutes maximum string/number widths; false is longer.
        widest = max(data['operations'].values(),key=lambda x:len(g.encode(x)))
        item_bound = len(g.encode(widest))
        header = copy.deepcopy(data); header['operations'] = {}
        map_bound = len(g.encode(header)) + (g.MAX_OPERATIONS+1)*(34+1+item_bound+1)
        self.assertLessEqual(len(planned), map_bound)
        self.assertLessEqual(map_bound, g.MAX_BYTES)
        record('full_guard_replacement','source-shaped logical-byte bound', operations=len(data['operations']),
               measured=peaks[0], conservative_current_upper=map_bound,
               enforced_per_serialization=g.MAX_BYTES, conservative_current_plus_next=2*g.MAX_BYTES,
               note='Boundary map seeded synthetically. Only Guard.save performs the measured replacement. No orphan, SQLite, filesystem or future retry credit.')

    def test_repeated_failed_guard_writes_accumulate_orphans(self):
        before_db = fingerprint(self.root)
        old = (self.root/g.FILE).read_bytes()
        retained = []
        for attempt in range(3):
            with patch.object(g.os,'fsync',side_effect=OSError('injected guard file fsync failure')):
                with self.assertRaises(OSError): self.store.logout('not-a-session')
            files = sorted(self.root.glob(g.TEMP+'*'))
            self.assertEqual(len(files), attempt+1)
            self.assertEqual((self.root/g.FILE).read_bytes(), old)
            self.assertEqual(fingerprint(self.root), before_db)
            retained.append({p.name:len(p.read_bytes()) for p in files})
        before = retained[-1]
        # A later successful safety write does not authorize orphan removal.
        self.store.logout('still-not-a-session')
        for name,length in before.items(): self.assertEqual((self.root/name).stat().st_size,length)
        self.assertEqual(self.store.operation_status()['state'],'paused')
        record('retry_orphans','observed unbounded-retry premise failure', failed_attempts=3,
               retained=retained, old_preserved_before_retry=True, failed_business_writes=0,
               later_safety_write=True, protected_files_removed=0, future_orphan_count='UNKNOWN', capacity_credit=0)

    def test_post_rename_directory_sync_uncertainty(self):
        old = (self.root/g.FILE).read_bytes()
        before_db = fingerprint(self.root)
        original = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError('injected parent fsync failure')
            return original(fd)
        with patch.object(g.os,'fsync',side_effect=fail_directory):
            with self.assertRaises(OSError): self.store.logout('no-session')
        self.assertNotEqual((self.root/g.FILE).read_bytes(),old)
        self.assertEqual(fingerprint(self.root),before_db)
        before = sizes(self.root)
        self.store.logout('later-safety')
        self.assertEqual(self.store.operation_status()['state'],'paused')
        record('post_rename_sync','visible generation is not durability proof', before_safety=before,
               after_safety=sizes(self.root), initial_business_write=False, later_safety_write=True,
               guard_hold=True, durable_completion_proven=False, reclaimed_bytes=0)

    def test_required_safety_actions_with_held_reader(self):
        results=[]
        for action in ('authenticate','logout','demote','remove','revoke','rotate','cancel','settle','close'):
            case=FiniteAuthorityTests('test_binding_and_profile_are_explicit_and_not_a_quota')
            try:
                case.setUp(); bob_key = case.store.add_user('bob')
                case.store.membership(case.pid,'alice','bob','editor')
                token=case.store.session(key=case.key)[1]
                bob_token=case.store.session(key=bob_key)[1]
                run=case.submit(); claimed=case.store.claim()
                bob_run=case.store.submit(case.pid,'bob',{'recipe':'summary-python','condition':'baseline','csv':'group,value\na,1\n'},case.config['images'],'bob-run')
                claimed.update(status='failed',cleanup_verified=True,
                               compute={'reserved_cpu_seconds':30,'charged_cpu_seconds':30,
                                        'provenance':'estimated_upper_bound','unresolved_liability':False})
                operations={'authenticate':lambda:case.store.session(token=token),
                            'logout':lambda:case.store.logout(token),
                            'demote':lambda:case.store.membership(case.pid,'alice','bob','viewer'),
                            'remove':lambda:case.store.membership(case.pid,'alice','bob'),
                            'revoke':lambda:case.store.revoke_user('bob'),
                            'rotate':lambda:case.store.rotate_user('bob'),
                            'cancel':lambda:case.store.cancel(case.pid,run['id'],'alice'),
                            'settle':lambda:case.store.finish(claimed,claimed=True),
                            'close':lambda:case.store.close_project(case.pid,'alice')}
                with closing(case.store.connect()) as reader:
                    reader.execute('BEGIN'); reader.execute('SELECT * FROM users').fetchall()
                    before=sizes(case.root); operations[action](); after=sizes(case.root)
                    if action=='logout':
                        with self.assertRaises(AccessError): case.store.session(token=token)
                    else: self.assertEqual(case.store.session(token=token),'alice')
                    if action in ('rotate','revoke'):
                        with self.assertRaises(AccessError): case.store.session(token=bob_token)
                    with closing(case.store.connect()) as observed:
                        if action in ('demote','remove','revoke','close'):
                            self.assertEqual(observed.execute('SELECT cancel_requested FROM runs WHERE id=?',(bob_run['id'],)).fetchone()[0],1)
                        if action=='cancel': self.assertEqual(observed.execute('SELECT cancel_requested FROM runs WHERE id=?',(run['id'],)).fetchone()[0],1)
                        if action in ('demote','remove'):
                            role=observed.execute('SELECT role FROM members WHERE project_id=? AND subject=?',(case.pid,'bob')).fetchone()
                            self.assertEqual(role[0] if role else None,'viewer' if action=='demote' else None)
                        if action=='rotate':self.assertNotEqual(observed.execute("SELECT key_hash FROM users WHERE subject='bob'").fetchone()[0],hashlib.sha256(bob_key.encode()).hexdigest())
                        if action=='revoke':self.assertEqual(observed.execute("SELECT active FROM users WHERE subject='bob'").fetchone()[0],0)
                    if action=='settle':self.assertEqual(case.store.run(case.pid,run['id'],'alice')['compute']['charged_cpu_seconds'],30)
                    if action=='close':self.assertEqual(case.store.project(case.pid,'alice')['project']['status'],'closed')
                    results.append(dict(action=action,before=before,after=after,effect_verified=True))
            finally:case.doCleanups()
        record('safety_reader_matrix','production methods in independent disposable fixtures',actions=results,
               physical_capacity_guaranteed=False, row_caps_filled=False)

    def test_borrowed_essential_repeats_without_counter_growth(self):
        self.store.guard.fail()
        before_guard=(self.root/g.FILE).read_bytes()
        with closing(self.held_reader()), closing(self.store._connect()):
            before=sizes(self.root)
            for _ in range(8): self.store.rotate_user('alice')
            after=sizes(self.root)
            self.assertGreater(after['workspace.sqlite3-wal'],before['workspace.sqlite3-wal'])
            self.assertEqual((self.root/g.FILE).read_bytes(),before_guard)
            self.assertEqual(self.store.operation_status()['state'],'paused')
            record('borrowed_essential_repeats','guard counters do not bound WAL history',
                   writes=8,before=before,after=after,guard_bytes_unchanged=True,remaining_wal='UNKNOWN')

    def test_recover_batch_and_maximum_run_records(self):
        first=self.submit(); self.store.claim()
        for i in range(19):self.submit('queued-'+str(i))
        # Existing logical run/queue limits, synthetic near-ceiling payloads.
        # No broker execution. Record seeding is outside the measured transition.
        with closing(self.store._connect()) as db:
            for row in db.execute('SELECT id,payload FROM runs').fetchall():
                run=json.loads(row['payload']);run['metrics']={'fixture_padding':'x'*250000}
                payload=self.store.serialized(run,'run')
                db.execute('UPDATE runs SET payload=? WHERE id=?',(payload,row['id']))
            db.commit()
        with closing(self.held_reader()), closing(self.store._connect()) as keeper:
            before=sizes(self.root)
            command = "import json,sys; from research_workspace.store import Store; s=Store(sys.argv[1]); s.recover(cancel_queued=True); print(json.dumps(s.operation_status()))"
            recovered = subprocess.run([sys.executable,'-W','error::ResourceWarning','-c',command,str(self.root)], cwd=WORKSPACE,
                                       check=True,capture_output=True,text=True,timeout=20)
            fresh_status=json.loads(recovered.stdout)
            after=sizes(self.root)
            values=[json.loads(row[0]) for row in keeper.execute('SELECT payload FROM runs')]
            self.assertEqual(sum(x['status']=='cancelled' for x in values),19)
            self.assertEqual(sum(x['status']=='interrupted' for x in values),1)
            self.assertTrue(next(x for x in values if x['id']==first['id'])['compute']['unresolved_liability'])
            self.assertEqual(fresh_status['state'],'paused')
            record('recovery_batch','observed finite-record batch with unknown engine peak',
                   rewritten_runs=20,synthetic_padding_bytes_each=250000,before=before,after=after,
                   fresh_process_status=fresh_status,
                   running_liability_preserved=True,broker_effects=0,engine_transient_peak='UNKNOWN')


CONTROL_NAMES = [
    'test_held_reader_safety_wal_and_checkpoint', 'test_startup_barrier_is_a_writer',
    'test_transaction_rollback_and_commit_acknowledgement', 'test_full_guard_replacement_peak',
    'test_repeated_failed_guard_writes_accumulate_orphans', 'test_post_rename_directory_sync_uncertainty',
    'test_required_safety_actions_with_held_reader',
    'test_borrowed_essential_repeats_without_counter_growth', 'test_recover_batch_and_maximum_run_records']


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    contract=json.loads((HERE/'contract-v1.json').read_bytes())
    checked={}
    for name,expected in contract['source_sha256'].items():
        actual=hashlib.sha256((WORKSPACE.parent/name).read_bytes()).hexdigest()
        if actual!=expected:raise ValueError('Pinned source changed: '+name)
        checked[name]=actual
    suite=unittest.TestSuite(ProtocolControls(name) for name in CONTROL_NAMES)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    with closing(sqlite3.connect(':memory:')) as db:
        engine={'version':sqlite3.sqlite_version,'source_id':db.execute('SELECT sqlite_source_id()').fetchone()[0],
                'compile_options':[row[0] for row in db.execute('PRAGMA compile_options')]}
    import _sqlite3
    engine['binding_note']='Source ID, compile options and Python extension hash; not complete dynamic-library/VFS attestation.'
    engine['python_extension_sha256']=hashlib.sha256(Path(_sqlite3.__file__).read_bytes()).hexdigest()
    payload={'format':'waypoint-sqlite-guard-qualification-v1','base_commit':contract['base_commit'],
             'source_sha256':checked,'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'contract_sha256':hashlib.sha256((HERE/'contract-v1.json').read_bytes()).hexdigest(),'python':platform.python_version(),'platform':platform.system(),
             'sqlite':engine,'tests_run':result.testsRun,'tests_passed':result.wasSuccessful(),
             'observations':OBSERVATIONS,'quota_enforced':False,'available_capacity':'UNKNOWN',
             'quota_admission':'NO-GO','fifth_headroom_failure':'FAIL preserved',
             'not_run':['physical exhaustion','power loss','VFS syscall or allocator upper bounds',
                        'production reader ownership/checkpoint gate','fixed retry-slot implementation',
                        'live broker/provider writes','finite restore','accepted store migration']}
    with args.output.open('x') as stream:json.dump(payload,stream,indent=2,sort_keys=True);stream.write('\n')
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
