"""Disposable v4 ownership controls; no provider or storage-capacity claim."""
from contextlib import closing, contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research_workspace import finite_authority as f, operation_guard as g
from research_workspace import sqlite_ownership as o, bounded_export as be, evidence
from research_workspace import recovery as r, guarded_recovery as gr, runtime
from research_workspace.store import Store, AccessError, canonical
from research_workspace.recipes import expected_result
import test_bounded_export as exports
from test_guard_publication import PublicationFixture
from test_recovery import FakeBroker as RecoveryBroker
from test_workspace import CSV, IMAGES
from test_operation_guard import FakeBroker


class Fixture(PublicationFixture):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "state"
        self.config = {"version":1,"mode":f.SCOPED_MODE,"broker":sys.executable,"images":IMAGES,
                       "total_budget_cpu_seconds":600,"logical_authority":f.profile(4)}
        self.store = Store(self.root,create=True,guarded=True,config=self.config,subject="alice")
        self.key = self.store.initial_key; self.token = self.store.session(key=self.key)[1]
        self.subject = "alice"; self.broker = FakeBroker()
        self.data = {"name":"Synthetic SQLite ownership","question":"Does uncertain cleanup refuse success?",
                     "success_criterion":"Bounded values and retained uncertainty.",
                     "ends_at":(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=7)).isoformat(),
                     "budget_cpu_seconds":120}
        self.project = self.store.create_project("alice",self.data,self.broker,600,"project-request")
        self.pid = self.project["id"]

    @contextmanager
    def connections(self):
        opened=[]; lock=threading.Lock()
        def event(root,role,boundary):
            if root != self.root:return
            with lock:
                if boundary == "opened":opened.append(SimpleNamespace(role=role,thread=threading.get_ident(),closed=False))
                elif boundary == "closed":
                    pending=[x for x in opened if x.role==role and x.thread==threading.get_ident() and not x.closed]
                    self.assertTrue(pending); pending[-1].closed=True
        with patch.object(o,"_event",side_effect=event):yield opened
        self.assertTrue(all(x.closed for x in opened))

    def raw(self):
        db=sqlite3.connect(self.root / "workspace.sqlite3");db.row_factory=sqlite3.Row;return closing(db)

    def fill_caps(self, run):
        with self.raw() as db:
            for i in range(126):
                name=f"seeduser{i:03d}"
                db.execute("INSERT OR IGNORE INTO users VALUES(?,?,1)",(name,hashlib.sha256(name.encode()).hexdigest()))
            users=[row[0] for row in db.execute("SELECT subject FROM users ORDER BY subject")]
            self.assertEqual(len(users),128)
            projects=[self.pid]+[f"seedproject{i:02d}" for i in range(19)]
            for pid in projects[1:]:
                payload=dict(self.project,id=pid,status="closed")
                db.execute("INSERT INTO projects VALUES(?,?)",(pid,canonical(payload)))
            for i in range(999):
                rid=f"seedrun{i:04d}";pid=projects[i%20]
                payload=dict(run,id=rid,project_id=pid,status="failed",cleanup_verified=True)
                db.execute("INSERT INTO runs(id,project_id,subject,status,payload) VALUES(?,?,?,'failed',?)",(rid,pid,"alice",canonical(payload)))
            for pid in projects:
                for subject in users:
                    if db.execute("SELECT count(*) FROM members").fetchone()[0]>=1024:break
                    db.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(pid,subject,"viewer"))
            # Keep the original authenticated session; every other subject has
            # at most eight sessions and the table reaches its real 256 limit.
            subjects=[x for x in users if x!="alice"]
            for i in range(255):
                db.execute("INSERT INTO sessions VALUES(?,?,?)",(hashlib.sha256(f"cap-session-{i}".encode()).hexdigest(),subjects[i//8],time.time()+300))
            data=self.store.guard.load()
            ordinary=sum(not item["essential"] for item in data["operations"].values())
            for i in range(ordinary,g.MAX_OPERATIONS):
                oid=f"{1000000+i:032x}";data["sequence"]+=1
                item={"kind":"mutation","phase":"effect","resource_id":"workspace","subject":"alice",
                      "request_key":None,"request_hash":g.request_hash(None),"owner":g.BOOT,"reason":None,
                      "sequence":data["sequence"],"essential":False}
                data["operations"][oid]=item
                db.execute("INSERT INTO operation_receipts VALUES(?,?,?,?,0)",(oid,item["sequence"],self.store.guard.intent_digest(oid,item),"committed"))
            db.execute("UPDATE operation_authority SET sequence=?",(data["sequence"],));db.commit()
        # Synthetic finite proof fixture; no production bypass is introduced.
        (self.root/g.FILE).write_bytes(g.encode(data))
        with self.raw() as db:
            self.assertEqual([db.execute("SELECT count(*) FROM "+table).fetchone()[0] for table in ("users","projects","members","runs","sessions")],[128,20,1024,1000,256])


@contextmanager
def fresh():
    case=Fixture();case.setUp()
    try:yield case
    finally:case.tearDown()


@contextmanager
def fault(role, stage, *, after=False, secondary=False):
    """Fault at the native boundary, retain exact per-handle attempt counts."""
    native=sqlite3.connect; local=threading.local(); connections=[]; events=[]
    def event(root, current_role, boundary):
        if boundary == "before-open":local.role=current_role
        events.append((current_role,boundary))
    class Cursor:
        def __init__(self,db):self.db=db;self.close_calls=0
        def __getattr__(self,key):return getattr(self.db,key)
        def execute(self,sql,*args):
            if stage=="query" and sql.startswith("SELECT payload FROM projects"):
                raise OSError("primary query failure")
            if stage=="setup" and sql=="PRAGMA foreign_keys=ON":raise OSError("primary setup failure")
            return self.db.execute(sql,*args)
        def close(self):
            self.close_calls+=1;self.db.close()
            if stage=="cursor-close" or secondary:raise OSError("cursor close failure")
    class Connection:
        def __init__(self,db,selected):self.db=db;self.selected=selected;self.close_calls=0;self.rollback_calls=0;self.cursors=[]
        def __getattr__(self,key):return getattr(self.db,key)
        def cursor(self):
            if not self.selected:return self.db.cursor()
            cursor=Cursor(self.db.cursor());self.cursors.append(cursor);return cursor
        def close(self):
            self.close_calls+=1;self.db.close()
            if self.selected and (stage=="close" or secondary):raise OSError("connection close failure")
        def rollback(self):
            self.rollback_calls+=1
            if self.selected and stage=="rollback":raise OSError("rollback failure")
            return self.db.rollback()
        def backup(self,target):return self.db.backup(target.db)
        def commit(self):
            if self.selected and stage=="commit":
                if after:self.db.commit()
                raise OSError("commit failure")
            return self.db.commit()
    def connect(*args,**kw):
        selected=getattr(local,"role",None)==role
        if selected and stage=="open":raise OSError("open failure")
        result=Connection(native(*args,**kw),selected);connections.append(result);return result
    with patch.object(o,"_event",side_effect=event),patch.object(o.sqlite3,"connect",side_effect=connect):
        yield connections,events
    assert all(c.close_calls==1 for c in connections), [(c.selected,c.close_calls) for c in connections]
    assert all(c.rollback_calls<=1 for c in connections)
    assert all(cur.close_calls==1 for c in connections for cur in c.cursors)


class OwnershipTests(Fixture):
    def test_exact_fresh_binding_and_no_raw_factory_escape(self):
        self.assertEqual(f.profile_digest(4),"ed235fd6d6bb3c10632c0c5dc2bb20c8d3cb9076ebb157c71c1cc9c8072a656e")
        self.assertEqual(self.store.guard.binding["format"],f.SCOPED_GUARD_FORMAT)
        for factory in (self.store.connect,self.store._connect):
            with self.assertRaises(ValueError):factory()
        with self.store.read() as db:
            self.assertFalse(hasattr(db,"raw"));self.assertFalse(hasattr(db,"connection"))
            values=db.execute("SELECT subject FROM users ORDER BY subject")
            self.assertIsInstance(values,o.Values)
            self.assertFalse(hasattr(values,"connection"))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],0x45464134)
        self.assertEqual(values.fetchone()["subject"],"alice")
        with self.assertRaises(RuntimeError):db.execute("SELECT subject FROM users")
        with self.store.read() as db:
            with self.assertRaises(ValueError):db.execute("DELETE FROM users")
            with self.assertRaises(ValueError):db.execute("ATTACH DATABASE ':memory:' AS other")
        for flag in ("--finite-authority","--bounded-export","--bounded-guard-publication"):
            path=self.root.parent/"uncreated"
            result=subprocess.run([sys.executable,"-m","research_workspace","init","--state",str(path),"--broker",sys.executable,"--scoped-sqlite",flag],capture_output=True,text=True)
            self.assertEqual(result.returncode,2);self.assertFalse(path.exists())

    def test_online_read_guard_before_existing_ro_and_same_snapshot(self):
        original=sqlite3.connect;uris=[];observations=[]
        def connect(location,*args,**kw):
            self.assertGreater(self.store.guard.lease.depth,0)
            uris.append(str(location));return original(location,*args,**kw)
        def event(root,role,boundary):observations.append((role,boundary))
        with patch.object(o.sqlite3,"connect",side_effect=connect),patch.object(o,"_event",side_effect=event):
            with self.store.read() as db:
                self.assertEqual(db.execute("SELECT payload FROM projects WHERE id=?",(self.pid,)).fetchone()[0],canonical(self.project))
        self.assertTrue(all(u==":memory:" or u.endswith("?mode=ro") for u in uris))
        self.assertIn(("online-read","rolled-back"),observations)
        self.assertEqual(observations[-1],("online-read","closed"))
        self.assertEqual(self.store.guard.lease.depth,0)

    def test_read_bounds_and_clean_query_error_do_not_poison(self):
        for column,value in (("payload","x"*32769),("payload",b"bytes")):
            with fresh() as c:
                with c.raw() as db:db.execute("UPDATE projects SET payload=? WHERE id=?",(value,c.pid));db.commit()
                with c.store.read() as db:
                    with self.assertRaises(o.ReadBounds):db.execute("SELECT payload FROM projects WHERE id=?",(c.pid,))
                self.assertFalse(o.held(c.root))
        with fault("online-read","query"):
            with self.assertRaisesRegex(OSError,"primary query"):
                with self.store.read() as db:db.execute("SELECT payload FROM projects")
        self.assertFalse(o.held(self.root));self.assertEqual(self.store.projects("alice"),[self.project])
        self.store.add_user("bob")

    def test_open_setup_cursor_rollback_close_failure_matrix(self):
        for stage,held in (("open",True),("setup",False),("query",False),("cursor-close",True),("rollback",True),("close",True)):
            with self.subTest(stage=stage),fresh() as c:
                with fault("online-read",stage) as (connections,events):
                    with self.assertRaises(OSError):
                        with c.store.read() as db:db.execute("SELECT payload FROM projects")
                self.assertEqual(o.held(c.root),held)
                self.assertEqual(c.store.guard.lease.depth,0)
                if held:
                    with patch.object(o.sqlite3,"connect",side_effect=AssertionError("held owner reopened")):
                        with self.assertRaises(o.OwnershipFailure):c.store.revoke_user("alice")
                        self.assertEqual(c.store.operation_status()["reason"],"sqlite_ownership_unconfirmed")
                        with self.assertRaises(o.OwnershipFailure):Store(c.root)
                        with self.assertRaises(o.OwnershipFailure):runtime.Worker(c.store,c.broker).start()
                else:self.assertEqual(c.store.operation_status()["state"],"ready")

    def test_caught_cursor_close_failure_cannot_turn_scope_exit_into_success(self):
        native=sqlite3.connect
        class Cursor:
            def __init__(self,db):self.db=db;self.selected=False
            def __getattr__(self,key):return getattr(self.db,key)
            def execute(self,sql,*args):self.selected=sql=="SELECT payload FROM projects";return self.db.execute(sql,*args)
            def close(self):
                self.db.close()
                if self.selected:raise OSError("uncertain cursor close")
        class Connection:
            def __init__(self,db):self.db=db
            def __getattr__(self,key):return getattr(self.db,key)
            def cursor(self):return Cursor(self.db.cursor())
        with patch.object(o.sqlite3,"connect",side_effect=lambda *a,**kw:Connection(native(*a,**kw))):
            with self.assertRaisesRegex(OSError,"uncertain cursor close"):
                with self.store.read() as db:
                    try:db.execute("SELECT payload FROM projects")
                    except OSError:pass
        self.assertTrue(o.held(self.root))

    def test_nested_business_barrier_and_diagnostic_business_reads_are_refused(self):
        with self.assertRaises(o.OwnershipFailure):
            with self.store.write() as db:
                db.execute("BEGIN IMMEDIATE")
                with patch.object(o.sqlite3,"connect",side_effect=AssertionError("nested barrier opened")):
                    with self.assertRaises(ValueError):self.store.guard.barrier()
        # A separate root covers diagnostic-role restrictions, independent of
        # the deliberately failed barrier's required process hold.
        with fresh() as c:
            token=c.store.guard.begin("mutation")
            with c.store.guard.locked(),o.diagnostic(token):
                with self.assertRaises(o.OwnershipFailure):
                    with c.store.write():pass
                with o.Owner(c.root,"guard-proof",guard=c.store.guard,readonly=True) as db:
                    with self.assertRaises(ValueError):db.execute("SELECT payload FROM projects")
            with c.store.guard.locked():
                with self.assertRaises(o.OwnershipFailure):
                    with o.diagnostic(token):pass
            token.release()

    def test_owner_reentry_reuse_and_wrong_thread_refuse_before_any_open(self):
        owner=o.Owner(self.root,"guard-proof",guard=self.store.guard,readonly=True)
        with self.connections() as opened:
            with self.assertRaisesRegex(RuntimeError,"owning thread"):
                owner.__exit__(None,None,None)
            failures=[]
            def wrong_thread():
                try:
                    with owner:failures.append("wrong-thread owner opened")
                except RuntimeError as error:
                    if "single-use" not in str(error):failures.append(str(error))
                except BaseException as error:failures.append(repr(error))
            thread=threading.Thread(target=wrong_thread);thread.start();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertEqual(failures,[]);self.assertEqual(opened,[])
            with owner as db:
                before=len(opened);depth=self.store.guard.lease.depth
                def wrong_exit():
                    try:
                        owner.__exit__(None,None,None);failures.append("wrong-thread exit succeeded")
                    except RuntimeError as error:
                        if "owning thread" not in str(error):failures.append(str(error))
                    except BaseException as error:failures.append(repr(error))
                thread=threading.Thread(target=wrong_exit);thread.start();thread.join(3)
                self.assertFalse(thread.is_alive());self.assertEqual(failures,[])
                with self.assertRaisesRegex(RuntimeError,"single-use"):
                    with owner:pass
                self.assertEqual(len(opened),before);self.assertEqual(self.store.guard.lease.depth,depth)
                self.assertTrue(db.execute("PRAGMA user_version").fetchone()[0])
            before=len(opened)
            with self.assertRaisesRegex(RuntimeError,"single-use"):
                with owner:pass
            self.assertEqual(len(opened),before)
        self.assertEqual(self.store.guard.lease.depth,0);self.assertFalse(o.held(self.root))

    def test_diagnostic_role_rejects_join_union_subquery_and_business_pragma(self):
        token=self.store.guard.begin("mutation");o.hold(self.root)
        try:
            with self.store.guard.locked(),o.diagnostic(token):
                with o.Owner(self.root,"guard-proof",guard=self.store.guard,readonly=True) as db:
                    for sql in ["SELECT u.subject FROM operation_authority a JOIN users u ON 1=1",
                                "SELECT authority_id FROM operation_authority UNION SELECT subject FROM users",
                                "SELECT (SELECT subject FROM users LIMIT 1) AS authority_id FROM operation_authority",
                                "PRAGMA table_info(users)","SELECT id FROM runs WHERE status='queued' LIMIT 21"]:
                        with self.subTest(sql=sql),self.assertRaises(ValueError):db.execute(sql)
                    self.assertEqual(len(db.execute("SELECT authority_id,sequence FROM operation_authority").fetchall()),1)
        finally:token.release()

    def test_proof_writer_roles_cannot_mutate_or_observe_business_rows(self):
        before=(self.root/g.FILE).read_bytes()
        for role in ("barrier","started-receipt"):
            with self.subTest(role=role),self.store.write(role=role) as db:
                for sql,args in [("UPDATE users SET active=0 WHERE subject=?",("alice",)),
                                 ("SELECT subject FROM users",()),
                                 ("UPDATE operation_authority SET barrier=(SELECT subject FROM users LIMIT 1)",())]:
                    with self.assertRaises(ValueError):db.execute(sql,args)
        with self.raw() as db:self.assertEqual(db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0],1)
        self.assertEqual((self.root/g.FILE).read_bytes(),before);self.assertFalse(o.held(self.root))

    def test_schema_scripts_cannot_attach_file_authority(self):
        token=self.store.guard.begin("mutation");before=(self.root/g.FILE).read_bytes()
        escaped=str(self.root/"workspace.sqlite3").replace("'","''")
        attack="ATTACH DATABASE '"+escaped+"' AS authority; UPDATE authority.users SET active=0 WHERE subject='alice'; DETACH DATABASE authority;"
        o.hold(self.root)
        try:
            with self.store.guard.locked(),o.diagnostic(token):
                with o.Owner(self.root,"memory-reference",guard=self.store.guard,memory=True,initialize=True,validate=False) as db:
                    for script in (attack,"CREATE TABLE injected(value)"):
                        with self.assertRaises(ValueError):db.executescript(script)
                    with self.assertRaises(ValueError):db.execute(attack)
        finally:token.release()
        with self.raw() as db:self.assertEqual(db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0],1)
        self.assertEqual((self.root/g.FILE).read_bytes(),before)
        with fresh() as c:
            for role in ("backup-destination","initialize"):
                with o.Owner(c.root,role,guard=c.store.guard,path=c.root.parent/(role+".sqlite3"),initialize=True,validate=False) as db:
                    with self.assertRaises(ValueError):db.executescript(attack)
                    with self.assertRaises(ValueError):db.executescript("CREATE TABLE injected(value)")
                    with self.assertRaises(ValueError):db.execute("CREATE TABLE injected(value)")

    def test_primary_spawn_and_offline_errors_survive_rollback_failure(self):
        run=self.submit();run=self.store.claim()
        with fault("dispatch-reservation","rollback"):
            with self.assertRaisesRegex(ValueError,"original spawn") as caught:
                self.store.launch_run(run,lambda:(_ for _ in ()).throw(ValueError("original spawn")))
        self.assertTrue(any("transaction cleanup" in n for n in caught.exception.__notes__))
        with fresh() as c:
            with fault("offline-freeze","rollback"):
                with self.assertRaisesRegex(ValueError,"original frozen"):
                    with r.frozen_db(c.root):raise ValueError("original frozen")

    def test_postspawn_cleanup_failure_stops_observer_after_sql_scope_ends(self):
        self.submit();run=self.store.claim();process=Mock();observed=[]
        def stop(value):
            self.assertIs(value,process)
            self.assertEqual(self.store.guard.lease.depth,0)
            self.assertEqual(getattr(o._local,"owners",[]),[])
            self.assertTrue(o.held(self.root));observed.append("stopped")
        with fault("dispatch-reservation","close"),patch.object(runtime.subprocess,"Popen",return_value=process),patch.object(runtime,"stop_observer",side_effect=stop),patch.object(runtime,"collect",side_effect=AssertionError("must not collect")):
            with self.assertRaisesRegex(OSError,"connection close failure"):
                runtime.Worker(self.store,self.broker).execute(run)
        self.assertEqual(observed,["stopped"])
        with self.raw() as db:
            saved=json.loads(db.execute("SELECT payload FROM runs WHERE id=?",(run["id"],)).fetchone()[0])
        self.assertEqual(saved["status"],"running")
        self.assertFalse((self.root/"runs"/run["id"]/"result.json").exists())

    def test_guard_close_failure_preserves_original_and_holds_other_store(self):
        other=Store(self.root);original_close=g.os.close
        lock_inode=(self.root/g.LOCK).stat().st_ino
        def close(fd):
            is_lock=os.fstat(fd).st_ino==lock_inode
            original_close(fd)
            if is_lock:raise OSError("lost lock close")
        with patch.object(g.os,"close",side_effect=close):
            with self.assertRaisesRegex(ValueError,"original failure") as caught:
                with self.store.guard.locked():raise ValueError("original failure")
        self.assertTrue(caught.exception.__notes__)
        self.assertTrue(o.held(self.root))
        with patch.object(o.sqlite3,"connect",side_effect=AssertionError("held root reopened")):
            with self.assertRaises(o.OwnershipFailure):other.logout(self.token)

    def test_startup_and_barrier_failures_do_not_invent_diagnostic_intents(self):
        for role,stage in (("discovery","open"),("barrier","commit"),("barrier","close")):
            with self.subTest(role=role,stage=stage),fresh() as c:
                before=(c.root/g.FILE).read_bytes()
                with fault(role,stage):
                    with self.assertRaises(OSError):Store(c.root)
                self.assertTrue(o.held(c.root));self.assertEqual((c.root/g.FILE).read_bytes(),before)

    def test_original_error_survives_multiple_cleanup_failures(self):
        # A setup query fails before native cursor/connection close failures.
        with fault("online-read","setup",secondary=True):
            with self.assertRaisesRegex(OSError,"primary setup") as caught:
                self.store.projects("alice")
        notes=caught.exception.__notes__
        self.assertTrue(any("cursor" in n for n in notes));self.assertTrue(any("connection" in n for n in notes))
        self.assertTrue(o.held(self.root))

    def test_hold_is_shared_by_resolved_root_threads_and_constructors(self):
        second=Store(self.root);alias=self.root.parent/"alias";alias.symlink_to(self.root,target_is_directory=True)
        alias_store=Store(alias)
        with fault("online-read","close"):
            with self.assertRaises(OSError):self.store.projects("alice")
        errors=[]
        def action(store):
            try:store.revoke_user("alice")
            except o.OwnershipFailure:errors.append(True)
        threads=[threading.Thread(target=action,args=(s,)) for s in (second,alias_store)]
        for t in threads:t.start()
        for t in threads:t.join(3);self.assertFalse(t.is_alive())
        self.assertEqual(errors,[True,True])
        with self.raw() as db:self.assertEqual(db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0],1)
        with fresh() as other:other.store.add_user("independent-root")
        self.assertTrue(o.held(self.root));self.assertTrue(o.held(alias))

    def test_diagnostic_allowance_is_once_per_token_not_nested_owner(self):
        token=self.store.guard.begin("mutation")
        o.hold(self.root)
        opened=[]
        with patch.object(o,"_event",side_effect=lambda root,role,b:opened.append(role) if b=="opened" else None):
            token.fail(error=RuntimeError("original"))
            count=len(opened)
            token.fail(error=RuntimeError("repeat"))
        self.assertGreater(count,0);self.assertEqual(len(opened),count)
        self.assertEqual(set(opened),{"guard-proof","memory-reference"})
        self.assertTrue(token._ownership_diagnostic_used)
        self.assertEqual(self.store.guard.load()["operations"][token.identity]["phase"],"uncertain")
        self.assertTrue(o.held(self.root))

    def test_failed_diagnostic_and_untokened_failure_do_not_open_another_owner(self):
        token=self.store.guard.begin("mutation");o.hold(self.root)
        with fault("guard-proof","open") as (_,events):
            token.fail(error=RuntimeError("original"));token.fail(error=RuntimeError("repeat"))
        self.assertEqual(events.count(("guard-proof","before-open")),1)
        before=self.store.guard.load()["sequence"]
        self.store.guard.fail(origin="ordinary",error=RuntimeError("untokened"))
        self.assertEqual(self.store.guard.load()["sequence"],before)
        self.assertTrue(o.held(self.root))

    def test_clean_denial_and_two_queued_runs_do_not_trigger_uniqueness_refusal(self):
        with self.assertRaises(ValueError):self.store.add_user("alice")
        self.assertEqual(self.store.operation_status()["state"],"ready")
        one=self.submit("one");two=self.submit("two")
        claimed=self.store.claim();self.assertEqual(claimed["id"],one["id"])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT status FROM runs WHERE id=?",(two["id"],)).fetchone()[0],"queued")
        self.assertFalse(o.held(self.root))

    def test_bootstrap_missing_stripped_and_mixed_binding_never_creates_database(self):
        for kind in ("missing-db","missing-lock","stripped-config","mixed-policy"):
            with self.subTest(kind=kind),fresh() as c:
                if kind=="missing-db":(c.root/"workspace.sqlite3").rename(c.root/"retained.sqlite3")
                if kind=="missing-lock":(c.root/g.LOCK).rename(c.root/"retained.lock")
                if kind in {"stripped-config","mixed-policy"}:
                    config=json.loads((c.root/"config.json").read_text())
                    if kind=="stripped-config":
                        config.pop("operation_guard");config.pop("logical_authority");config["mode"]="local-public-data"
                    else:config["logical_authority"]=f.profile(3)
                    (c.root/"config.json").write_text(canonical(config))
                with self.assertRaises((ValueError,g.GuardPaused,OSError)):Store(c.root)
                if kind=="missing-db":self.assertFalse((c.root/"workspace.sqlite3").exists())
                if kind=="missing-lock":self.assertFalse((c.root/g.LOCK).exists())

    def test_two_process_read_snapshot_then_artifact_wait_does_not_retain_reader(self):
        reader_code='''
import sys
from research_workspace.store import Store
s=Store(sys.argv[1],resume=False)
with s.read() as db:
    db.execute("SELECT payload FROM projects").fetchall()
    print('snapshot',flush=True)
    sys.stdin.readline()
print('artifact-wait',flush=True)
sys.stdin.readline()
'''
        writer_code="import sys;from research_workspace.store import Store;print('started',flush=True);s=Store(sys.argv[1],resume=False);s.revoke_user('alice');print('revoked',flush=True)"
        a=subprocess.Popen([sys.executable,"-c",reader_code,str(self.root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        b=None
        try:
            self.assertEqual(a.stdout.readline().strip(),"snapshot")
            b=subprocess.Popen([sys.executable,"-c",writer_code,str(self.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            self.assertEqual(b.stdout.readline().strip(),"started")
            # B has started before attempting its guarded bootstrap.
            time.sleep(.1);self.assertIsNone(b.poll())
            a.stdin.write("release\n");a.stdin.flush()
            self.assertEqual(a.stdout.readline().strip(),"artifact-wait")
            out,err=b.communicate(timeout=10);self.assertEqual(b.returncode,0,err);self.assertIn("revoked",out)
            self.assertIsNone(a.poll())
        finally:
            if a.poll() is None:a.stdin.write("done\n");a.stdin.flush()
            _,err=a.communicate(timeout=10);self.assertEqual(a.returncode,0,err)
            if b is not None and b.poll() is None:b.kill();b.communicate()

    def test_predispatch_refusal_retains_token_through_close(self):
        for action in ("cancel","revoke","close","expired"):
            with self.subTest(action=action),fresh() as c:
                run=c.submit()
                if action=="cancel":c.store.cancel(c.pid,run["id"],"alice")
                elif action=="revoke":c.store.revoke_user("alice")
                elif action=="close":c.store.close_project(c.pid,"alice")
                else:
                    with c.store.transaction(essential=True) as db:
                        payload=dict(c.project,ends_at="2000-01-01T00:00:00+00:00")
                        db.execute("UPDATE projects SET payload=? WHERE id=?",(canonical(payload),c.pid))
                seen=[]
                def event(root,role,boundary):
                    if role=="mutation" and boundary in {"committed","closed"}:
                        self.assertEqual(len(c.store.guard.lease.active),1);seen.append(boundary)
                with patch.object(o,"_event",side_effect=event):self.assertIsNone(c.store.claim())
                self.assertEqual(seen,["committed","closed"])
                c.store.add_user("after-clean-refusal")
                self.assertEqual(c.store.operation_status()["state"],"ready")

    def test_claim_commit_and_close_failure_preserve_uncertainty(self):
        for stage,after in (("commit",False),("commit",True),("close",False)):
            with self.subTest(stage=stage,after=after),fresh() as c:
                run=c.submit();c.store.cancel(c.pid,run["id"],"alice")
                with fault("mutation",stage,after=after):
                    with self.assertRaises(OSError):c.store.claim()
                self.assertEqual(c.store.operation_status()["state"],"paused")
                self.assertTrue(any(v["kind"]=="run" and v["phase"]=="uncertain" for v in c.store.guard.load()["operations"].values()))
                with c.raw() as db:
                    self.assertEqual(db.execute("SELECT status FROM runs WHERE id=?",(run["id"],)).fetchone()[0],"queued" if stage=="commit" and not after else "cancelled")

    def test_unrelated_publication_qualifies_before_request_and_final_spawn_fenced(self):
        for late in (False,True):
            with self.subTest(late=late),fresh() as c:
                c.submit();run=c.store.claim()
                c.child("import json,sys;from research_workspace.store import Store;s=Store(sys.argv[1]);s.logout('unrelated');print(json.dumps(True))")
                order=[];active=[];write=runtime.private_write;native_popen=subprocess.Popen
                def event(root,role,boundary):
                    if boundary=="opened":active.append(role)
                    elif boundary=="closed":active.remove(role)
                    if role=="barrier" and boundary=="before-open":
                        self.assertNotIn("mutation",active);self.assertNotIn("dispatch-reservation",active);order.append("barrier")
                def publish(path,content):
                    write(path,content)
                    if path.name=="request.private.json":
                        self.assertEqual(active,[]);order.append("request")
                        if late:c.child("import json,sys;from research_workspace.store import Store;s=Store(sys.argv[1]);s.cancel("+repr(c.pid)+","+repr(run['id'])+",'alice');print(json.dumps(True))")
                def spawn(command,**kwargs):
                    if command[0]==sys.executable:return native_popen(command,**kwargs)
                    self.assertIn("dispatch-reservation",active);self.assertGreater(c.store.guard.lease.depth,0);order.append("spawn");return Mock(returncode=0)
                receipt={"schema_version":1,"request_id":run['id'],"project_id":c.pid,"image":run['environment']['image'],"status":"succeeded","admitted":True,"execution_started":True,"cleanup_verified":True,"unresolved_liability":False,"stdout":json.dumps(expected_result(CSV,"baseline")),"stderr":"","error":"","exit_code":0,"compute":{"reserved_cpu_seconds":30,"charged_cpu_seconds":30,"provenance":"estimated_upper_bound"}}
                def collect(*args,**kw):self.assertEqual(active,[]);return json.dumps(receipt).encode(),b""
                with patch.object(o,"_event",side_effect=event),patch.object(runtime,"private_write",side_effect=publish),patch.object(runtime.subprocess,"Popen",side_effect=spawn),patch.object(runtime,"collect",side_effect=collect):
                    runtime.Worker(c.store,c.broker).execute(run)
                self.assertEqual(order,["barrier","request","barrier"] if late else ["barrier","request","spawn"])
                saved=c.store.run(c.pid,run['id'],"alice")
                self.assertEqual(saved['status'],"cancelled" if late else "succeeded")
                self.assertEqual(saved['compute']['charged_cpu_seconds'],0 if late else 30)
                self.assertEqual(active,[])

    def test_offline_backup_distinct_owners_freeze_and_restore_refusal(self):
        RecoveryBroker("unused",self.root/"broker").write({"admission":"active","unresolved":[],"projects":[]})
        target=self.root.parent/"backup";events=[];active=[]
        original_call=RecoveryBroker.call
        def event(root,role,boundary):
            events.append((role,boundary))
            if boundary=="opened":active.append(role)
            if boundary=="closed":active.remove(role)
            if role in {"backup-source","backup-destination"} and boundary=="opened":self.assertIn("offline-freeze",active)
        def call(broker,*args):
            self.assertEqual(active,[]);self.assertEqual(self.store.guard.lease.depth,0)
            return original_call(broker,*args)
        with patch.object(r,"Broker",RecoveryBroker),patch.object(gr,"Broker",RecoveryBroker),patch.object(o,"_event",side_effect=event),patch.object(RecoveryBroker,"call",call):
            report=r.checkpoint(self.root,target)
        self.assertEqual(events.count(("offline-freeze","opened")),2)
        self.assertIn(("backup-source","closed"),events);self.assertIn(("backup-destination","closed"),events)
        self.assertTrue((target/"manifest.json").is_file())
        with patch.object(gr,"Broker") as provider:
            with self.assertRaisesRegex(ValueError,"not qualified"):
                r.restore(target,self.root.parent/"replacement",trusted_sha256=report['checkpoint_sha256'],broker="unused")
            provider.assert_not_called()
        self.assertFalse((self.root.parent/"replacement").exists())

    def test_backup_failure_retains_partial_output_without_manifest_or_provider(self):
        for role in ("backup-source","backup-destination","offline-freeze"):
            with self.subTest(role=role),fresh() as c:
                RecoveryBroker("unused",c.root/"broker").write({"admission":"active","unresolved":[],"projects":[]})
                target=c.root.parent/"partial"
                with patch.object(gr,"Broker") as provider,fault(role,"close"):
                    with self.assertRaises(OSError):r.checkpoint(c.root,target)
                    provider.assert_not_called()
                self.assertTrue(target.exists());self.assertFalse((target/"manifest.json").exists());self.assertTrue(o.held(c.root))

    def test_full_real_row_and_intent_caps_keep_each_clean_safety_control_available(self):
        for name in ("logout","revoke","rotate","demote","remove","cancel","close","settle","recover"):
            with self.subTest(action=name),fresh() as c:
                actions=c.safety_actions()
                with c.store.read() as db:
                    run=json.loads(db.execute("SELECT payload FROM runs WHERE status='running'").fetchone()[0])
                c.fill_caps(run)
                with self.assertRaises((f.LogicalLimit,g.GuardPaused)):c.store.add_user("over-cap")
                actions[name]()
                self.assertFalse(o.held(c.root))
                with c.raw() as db:
                    self.assertEqual([db.execute("SELECT count(*) FROM "+table).fetchone()[0] for table in ("users","projects","runs")],[128,20,1000])
                    self.assertEqual(db.execute("SELECT count(*) FROM operation_receipts WHERE essential=0").fetchone()[0],4096)
                    if name=="revoke":self.assertEqual(db.execute("SELECT active FROM users WHERE subject='bob'").fetchone()[0],0)
                    if name=="settle":
                        saved=json.loads(db.execute("SELECT payload FROM runs WHERE id=?",(run['id'],)).fetchone()[0])
                        self.assertEqual(saved['compute']['charged_cpu_seconds'],30)
                        self.assertFalse(saved['compute']['unresolved_liability'])

    def test_held_revocation_and_settlement_preserve_access_and_debt(self):
        self.store.add_user("bob");run=self.settled()
        before=self.database()
        with fault("online-read","close"):
            with self.assertRaises(OSError):self.store.projects("alice")
        with self.assertRaises(o.OwnershipFailure):self.store.revoke_user("bob")
        with self.assertRaises(o.OwnershipFailure):self.store.finish(run,run['metrics'],claimed=True)
        self.assertEqual(self.database(),before)
        with self.raw() as db:
            self.assertEqual(db.execute("SELECT active FROM users WHERE subject='bob'").fetchone()[0],1)
            saved=json.loads(db.execute("SELECT payload FROM runs WHERE id=?",(run['id'],)).fetchone()[0])
            self.assertTrue(saved['compute']['unresolved_liability'])
        self.assertFalse((self.root/'runs'/run['id']/'result.json').exists())

    def test_actual_unclosed_writer_blocks_other_process_even_after_guard_unwinds(self):
        native=sqlite3.connect;retained=[]
        class Unclosed:
            def __init__(self,db):self.db=db;self.close_calls=0;self.rollback_calls=0
            def __getattr__(self,key):return getattr(self.db,key)
            def rollback(self):self.rollback_calls+=1;raise OSError("rollback unconfirmed")
            def close(self):self.close_calls+=1;raise OSError("close unconfirmed")
        def connect(location,*args,**kw):
            db=native(location,*args,**kw)
            if str(location).endswith('?mode=rw'):
                wrapped=Unclosed(db);retained.append(wrapped);return wrapped
            return db
        try:
            with patch.object(o.sqlite3,"connect",side_effect=connect):
                with self.assertRaisesRegex(RuntimeError,"original body") as caught:
                    with self.store.write() as db:
                        db.execute("BEGIN IMMEDIATE")
                        raise RuntimeError("original body")
            self.assertTrue(o.held(self.root));self.assertEqual(self.store.guard.lease.depth,0)
            self.assertEqual([(x.close_calls,x.rollback_calls) for x in retained],[(1,1)])
            code="import sqlite3,sys; db=sqlite3.connect(sys.argv[1],timeout=.1);\ntry: db.execute('BEGIN IMMEDIATE')\nexcept sqlite3.OperationalError: print('native contention')\nfinally: db.close()"
            child=subprocess.run([sys.executable,"-c",code,str(self.root/'workspace.sqlite3')],capture_output=True,text=True,timeout=3)
            self.assertEqual(child.returncode,0,child.stderr);self.assertIn('native contention',child.stdout)
            self.assertTrue(any('transaction cleanup' in n for n in caught.exception.__notes__))
        finally:
            # Test harness releases its disposable native handles; production
            # has no retry/reset path and the process hold remains latched.
            for x in retained:x.db.close()
        self.assertTrue(o.held(self.root))

    def test_phase_two_freeze_close_failure_prevents_final_manifest(self):
        RecoveryBroker("unused",self.root/'broker').write({'admission':'active','unresolved':[],'projects':[]})
        native=sqlite3.connect;local=threading.local();freezes=0
        def event(root,role,boundary):
            nonlocal freezes
            if role=='offline-freeze' and boundary=='before-open':freezes+=1
            if boundary=='before-open':local.role=role
        class CloseFailure:
            def __init__(self,db):self.db=db
            def __getattr__(self,key):return getattr(self.db,key)
            def close(self):self.db.close();raise OSError('second freeze close')
        def connect(*args,**kw):
            db=native(*args,**kw)
            return CloseFailure(db) if getattr(local,'role',None)=='offline-freeze' and freezes==2 else db
        target=self.root.parent/'second-phase'
        with patch.object(gr,'Broker',RecoveryBroker),patch.object(o,'_event',side_effect=event),patch.object(o.sqlite3,'connect',side_effect=connect):
            with self.assertRaisesRegex(OSError,'second freeze close'):r.checkpoint(self.root,target)
        self.assertEqual(freezes,2);self.assertTrue((target/'broker').exists());self.assertFalse((target/'manifest.json').exists())
        self.assertTrue(o.held(self.root))

    def test_fingerprint_change_during_broker_wait_refuses_manifest(self):
        RecoveryBroker("unused",self.root/'broker').write({'admission':'active','unresolved':[],'projects':[]})
        original=RecoveryBroker.call
        def call(broker,*args):
            result=original(broker,*args);self.store.revoke_user('alice');return result
        target=self.root.parent/'changed'
        with patch.object(gr,'Broker',RecoveryBroker),patch.object(RecoveryBroker,'call',call):
            with self.assertRaisesRegex(ValueError,'changed during checkpoint'):r.checkpoint(self.root,target)
        self.assertFalse((target/'manifest.json').exists())

    def test_recovery_status_closes_reader_before_provider_wait(self):
        RecoveryBroker("unused",self.root/'broker').write({'admission':'active','unresolved':[],'projects':[]})
        original=RecoveryBroker.call
        with self.connections() as opened:
            def call(broker,*args):
                self.assert_released(opened)
                return original(broker,*args)
            with patch.object(r,'Broker',RecoveryBroker),patch.object(RecoveryBroker,'call',call):
                report=r.recovery_status(self.root)
        self.assertFalse(report['automatic_replay'])

    def test_http_held_logout_visibly_refuses_without_clearing_cookie(self):
        import http.client
        from research_workspace.server import Application,Handler,Server
        app=Application.__new__(Application);app.store=self.store;app.config=self.config;app.identity=None
        server=Server(('127.0.0.1',0),Handler);server.app=app
        o.hold(self.root)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        try:
            conn.request('DELETE','/api/session',headers={'Cookie':'research_session='+self.token,'X-Research-Request':'1','Origin':f'http://127.0.0.1:{server.server_port}'})
            response=conn.getresponse();body=json.loads(response.read())
            self.assertEqual(response.status,503);self.assertIsNone(response.getheader('Set-Cookie'))
            self.assertEqual(body['operation_status']['reason'],'sqlite_ownership_unconfirmed')
        finally:conn.close();server.shutdown();thread.join(3);server.server_close()
        with self.raw() as db:self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],1)

    test_slow_artifacts_and_zip_release_readers = exports.BoundedExportTests.test_blocked_artifacts_and_zip_release_readers_for_safety
    test_export_rechecks_authority = exports.BoundedExportTests.test_final_check_rejects_membership_logout_expiry_close_and_new_run
    test_http_stream_releases_readers = exports.BoundedExportTests.test_http_stream_has_no_readers_or_locks_and_no_late_retraction


if __name__ == "__main__":unittest.main()
