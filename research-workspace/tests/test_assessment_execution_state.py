"""Source-bound EX2 controls. No helper, provider, upload or workload launch."""
import hashlib
import json
import math
import os
import secrets
import tempfile
import unittest
from unittest import mock
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from research_workspace import assessment_execution_state as ex2
from research_workspace import finite_authority as finite
from research_workspace import sqlite_ownership as owned
from research_workspace.operation_guard import Guard, GuardPaused, LOCK, FILE, APPLICATION_ID, sync_directory
from research_workspace.store import Store, AccessError, private_write


class Clock:
    def __init__(self, wall=1800000000.0, tick=1000.0):
        self.wall, self.tick = wall, tick

    def __call__(self):
        return self.wall

    def monotonic(self):
        return self.tick


def fresh_ex2_store(root, clock=None, *, extra_fact=False, project_seconds=7200, auth_seconds=3600):
    """The sole test-only identity constructor; no public profile selector."""
    clock = clock or Clock()
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    store = object.__new__(Store)
    store.root, store.profile_version, store.finite = root, "EX2", True
    store._assessment, store._assessment_service = ex2.EX2, None
    store._initializing, store.initial_key = True, None
    binding = {"format": ex2.GUARD_FORMAT, "authority_id": "6" * 32,
               "logical_authority_sha256": ex2.policy_digest()}
    store.guard = Guard(root, binding, store.write, assessment=ex2.EX2)
    store.guard.publication.initializing = True
    private_write(root / "config.json", ex2.encode({"version": 1, "mode": ex2.MODE,
        "operation_guard": binding, "logical_authority": ex2.policy()}))
    fd = os.open(root / LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    sync_directory(root)
    with store.guard.locked():
        store.guard.publish({**binding, "generation": 0, "sequence": 0,
                             "operations": {}}, stage="bootstrap")
        token = store.guard.begin("initialize", subject="alice", request={"action": "initialize"},
                                  request_key="ex2_initialize", bootstrap=True)
        try:
            with store.write(role="ex2-test-initialize") as db:
                db.executescript(";".join(ex2.schema()))
                db.execute("BEGIN IMMEDIATE")
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={ex2.MARKER}")
                db.execute("INSERT INTO operation_authority VALUES(?,0,?)",
                           (binding["authority_id"], secrets.token_hex(16)))
                token.register(db)
                db.commit()
                token.effect()
                for subject in ("alice", "bob"):
                    db.execute("INSERT INTO users VALUES(?,?,1)",
                               (subject, hashlib.sha256(subject.encode()).hexdigest()))
                    db.execute("INSERT INTO sessions VALUES(?,?,?)",
                               (hashlib.sha256((subject + "-token").encode()).hexdigest(),
                                subject, clock() + auth_seconds))
                for project_id in ("2" * 32, "3" * 32):
                    project = {"id": project_id, "owner": "alice", "name": "EX2 synthetic fixture",
                               "question": "Typed metadata control", "success_criterion": "Retain uncertainty",
                               "created_at": "2026-10-20T12:00:00Z", "ends_at": ex2.utc(clock() + project_seconds),
                               "budget_cpu_seconds": 60, "status": "active"}
                    db.execute("INSERT INTO projects VALUES(?,?)",
                               (project_id, ex2.encode(project).decode("ascii")))
                    for subject in ("alice", "bob"):
                        db.execute("INSERT INTO members VALUES(?,?,?)",
                                   (project_id, subject, "owner" if subject == "alice" else "editor"))
                actions = ("revoke-user", "rotate-user", "logout")
                facts = [(action, subject, None) for subject in ("alice", "bob")
                         for action in actions]
                facts += [("membership", subject, project_id) for project_id in
                          ("2" * 32, "3" * 32) for subject in ("alice", "bob")]
                facts += [("close-project", None, project_id) for project_id in
                          ("2" * 32, "3" * 32)]
                for index, (action, subject, project_id) in enumerate(facts, 1):
                    db.execute(ex2.SQL_MUTATIONS["INSERT_ex2_security_facts"],
                               (f"{index:032x}", action, subject, project_id, 0,
                                f"{index + 100:032x}", 1))
                if extra_fact:
                    db.execute(ex2.SQL_MUTATIONS["INSERT_ex2_security_facts"],
                        ('f' * 32, 'logout', 'bob', None, 0, 'e' * 32, 1))
                token.complete_sql(db)
                db.commit()
        finally:
            token.release()
    store._initializing = False
    store.guard.publication.initializing = False
    store.guard.resume()
    ex2._LocalFixture(store, clock=clock, monotonic=clock.monotonic)
    return store


class RealStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = Clock()
        self.store = fresh_ex2_store(Path(self.temporary.name) / 'authority', self.clock)
        self.fixture = self.store._ex2_fixture
        self.token = 'bob-token'

    def seed(self, *, admit=True):
        manifest = self.fixture.supported_manifest('2' * 32)
        child = object()
        with mock.patch.object(self.store, '_ex2_launch_seed', return_value=child) as launch:
            self.assertIs(self.store._ex2_seed(self.token, '2' * 32, manifest), child)
            self.assertEqual(launch.call_count, 1)
        self.assertFalse(self.fixture.published)
        with mock.patch.object(self.store, '_ex2_validate_seed_result', return_value=self.fixture.binding):
            self.store._ex2_accept_seed(child)
        if admit:
            self.store._ex2_admit(self.token, manifest)
        return manifest

    def state(self):
        with self.store.read() as db:
            scopes = ex2.rows(db, 'SCOPE_ALL_META')
            slots = [x for scope in scopes for x in ex2.rows(db, 'SLOT_META_FOR_SCOPE', (scope['scope_id'],))]
            commands = [x for scope in scopes for x in ex2.rows(db, 'COMMANDS_FOR_SCOPE', (scope['scope_id'],))]
            facts = [ex2.row(db, 'SECURITY_BY_ID', (fact['fact_id'],)) for fact in self.fixture.facts]
        return dict(scopes=scopes, slots=slots, commands=commands, facts=facts,
                    guard=self.store.guard.load(), receipts=self.store.guard.receipts())

    def command(self, opportunity):
        return next(row for row in self.state()['commands'] if row['opportunity'] == opportunity)

    def pending(self, opportunity):
        command = self.command(opportunity)
        self.store._ex2_pending(self.token, command['command_id'], command['version'])
        return self.command(opportunity)

    def trigger(self, opportunity):
        command = self.pending(opportunity)
        with mock.patch.object(self.store, '_ex2_trigger_external', return_value=1) as trigger:
            self.store._ex2_trigger(self.token, command['command_id'], command['version'])
            self.assertEqual(trigger.call_count, 1)
            self.assertEqual(self.store.guard.lease.active, {})
        return self.command(opportunity)

    def scope_observation(self, command, generation, **changes):
        with self.store.write(role='ex2-command') as db:
            db.execute('BEGIN IMMEDIATE')
            scope = ex2.row(db, 'SCOPE_FULL_BY_ID', (self.fixture.scope_id,))
            db.rollback()
        projection = {key: scope[key] for key in ex2.SCOPE_CACHE}
        projection.update(scope_generation=generation, **changes)
        return ex2._Observation(command_id=command['command_id'], control_sha256=command['control_sha256'],
            binding_sha256=scope['binding_sha256'], code='OK', projection=projection,
            writer_epoch='7' * 32 if command['operation'] == 'prepare' else None,
            watch_epoch='8' * 32 if command['operation'] == 'prepare' else None)

    def observe(self, opportunity, value):
        command = self.command(opportunity)
        self.store._ex2_observe(self.token, command['command_id'], command['version'], value)

    def prepared(self):
        self.seed()
        command = self.trigger('prepare')
        self.observe('prepare', self.scope_observation(command, 1, scope_state='accepting'))

    def sealed(self):
        self.prepared()
        command = self.trigger('ingest')
        self.observe('ingest', self.scope_observation(command, 2, sealed_length_bytes=21,
                     upload_complete=1, content_close_confirmed=1))
        command = self.trigger('seal')
        self.observe('seal', self.scope_observation(command, 3, scope_state='sealed'))

    def refused_unchanged(self, function, *args, **kwargs):
        before = self.state()
        with self.assertRaises((ValueError, AccessError, GuardPaused)):
            function(*args, **kwargs)
        self.assertEqual(self.state(), before)
        self.assertFalse(owned.held(self.store.root))

    def test_real_initialization_markers_facts_and_closed_public_entry(self):
        with self.store.read() as db:
            self.assertEqual(db.execute('PRAGMA application_id').fetchone()[0], APPLICATION_ID)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], ex2.MARKER)
            self.assertEqual(db.execute(ex2.SQL_READS['AUTH_SHAPE']).fetchone(), (2, 2, 4, 2, 0))
        state = self.state()
        self.assertEqual(len(state['facts']), 12)
        self.assertEqual(state['guard']['sequence'], 1)
        self.assertEqual(len(state['receipts'][1]), 1)
        self.assertEqual(next(iter(state['receipts'][1].values()))[2], 'committed')
        with self.assertRaises(ValueError):
            Store(self.store.root)
        with self.assertRaises(ValueError):
            self.store.connect()
        with self.assertRaises(ValueError):
            with self.store.transaction(request={'action': 'deny-auth'}, preflight=lambda db: True):
                self.fail('Generic callback bypassed EX2 state authority')
        root = Path(self.temporary.name) / 'public'
        with self.assertRaises(ValueError):
            Store(root, create=True, config={'mode': ex2.MODE})
        self.assertFalse(root.exists())

    def test_all_registered_ex2_tables_materialize_empty_and_populated_projections(self):
        scope_id = self.fixture.scope_id
        fact_id = self.fixture.facts[0]['fact_id']
        cases = (
            ('ex2_scopes', 'SCOPE_ALL_META', (), (), 1, 'scope_id', scope_id, 'deny_reason'),
            ('ex2_slots', 'SLOT_META_FOR_SCOPE', (scope_id,), (scope_id,), 2, 'slot_index', 0, 'local_phase'),
            ('ex2_commands', 'COMMANDS_FOR_SCOPE', (scope_id,), (scope_id,), 15, 'scope_id', scope_id, 'state'),
            ('ex2_security_facts', 'SECURITY_BY_ID', ('f' * 32,), (fact_id,), 1, 'fact_id', fact_id, 'action'),
        )
        with self.store.read() as db:
            for table, query, empty, _, _, _, _, _ in cases:
                with self.subTest(table=table, phase='empty'):
                    sql = ex2.SQL_READS[query]
                    self.assertEqual(owned._query_limit(sql, execution=True), (ex2.READ_LIMITS[sql], table))
                    self.assertEqual(ex2.rows(db, query, empty), [])
        self.seed()
        populated = {}
        with self.store.read() as db:
            for table, query, _, parameters, count, field, expected, _ in cases:
                with self.subTest(table=table, phase='populated'):
                    values = ex2.rows(db, query, parameters)
                    self.assertEqual(len(values), count)
                    self.assertEqual(values[0][field], expected)
                    self.assertIs(type(values[0]['version']), int)
                    populated[table] = values[0]
            with self.assertRaises(ValueError):
                db.execute('SELECT scope_id FROM ex2_scopes')
        with self.store.read(role='ex2-read-one-full') as db:
            scope = ex2.row(db, 'SCOPE_FULL_BY_ID', (scope_id,))
        with self.store.read(role='ex2-read-one-full') as db:
            slot = ex2.row(db, 'SLOT_FULL_BY_ID', (scope_id, 0))
        self.assertEqual(scope['manifest_json'], self.fixture.manifest)
        self.assertEqual(slot['handoff_nonce'], self.fixture.slots[0]['handoff_nonce'])
        # Real positive rows have materialized before checking the same typed
        # validator on malformed detached projections. No SQL gate is widened.
        for table, _, _, _, _, _, _, domain in cases:
            identity = 'fact_id' if table == 'ex2_security_facts' else 'command_id' if table == 'ex2_commands' else 'scope_id'
            for field, invalid in (('version', '1'), (identity, 'a' * 33), (domain, 'not_registered')):
                with self.subTest(table=table, invalid_field=field):
                    with self.assertRaises(ValueError):
                        ex2.validate_row(table, {**populated[table], field: invalid}, metadata=True)

    def test_role_gates_execute_approved_literal_and_reject_cross_role(self):
        # These test-only rollback probes exercise both real Owner gates. The
        # production Store exposes neither a generic EX2 transaction nor SQL.
        statements = [
            ('UPDATE users SET active=0 WHERE subject=?', ('bob',)),
            ('DELETE FROM sessions WHERE subject=?', ('bob',)),
            ('UPDATE users SET key_hash=? WHERE subject=?', ('9' * 64, 'bob')),
            ('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(b'bob-token').hexdigest(),)),
            ('DELETE FROM members WHERE project_id=? AND subject=?', ('2' * 32, 'bob')),
            ('INSERT INTO members VALUES(?,?,?) ON CONFLICT(project_id,subject) DO UPDATE SET role=excluded.role', ('2' * 32, 'bob', 'viewer')),
            ('UPDATE projects SET payload=? WHERE id=?', ('{}', '2' * 32)),
        ]
        self.assertEqual({sql for sql, _ in statements}, set(ex2.COMPILED['auth_mutations']))
        for sql, parameters in statements:
            with self.subTest(sql=sql):
                with self.store.write(role='ex2-deny-auth') as db:
                    db.execute('BEGIN IMMEDIATE')
                    self.assertEqual(db.execute(sql, parameters).rowcount, 1)
                    db.rollback()
                with self.store.write(role='ex2-command') as db:
                    db.execute('BEGIN IMMEDIATE')
                    with self.assertRaises(ValueError):
                        db.execute(sql, parameters)
                    db.rollback()
        with self.store.read() as db:
            with self.assertRaises(ValueError):
                db.execute(ex2.SQL_MUTATIONS['SECURITY_CONSUME'], ('1'.zfill(32), 1))
            with self.assertRaises(ValueError):
                db.execute(f'PRAGMA user_version={ex2.MARKER}')
        with self.store.write(role='ex2-deny-auth') as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(ValueError):
                db.execute('INSERT INTO sessions VALUES(?,?,?)', ('0' * 64, 'bob', self.clock() + 90))
            db.rollback()
        self.assertEqual(self.state()['guard']['sequence'], 1)

    def test_seed_unpublished_one_shot_and_atomic_whole_plan(self):
        manifest = self.seed()
        state = self.state()
        self.assertTrue(self.fixture.published)
        self.assertEqual((len(state['scopes']), len(state['slots']), len(state['commands'])), (1, 2, 15))
        self.assertEqual(sum(x['lane'] == 'P' for x in state['commands']), 5)
        self.assertEqual(sum(x['lane'] == 'S' for x in state['commands']), 10)
        self.assertEqual(state['guard']['sequence'], 3)
        self.assertEqual({item['request_key'][-2:] for item in state['guard']['operations'].values()
                          if item['kind'] == 'mutation'}, {'_f', '_a'})
        self.refused_unchanged(self.store._ex2_seed, self.token, '2' * 32, manifest)
        self.refused_unchanged(self.store._ex2_admit, self.token, manifest)
        snapshot = self.store._ex2_snapshot(self.token, full=True)
        self.assertEqual(snapshot['scope']['manifest_json'], manifest)
        self.assertEqual(snapshot['scope']['binding_json'], self.fixture.binding)
        self.assertEqual(len(snapshot['slots']), 2)
        self.assertFalse(snapshot['execution_authorized'])

    def test_nonselected_manifest_refuses_before_seed_intent_or_effect(self):
        original = json.loads(self.fixture.supported_manifest('2' * 32))
        changes = [('pathway', 'instructional'), ('data_classification', 'public')]
        with mock.patch.object(self.store, '_ex2_launch_seed') as launch:
            for key, value in changes:
                mutated = deepcopy(original)
                mutated[key] = value
                self.refused_unchanged(self.store._ex2_seed, self.token, '2' * 32, ex2.encode(mutated).decode())
            mutated = deepcopy(original)
            mutated['ceilings']['max_attempts'] = 12
            self.refused_unchanged(self.store._ex2_seed, self.token, '2' * 32, ex2.encode(mutated).decode())
            launch.assert_not_called()

    def test_seed_must_be_accepted_before_admission_and_wrong_child_holds(self):
        manifest = self.fixture.supported_manifest('2' * 32)
        self.refused_unchanged(self.store._ex2_admit, self.token, manifest)
        child = object()
        with mock.patch.object(self.store, '_ex2_launch_seed', return_value=child):
            self.store._ex2_seed(self.token, '2' * 32, manifest)
        self.refused_unchanged(self.store._ex2_admit, self.token, manifest)
        before = (self.store.root / FILE).read_bytes()
        with self.assertRaises(ValueError):
            self.store._ex2_accept_seed(object())
        self.assertTrue(owned.held(self.store.root))
        self.assertEqual((self.store.root / FILE).read_bytes(), before)
        self.assertFalse(self.fixture.published)

    def test_current_auth_role_session_and_project_expiry_prebegin(self):
        self.seed()
        command = self.command('prepare')
        for token in ('missing', 'alice-token', ''):
            self.refused_unchanged(self.store._ex2_pending, token, command['command_id'], 1)
        self.clock.wall += 3601
        self.refused_unchanged(self.store._ex2_pending, self.token, command['command_id'], 1)

    def test_current_project_expiry_refuses_before_seed_effect(self):
        store = fresh_ex2_store(Path(self.temporary.name) / 'expired-project', self.clock, project_seconds=10)
        fixture = store._ex2_fixture
        before = store.guard.load()
        self.clock.wall += 11
        with mock.patch.object(store, '_ex2_launch_seed') as launch:
            with self.assertRaises(AccessError):
                store._ex2_seed(self.token, '2' * 32, fixture.supported_manifest('2' * 32))
            launch.assert_not_called()
        self.assertEqual(store.guard.load(), before)
        self.assertFalse(owned.held(store.root))

    def test_current_project_close_and_member_demotion_block_primary(self):
        self.seed()
        fact = next(x for x in self.fixture.facts if x['action'] == 'membership' and x['subject'] == 'bob' and x['project_id'] == '2' * 32)
        self.store._ex2_security('alice-token', fact['fact_id'], 1, role='viewer')
        command = self.command('prepare')
        self.refused_unchanged(self.store._ex2_pending, self.token, command['command_id'], 1)
        state = self.state()
        self.assertEqual(state['scopes'][0]['deny_reason'], 'access_ended')
        self.assertTrue(all(x['deny_reason'] == 'access_ended' for x in state['slots']))

    def test_phase_keys_real_command_and_exact_lane_gate(self):
        self.seed()
        command = self.trigger('prepare')
        observed = self.scope_observation(command, 1, scope_state='accepting')
        self.observe('prepare', observed)
        state = self.state()
        keys = {x['request_key'] for x in state['guard']['operations'].values()}
        for suffix in ('_p', '_t', '_o'):
            self.assertIn(command['command_id'] + suffix, keys)
        self.assertEqual(self.command('prepare')['state'], 'observed')
        self.refused_unchanged(self.store._ex2_observe, self.token, command['command_id'], 3, observed)
        self.refused_unchanged(self.store.guard.begin, 'mutation', resource_id=self.fixture.scope_id,
            subject='bob', request=dict(action='command-pending', command_id='a' * 32,
                scope_id=self.fixture.scope_id, project_id='2' * 32, subject='bob', lane='S', ordinal=1,
                control_sha256='c' * 64), request_key='a' * 32 + '_p', essential=False)

    def test_invalid_order_cas_and_duplicate_pending_have_no_intent(self):
        self.seed()
        self.refused_unchanged(self.store._ex2_pending, self.token, self.command('ingest')['command_id'], 1)
        command = self.command('prepare')
        self.refused_unchanged(self.store._ex2_pending, self.token, command['command_id'], True)
        self.refused_unchanged(self.store._ex2_pending, self.token, command['command_id'], 2)
        self.pending('prepare')
        self.refused_unchanged(self.store._ex2_pending, self.token, command['command_id'], 1)
        self.refused_unchanged(self.store._ex2_trigger, self.token, command['command_id'], 1)

    def test_equal_cache_completes_actual_trigger_and_releases_pending_lane(self):
        self.prepared()
        before_scope = self.state()['scopes'][0]
        command = self.trigger('scope-status')
        self.observe('scope-status', self.scope_observation(command, 1))
        after = self.state()
        self.assertEqual(after['scopes'][0], before_scope)
        self.assertEqual(self.command('scope-status')['state'], 'observed')
        with self.store.read() as db:
            self.assertEqual(ex2.rows(db, 'PENDING_LANE', (self.fixture.scope_id, 'S')), [])
        self.assertIsNotNone(self.command('scope-status')['reply_sha256'])
        self.assertEqual(self.store.guard.lease.active, {})

    def test_lower_generation_derives_stale_without_any_caller_flag(self):
        self.sealed()
        before_scope = self.state()['scopes'][0]
        command = self.trigger('scope-status')
        value = self.scope_observation(command, 2)
        self.observe('scope-status', value)
        after = self.state()
        self.assertEqual(after['scopes'][0], before_scope)
        observed = self.command('scope-status')
        self.assertEqual((observed['state'], observed['reply_sha256'], observed['reply_generation']), ('observed_stale', None, 2))
        with self.store.read() as db:
            self.assertEqual(ex2.rows(db, 'PENDING_LANE', (self.fixture.scope_id, 'S')), [])
        self.assertEqual(ex2.definitive_observation({'state': 'triggered'}, correlated=True,
            validated=True, reply_code='OK', reply_generation=4, cache_generation=5,
            cache_equal=False, stale=False), ('observed_stale', False))

    def test_equal_generation_contradiction_holds_without_cache_write(self):
        self.prepared()
        command = self.trigger('scope-status')
        before = (self.store.root / FILE).read_bytes()
        value = self.scope_observation(command, 1, scope_state='preparing')
        with self.assertRaises(ValueError):
            self.observe('scope-status', value)
        self.assertTrue(owned.held(self.store.root))
        self.assertEqual(self.store.guard.lease.failure, 'operation_failed')
        self.assertEqual((self.store.root / FILE).read_bytes(), before)
        with self.assertRaises(owned.OwnershipFailure):
            with self.store.read():
                pass

    def test_definitive_error_completes_without_fabricating_cache(self):
        self.seed()
        command = self.trigger('prepare')
        before_scope = self.state()['scopes'][0]
        value = ex2._Observation(command_id=command['command_id'], control_sha256=command['control_sha256'],
            binding_sha256=before_scope['binding_sha256'], code='ASSESSMENT_HELD')
        self.observe('prepare', value)
        after = self.state()
        self.assertEqual(after['scopes'][0], before_scope)
        self.assertEqual(self.command('prepare')['state'], 'observed')
        self.assertIsNone(self.command('prepare')['reply_sha256'])
        self.assertIsNone(self.command('prepare')['reply_generation'])
        self.assertIsNone(after['slots'][0]['charged_cpu_seconds'])
        self.assertEqual(after['slots'][0]['accounting_unresolved'], 1)

    def test_cancel_slot_only_blocks_matching_execute(self):
        self.sealed()
        execute = self.pending('execute-0')
        self.pending('slot-0-cancel')
        state = self.state()
        self.assertEqual(state['scopes'][0]['deny_reason'], 'none')
        self.assertEqual([x['deny_reason'] for x in state['slots']], ['cancelled', 'none'])
        self.refused_unchanged(self.store._ex2_trigger, self.token, execute['command_id'], execute['version'])

    def test_cancel_scope_denies_all_and_keeps_same_actor_safety_available(self):
        self.prepared()
        self.pending('scope-cancel')
        state = self.state()
        self.assertEqual(state['scopes'][0]['deny_reason'], 'cancelled')
        self.assertTrue(all(x['deny_reason'] == 'cancelled' for x in state['slots']))
        command = self.command('scope-cancel')
        with mock.patch.object(self.store, '_ex2_trigger_external', return_value=1):
            self.store._ex2_trigger(self.token, command['command_id'], command['version'])
        self.assertEqual(self.command('scope-cancel')['state'], 'triggered')
        self.refused_unchanged(self.store._ex2_pending, self.token, self.command('ingest')['command_id'], 1)

    def test_trigger_first_then_cancel_preserves_actual_trigger_record(self):
        self.sealed()
        self.trigger('execute-0')
        self.pending('slot-0-cancel')
        self.assertEqual(self.command('execute-0')['state'], 'triggered')
        self.assertEqual(self.state()['slots'][0]['deny_reason'], 'cancelled')

    def test_security_after_cancel_preserves_first_absorbing_reason(self):
        self.prepared()
        self.pending('scope-cancel')
        fact = next(x for x in self.fixture.facts if x['action'] == 'membership' and x['subject'] == 'bob' and x['project_id'] == '2' * 32)
        self.store._ex2_security('alice-token', fact['fact_id'], 1, role='viewer')
        state = self.state()
        self.assertEqual(state['scopes'][0]['deny_reason'], 'cancelled')
        self.assertTrue(all(x['deny_reason'] == 'cancelled' for x in state['slots']))
        self.assertEqual(next(x for x in state['facts'] if x['fact_id'] == fact['fact_id'])['consumed'], 1)

    def test_fifth_publication_refuses_before_replacing_actual_guard(self):
        self.seed()
        command = self.pending('prepare')
        counts = []
        def boundary(*args):
            operation = next(iter(self.store.guard.lease.active.values()))
            while operation._publications < 4:
                data = self.store.guard.load()
                self.store.guard.publish(data, identity=operation.identity, stage='effect')
            before = (self.store.root / FILE).read_bytes()
            with self.assertRaises(GuardPaused):
                self.store.guard.publish(self.store.guard.load(), identity=operation.identity, stage='effect')
            self.assertEqual((self.store.root / FILE).read_bytes(), before)
            counts.append(operation._publications)
            return 1
        with mock.patch.object(self.store, '_ex2_trigger_external', side_effect=boundary):
            self.store._ex2_trigger(self.token, command['command_id'], command['version'])
        self.assertEqual(counts, [4])
        self.assertFalse(owned.held(self.store.root))

    def test_admitted_failure_preserves_earlier_lease_reason(self):
        self.seed()
        command = self.pending('prepare')
        def boundary(*args):
            self.store.guard.lease.failure = 'prior_failure'
            raise OSError('native boundary uncertainty')
        with mock.patch.object(self.store, '_ex2_trigger_external', side_effect=boundary):
            with self.assertRaises(OSError):
                self.store._ex2_trigger(self.token, command['command_id'], command['version'])
        self.assertTrue(owned.held(self.store.root))
        self.assertEqual(self.store.guard.lease.failure, 'prior_failure')

    def test_all_security_actions_are_atomic_real_auth_and_absorbing_denial(self):
        # Separate fresh authority for each once-only fact; no reset or retry.
        for action in ('revoke-user', 'rotate-user', 'logout', 'membership', 'close-project'):
            with self.subTest(action=action):
                store = fresh_ex2_store(Path(self.temporary.name) / action, Clock())
                old_store, old_fixture = self.store, self.fixture
                self.store, self.fixture = store, store._ex2_fixture
                try:
                    self.seed()
                    fact = next(x for x in self.fixture.facts if x['action'] == action
                        and x['subject'] == (None if action == 'close-project' else 'bob')
                        and x['project_id'] == ('2' * 32 if action in {'membership', 'close-project'} else None))
                    actor = 'alice-token' if action in {'membership', 'close-project'} else self.token
                    options = {'role': None} if action == 'membership' else {'replacement_key': 'replacement-key-' + 'x' * 32} if action == 'rotate-user' else {}
                    before = self.state()
                    store._ex2_security(actor, fact['fact_id'], 1, **options)
                    after = self.state()
                    changed = next(x for x in after['facts'] if x['fact_id'] == fact['fact_id'])
                    self.assertEqual((changed['consumed'], changed['version']), (1, 2))
                    self.assertEqual(after['guard']['sequence'], before['guard']['sequence'] + 1)
                    self.assertEqual(after['scopes'][0]['deny_reason'], 'access_ended')
                    self.assertTrue(all(x['deny_reason'] == 'access_ended' for x in after['slots']))
                    with store.read() as db:
                        if action in {'revoke-user', 'rotate-user', 'logout'}:
                            self.assertIsNone(ex2.row(db, 'CURRENT_AUTH', (hashlib.sha256(b'bob-token').hexdigest(), self.clock()), required=False))
                        if action == 'revoke-user':
                            self.assertIsNone(ex2.row(db, 'ACTIVE_USER', ('bob',), required=False))
                        if action == 'membership':
                            self.assertIsNone(ex2.row(db, 'MEMBER_ROLE', ('2' * 32, 'bob'), required=False))
                        if action == 'close-project':
                            project = ex2.row(db, 'CURRENT_PROJECT', ('2' * 32, 'alice'))
                            self.assertEqual(json.loads(project['payload'])['status'], 'closed')
                    self.refused_unchanged(store._ex2_security, actor, fact['fact_id'], 1, **options)
                    self.assertFalse(owned.held(store.root))
                finally:
                    self.store, self.fixture = old_store, old_fixture

    def test_security_owner_checks_same_role_and_consumed_change_noop(self):
        self.seed()
        facts = self.fixture.facts
        member = next(x for x in facts if x['action'] == 'membership' and x['subject'] == 'bob' and x['project_id'] == '2' * 32)
        owner = next(x for x in facts if x['action'] == 'membership' and x['subject'] == 'alice' and x['project_id'] == '2' * 32)
        self.refused_unchanged(self.store._ex2_security, 'bob-token', member['fact_id'], 1, role='viewer')
        self.refused_unchanged(self.store._ex2_security, 'alice-token', owner['fact_id'], 1, role=None)
        self.refused_unchanged(self.store._ex2_security, 'alice-token', member['fact_id'], 1, role='editor')
        self.store._ex2_security('alice-token', member['fact_id'], 1, role='viewer')
        self.refused_unchanged(self.store._ex2_security, 'alice-token', member['fact_id'], 2, role='editor')

    def test_pre_scope_security_fact_prevents_later_admission(self):
        manifest = self.seed(admit=False)
        fact = next(x for x in self.fixture.facts if x['action'] == 'membership' and x['subject'] == 'bob' and x['project_id'] == '2' * 32)
        self.store._ex2_security('alice-token', fact['fact_id'], 1, role='viewer')
        self.refused_unchanged(self.store._ex2_admit, self.token, manifest)
        self.assertEqual(self.state()['scopes'], [])

    def test_denial_is_first_only_and_preserves_unknown_accounting(self):
        self.seed()
        scope = self.state()['scopes'][0]
        self.refused_unchanged(self.store._ex2_deny, scope['scope_id'], scope['version'], 'expired')
        self.clock.tick += 121
        self.store._ex2_deny(scope['scope_id'], scope['version'], 'expired')
        state = self.state()
        self.assertEqual(state['scopes'][0]['deny_reason'], 'expired')
        self.assertTrue(all(x['charged_cpu_seconds'] is None and x['accounting_unresolved'] == 1 for x in state['slots']))
        self.refused_unchanged(self.store._ex2_deny, scope['scope_id'], scope['version'] + 1, 'expired')

    def test_single_full_snapshot_budget_and_metadata_omissions(self):
        self.seed()
        before = self.state()['guard']
        summary = self.store._ex2_snapshot(self.token)
        self.assertNotIn('manifest_json', summary['scope'])
        self.assertNotIn('metrics_json', summary['slots'][0])
        full = self.store._ex2_snapshot(self.token, full=True)
        slot = self.store._ex2_snapshot(self.token, slot_index=0)
        self.assertLessEqual(len(ex2.encode(full)), 262144)
        self.assertLessEqual(len(ex2.encode(slot)), 65536)
        with self.store.read(role='ex2-read-one-full') as db:
            db.execute(ex2.SQL_READS['SCOPE_FULL_BY_ID'], (self.fixture.scope_id,)).fetchone()
            with self.assertRaises(owned.ReadBounds):
                db.execute(ex2.SQL_READS['SLOT_FULL_BY_ID'], (self.fixture.scope_id, 0))
        self.assertEqual(self.state()['guard'], before)

    def test_real_receipt_rows_at_cap_and_over_cap_sentinel(self):
        # Test-only exact internal role writes construct a boundary fixture.
        # These are not claimed to be 96 real study execution attempts.
        with self.store.write(role='started-receipt') as db:
            db.execute('BEGIN IMMEDIATE')
            for index in range(2, 98):
                db.execute('INSERT INTO operation_receipts VALUES(?,?,?,?,?)',
                           (f'{index:032x}', index, 'b' * 64, 'committed', 0 if index < 97 else 1))
            db.commit()
        with self.store.read(role='guard-proof') as db:
            rows = db.execute('SELECT id,sequence,digest,outcome,essential FROM operation_receipts').fetchmany(98)
        self.assertEqual(len(rows), 97)
        with self.store.write(role='started-receipt') as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO operation_receipts VALUES(?,?,?,?,?)', ('f' * 32, 98, 'b' * 64, 'committed', 0))
            db.commit()
        with self.store.read(role='guard-proof') as db:
            with self.assertRaises(owned.ReadBounds):
                db.execute('SELECT id,sequence,digest,outcome,essential FROM operation_receipts')

    def test_real_extra_security_fact_rejected_by_same_snapshot_preflight(self):
        store = fresh_ex2_store(Path(self.temporary.name) / 'extra-fact', Clock(), extra_fact=True)
        with store.read() as db:
            with self.assertRaises(owned.ReadBounds):
                db.execute(ex2.SQL_READS['ex2_security_facts_COUNT'])

    def test_full_row_byte_boundary_materializes_and_one_over_refuses(self):
        self.seed(admit=False)
        # This is a typed-storage maximum-form fixture, deliberately not an
        # executable manifest. Store admission's exact A2 predicate rejects it.
        scope = self.fixture.scope()
        scope['manifest_json'] = ex2.encode('x' * 65534).decode()
        scope['binding_json'] = ex2.encode('y' * 8190).decode()
        scope['manifest_sha256'] = hashlib.sha256(scope['manifest_json'].encode()).hexdigest()
        scope['binding_sha256'] = hashlib.sha256(scope['binding_json'].encode()).hexdigest()
        with self.store.write(role='ex2-admit') as db:
            db.execute('BEGIN IMMEDIATE')
            for field in ('manifest_json', 'binding_json'):
                too_large = {**scope, field: ex2.encode('z' * (len(scope[field]) - 1)).decode()}
                with self.assertRaises(ValueError):
                    ex2.insert(db, 'ex2_scopes', too_large)
            ex2.insert(db, 'ex2_scopes', scope)
            for slot in self.fixture.slots:
                ex2.insert(db, 'ex2_slots', slot)
            for command in self.fixture.commands:
                ex2.insert(db, 'ex2_commands', command)
            db.commit()
        with self.store.read(role='ex2-read-one-full') as db:
            materialized = ex2.row(db, 'SCOPE_FULL_BY_ID', (self.fixture.scope_id,))
        self.assertEqual(len(materialized['manifest_json'].encode()), 65536)
        self.assertEqual(len(materialized['binding_json'].encode()), 8192)
        self.assertLessEqual(len(ex2.encode(materialized)), 164987)
        self.refused_unchanged(self.store._ex2_admit, self.token, scope['manifest_json'])

    def test_actual_148_typed_rows_and_256_total_envelope_then_overflow(self):
        self.seed(admit=False)
        # Internal storage-bound fixture only. There is no Store admission of
        # this generic 2x12 design and no execution grant or helper call.
        with self.store.write(role='ex2-admit') as db:
            db.execute('BEGIN IMMEDIATE')
            for scope_index in range(2):
                scope_id = f'{1000 + scope_index:032x}'
                scope = {**self.fixture.scope(), 'scope_id': scope_id, 'request_key': scope_id}
                ex2.insert(db, 'ex2_scopes', scope)
                for index in range(12):
                    base = 10000 + scope_index * 1000 + index * 10
                    slot = {**self.fixture.slots[0], 'scope_id': scope_id, 'slot_index': index,
                        'attempt_id': f'{base:032x}', 'reservation_id': f'{base + 1:032x}',
                        'handoff_nonce': f'{base + 2:032x}', 'request_key': f'{base + 3:032x}'}
                    ex2.insert(db, 'ex2_slots', slot)
                for lane, count in (('P', 15), ('S', 40)):
                    for ordinal in range(1, count + 1):
                        base = 50000 + scope_index * 1000 + (100 if lane == 'S' else 0) + ordinal
                        command = {**self.fixture.commands[0], 'scope_id': scope_id,
                            'command_id': f'{base:032x}', 'request_key': f'{base + 10000:032x}',
                            'lane': lane, 'ordinal': ordinal, 'opportunity': f'{lane}-{ordinal}',
                            'operation': 'prepare' if lane == 'P' else 'status'}
                        ex2.insert(db, 'ex2_commands', command)
            db.commit()
        # Initialization and seed already retain two receipts. Add 94 ordinary
        # and one essential receipt using the exact internal role only.
        with self.store.write(role='started-receipt') as db:
            db.execute('BEGIN IMMEDIATE')
            for index in range(3, 98):
                db.execute('INSERT INTO operation_receipts VALUES(?,?,?,?,?)',
                    (f'{index:032x}', index, 'b' * 64, 'committed', int(index == 97)))
            db.commit()
        with self.store.read() as db:
            counts = [db.execute(ex2.SQL_READS[table + '_COUNT']).fetchone()[0] for table in ex2.FIELDSETS]
            shape = db.execute(ex2.SQL_READS['AUTH_SHAPE']).fetchone()
        self.assertEqual(sum(counts), 148)
        self.assertEqual(sum(counts) + sum(shape) + 97 + 1, 256)
        with self.store.read(role='guard-proof') as db:
            self.assertEqual(len(db.execute('SELECT id,sequence,digest,outcome,essential FROM operation_receipts').fetchmany(98)), 97)
        with self.store.write(role='started-receipt') as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO operation_receipts VALUES(?,?,?,?,?)', ('f' * 32, 98, 'b' * 64, 'committed', 0))
            db.commit()
        with self.store.read() as db:
            with self.assertRaises(owned.ReadBounds):
                db.execute(ex2.SQL_READS['ex2_scopes_COUNT'])

    def test_late_auth_expiry_after_effect_prevents_external_trigger_and_holds(self):
        self.seed()
        command = self.pending('prepare')
        publish = self.store.guard.publish
        def delayed(data, **kwargs):
            result = publish(data, **kwargs)
            if kwargs.get('stage') == 'effect':
                self.clock.wall += 3601
            return result
        with mock.patch.object(self.store.guard, 'publish', side_effect=delayed), mock.patch.object(self.store, '_ex2_trigger_external') as trigger:
            with self.assertRaises((ValueError, AccessError)):
                self.store._ex2_trigger(self.token, command['command_id'], command['version'])
            trigger.assert_not_called()
        self.assertTrue(owned.held(self.store.root))
        self.assertEqual(self.store.guard.lease.active, {})

    def test_ordinary_and_essential_begin_publication_failure_hold_under_outer_guard(self):
        for lane in ('P', 'S'):
            with self.subTest(lane=lane):
                store = fresh_ex2_store(Path(self.temporary.name) / ('begin-' + lane), Clock())
                old_store, old_fixture = self.store, self.fixture
                self.store, self.fixture = store, store._ex2_fixture
                try:
                    self.seed()
                    command = self.command('prepare' if lane == 'P' else 'scope-status')
                    holds = []
                    real_hold = owned.hold
                    def hold(root, *args, **kwargs):
                        holds.append(store.guard.lease.depth)
                        return real_hold(root, *args, **kwargs)
                    original = store.guard.publish
                    def fail(data, **kwargs):
                        if kwargs.get('stage') == 'prepare':
                            raise OSError('injected prepare publication')
                        return original(data, **kwargs)
                    with mock.patch.object(owned, 'hold', side_effect=hold), mock.patch.object(store.guard, 'publish', side_effect=fail):
                        with self.assertRaises(OSError):
                            store._ex2_pending(self.token, command['command_id'], 1)
                    self.assertTrue(owned.held(store.root))
                    self.assertTrue(holds and all(depth > 0 for depth in holds))
                    self.assertEqual(store.guard.lease.depth, 0)
                    self.assertEqual(store.guard.lease.active, {})
                finally:
                    self.store, self.fixture = old_store, old_fixture

    def test_ordinary_and_essential_effect_commit_rollback_and_close_faults(self):
        for lane in ('P', 'S'):
            for boundary in ('external', 'committed', 'rolled-back', 'closed'):
                with self.subTest(lane=lane, boundary=boundary):
                    store = fresh_ex2_store(Path(self.temporary.name) / (lane + '-' + boundary), Clock())
                    old_store, old_fixture = self.store, self.fixture
                    self.store, self.fixture = store, store._ex2_fixture
                    try:
                        self.seed()
                        command = self.pending('prepare' if lane == 'P' else 'scope-status')
                        before = store.guard.load()['sequence']
                        reached, holds = [], []
                        real_hold = owned.hold
                        def hold(root, *args, **kwargs):
                            holds.append(store.guard.lease.depth)
                            return real_hold(root, *args, **kwargs)
                        def event(root, role, stage):
                            if role == 'ex2-command' and stage == boundary and store.guard.load()['sequence'] > before:
                                reached.append(stage)
                                raise OSError('injected Owner ' + stage)
                        def trigger(*args):
                            reached.append('external')
                            if boundary in ('external', 'rolled-back'):
                                raise OSError('injected native boundary')
                            return 1
                        with mock.patch.object(owned, '_event', side_effect=event), mock.patch.object(owned, 'hold', side_effect=hold), mock.patch.object(store, '_ex2_trigger_external', side_effect=trigger):
                            with self.assertRaises(OSError):
                                store._ex2_trigger(self.token, command['command_id'], command['version'])
                        self.assertIn(boundary, reached)
                        self.assertTrue(owned.held(store.root))
                        self.assertTrue(holds and all(depth > 0 for depth in holds))
                        self.assertEqual(store.guard.lease.depth, 0)
                        self.assertEqual(store.guard.lease.active, {})
                    finally:
                        self.store, self.fixture = old_store, old_fixture

    def test_atomic_security_rollback_values_observed_before_hold(self):
        self.seed()
        fact = next(x for x in self.fixture.facts if x['action'] == 'membership' and x['subject'] == 'bob' and x['project_id'] == '2' * 32)
        captured = []
        rollback = owned.Owner.rollback
        def inspect_after_rollback(owner):
            result = rollback(owner)
            if owner.role == 'ex2-deny-auth':
                captured.append((ex2.row(owner, 'SECURITY_BY_ID', (fact['fact_id'],))['consumed'],
                    ex2.row(owner, 'MEMBER_ROLE', ('2' * 32, 'bob'))['role'],
                    ex2.row(owner, 'SCOPE_META_BY_ID', (self.fixture.scope_id,))['deny_reason']))
                rollback(owner)
            return result
        with mock.patch.object(owned.Owner, 'rollback', inspect_after_rollback), mock.patch.object(self.store, '_ex2_apply_denial', side_effect=OSError('after auth mutation')):
            with self.assertRaises(OSError):
                self.store._ex2_security('alice-token', fact['fact_id'], 1, role='viewer')
        self.assertIn((0, 'editor', 'none'), captured)
        self.assertTrue(owned.held(self.store.root))

    def test_post_wait_failure_uses_only_existing_lease_and_preserves_failure(self):
        self.store.guard.lease.failure = 'prior_failure'
        before = (self.store.root / FILE).read_bytes()
        with mock.patch.object(self.store.guard, 'locked', side_effect=AssertionError('OS lock forbidden')), mock.patch.object(owned.Owner, '__enter__', side_effect=AssertionError('SQL forbidden')):
            ex2.post_wait_hold(self.store)
        self.assertTrue(owned.held(self.store.root))
        self.assertEqual(self.store.guard.lease.failure, 'prior_failure')
        self.assertEqual((self.store.root / FILE).read_bytes(), before)


class ExpiryContractTests(unittest.TestCase):
    def assert_original_expiry(self, *, sampled_at, expected_scope_epoch, auth_seconds=3600, project_seconds=7200):
        # Independent J1 oracle uses only supplied original inputs, never
        # fixture-computed expiry, a returned binding, or the persisted row.
        original_tick = 1000.25
        original_policy = sampled_at + 1800
        wall_deadline = sampled_at + 120
        policy = math.floor(min(original_policy, wall_deadline))
        manifest_expiry = math.floor(sampled_at + 3600)
        auth_expiry = sampled_at + auth_seconds
        project_expiry = math.floor(sampled_at + project_seconds)
        scope_expiry = math.floor(min(sampled_at + 1800, auth_expiry, project_expiry,
                                      policy, manifest_expiry))
        self.assertEqual(scope_expiry, expected_scope_epoch)
        def stamp(value, *, micros=False):
            return datetime.fromtimestamp(value, timezone.utc).isoformat(
                timespec='microseconds' if micros else 'seconds').replace('+00:00', 'Z')
        clock = Clock(sampled_at, original_tick)
        with tempfile.TemporaryDirectory() as temporary:
            store = fresh_ex2_store(Path(temporary) / 'expiry', clock,
                auth_seconds=auth_seconds, project_seconds=project_seconds)
            fixture = store._ex2_fixture
            manifest = fixture.supported_manifest('2' * 32)
            self.assertEqual(json.loads(manifest)['expires_at'], stamp(manifest_expiry))
            child = object()
            clock.wall += 1.25
            clock.tick += 1.25
            with mock.patch.object(store, '_ex2_launch_seed', return_value=child) as launch:
                store._ex2_seed('bob-token', '2' * 32, manifest)
            launch.assert_called_once()
            metadata = launch.call_args.args[0]
            self.assertEqual(metadata['sampled_at'], stamp(sampled_at, micros=True))
            self.assertEqual(metadata['session_deadline'], stamp(wall_deadline, micros=True))
            self.assertEqual(metadata['policy_expiry'], stamp(math.floor(original_policy)))
            self.assertNotEqual(metadata['policy_expiry'], stamp(policy))
            self.assertEqual(metadata['authorization_expiry'], stamp(math.floor(auth_expiry)))
            self.assertEqual(metadata['project_expiry'], stamp(project_expiry))
            clock.wall += 1.25
            clock.tick += 1.25
            with mock.patch.object(store, '_ex2_validate_seed_result', return_value=fixture.binding):
                store._ex2_accept_seed(child)
            clock.wall += 1.25
            clock.tick += 1.25
            store._ex2_admit('bob-token', manifest)
            snapshot = store._ex2_snapshot('bob-token', full=True)
            self.assertEqual(snapshot['scope']['expires_at'], stamp(scope_expiry))
            prepare = next(x for x in snapshot['commands'] if x['opportunity'] == 'prepare')
            clock.wall += 1.25
            clock.tick += 1.25
            store._ex2_pending('bob-token', prepare['command_id'], 1)
            with mock.patch.object(store, '_ex2_trigger_external', return_value=1) as trigger:
                store._ex2_trigger('bob-token', prepare['command_id'], 2)
            trigger.assert_called_once()
            control = json.loads(trigger.call_args.args[1])
            self.assertEqual(control['operation'], 'prepare')
            self.assertEqual(control['expires_at'], stamp(scope_expiry))
            self.assertEqual(fixture.sample, sampled_at)
            self.assertEqual(fixture.policy_expiry, original_policy)
            self.assertEqual(fixture.session_policy_expiry, policy)
            self.assertEqual(fixture.wall_deadline, wall_deadline)
            self.assertEqual(fixture.deadline, original_tick + 120)
            self.assertLessEqual(scope_expiry, wall_deadline)

    def test_default_scope_and_prepare_expiry_use_original_wall_deadline(self):
        self.assert_original_expiry(sampled_at=1800000000.0, expected_scope_epoch=1800000120)

    def test_fractional_sample_floors_policy_scope_and_manifest_without_renewal(self):
        self.assert_original_expiry(sampled_at=1800000000.75, expected_scope_epoch=1800000120)

    def test_earlier_actual_auth_caps_persisted_scope_and_prepare(self):
        self.assert_original_expiry(sampled_at=1800000000.75, auth_seconds=75.5,
                                   expected_scope_epoch=1800000076)

    def test_earlier_project_caps_persisted_scope_and_prepare(self):
        self.assert_original_expiry(sampled_at=1800000000.75, project_seconds=62.5,
                                   expected_scope_epoch=1800000063)


class ClosedPolicyTests(unittest.TestCase):
    def test_registered_legacy_identifiers_and_closed_ex2_lookup_remain_compatible(self):
        cases = (
            ('SELECT subject FROM users', 128, 'users'),
            ('SELECT payload FROM projects', 20, 'projects'),
            ('SELECT subject,role FROM members WHERE project_id=? ORDER BY subject', 1024, 'members'),
            ('SELECT rowid,* FROM sessions ORDER BY rowid', 256, 'sessions'),
            ('SELECT payload FROM runs ORDER BY rowid', 1000, 'runs'),
            (owned._AUTHORITY_SELECT, 1, 'operation_authority'),
            (owned._RECEIPTS_SELECT, 4097, 'operation_receipts'),
            (owned._SCHEMA_SELECT, 100, 'sqlite_master'),
        )
        for sql, limit, table in cases:
            with self.subTest(table=table):
                self.assertIn(sql, owned._SELECTS)
                self.assertEqual(owned._query_limit(sql), (limit, table))
        self.assertEqual(owned._query_limit(owned.astate.ALL_META, assessment=True), (16, 'assessment_sessions'))
        self.assertEqual(owned._query_limit(owned._RECEIPTS_SELECT, execution=True), (97, 'operation_receipts'))
        for sql in ('SELECT scope_id FROM ex2_scopes', 'SELECT scope_id FROM ex2_scopes_extra',
                    'SELECT scope_id FROM ex3_scopes'):
            with self.subTest(unregistered=sql):
                with self.assertRaises(owned.ReadBounds):
                    owned._query_limit(sql, execution=True)
                with self.assertRaises(ValueError):
                    owned._query_limit(sql)

    def test_each_ex2_recovery_marker_refuses_before_any_database_or_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markers = ({'mode': ex2.MODE}, {'logical_authority': {'format': ex2.FORMAT}},
                       {'operation_guard': {'format': ex2.GUARD_FORMAT}})
            for index, config in enumerate(markers):
                with self.subTest(marker=config):
                    archive = root / str(index)
                    archive.mkdir()
                    (archive / 'config.json').write_bytes(ex2.encode(config))
                    before = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
                    with mock.patch.object(finite.sqlite3, 'connect', side_effect=AssertionError('raw SQLite fallback')), mock.patch.object(owned.Owner, '__enter__', side_effect=AssertionError('Owner fallback')):
                        with self.assertRaises(ValueError):
                            finite.refuse_unqualified_recovery(archive)
                    self.assertEqual(sorted(str(path.relative_to(root)) for path in root.rglob('*')), before)

    def test_fixed_compiled_identity_and_legacy_maps_unchanged(self):
        self.assertEqual(ex2.policy_digest(), ex2.POLICY_SHA256)
        self.assertNotIn(ex2.MODE, finite.MODES)
        self.assertNotIn(ex2.GUARD_FORMAT, finite.GUARD_FORMATS)
        self.assertEqual(set(finite.PROFILE_MODES), {1, 2, 3, 4})
        self.assertFalse(ex2.compiled_write('ex2-command', 'UPDATE users SET active=0 WHERE subject=?'))
        self.assertTrue(ex2.compiled_write('ex2-deny-auth', 'UPDATE users SET active=0 WHERE subject=?'))
        self.assertEqual(len(ex2.COMPILED['auth_mutations']), 7)

    def test_counter_edges_and_max_guard_reserve_are_independent_bounds(self):
        # No assertion here is represented as 350 real execution transactions.
        ordinary = {f'{i:032x}': {'essential': False} for i in range(96)}
        with self.assertRaises(GuardPaused):
            ex2.admission(dict(sequence=96, generation=385, operations=ordinary), False)
        ex2.admission(dict(sequence=96, generation=385, operations=ordinary), True)
        with self.assertRaises(GuardPaused):
            ex2.admission(dict(sequence=350, generation=1401, operations=ordinary), True)
        with self.assertRaises(GuardPaused):
            ex2.admission(dict(sequence=349, generation=1398, operations=ordinary), True)
        self.assertEqual(97 * 1024 + 2048, 101376)
        self.assertLessEqual(101376, 131072)
        self.assertEqual((ex2.COUNTERS['ordinary_transactions'], ex2.COUNTERS['safety_transactions'],
            ex2.COUNTERS['max_fresh_transactions'], ex2.COUNTERS['max_fresh_guard_generations_including_bootstrap']), (96, 254, 350, 1401))


if __name__ == '__main__':
    unittest.main()
