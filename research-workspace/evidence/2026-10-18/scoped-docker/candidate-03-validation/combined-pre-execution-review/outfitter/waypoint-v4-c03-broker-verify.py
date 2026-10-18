import hashlib, json, shutil, subprocess, tarfile
from pathlib import Path
owner=Path('/Users/gregchism/dev/.educloud-worktrees/waypoint-v4-docker-qualification')
root=owner/'research-workspace'
snapshot=Path('/tmp/waypoint-v4-c03-broker-pdhkd2yg')
candidate=root/'evidence/2026-10-18/scoped-docker/candidate-03'
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def need(ok, message):
    if not ok: raise RuntimeError(message)
def git(*args): return subprocess.check_output(['git',*args], cwd=owner).decode().strip()
manifest=json.loads((candidate/'manifest.json').read_bytes())
need(sha(candidate/'manifest.json')=='ad701c8d5b164666133a1d06f241a80ab052ccbeb02a48c499a663589e537295','manifest changed')
need(sha(candidate/'source.tar.gz')==manifest['source_archive_sha256']=='6fcfce9bf3db5b57b4f503574f379123a43739c44fc69f9af9b40a8cc17055b5','archive changed')
with tarfile.open(candidate/'source.tar.gz','r:gz') as archive:
    members=archive.getmembers()
    need(len(members)==len(manifest['sources']) and {m.name for m in members}==set(manifest['sources']),'archive inventory mismatch')
    for member in members:
        need(member.isfile() and not member.name.startswith('/') and '..' not in Path(member.name).parts,'unsafe archive member')
        expected=manifest['sources'][member.name]
        need(hashlib.sha256(archive.extractfile(member).read()).hexdigest()==expected==sha(snapshot/member.name)==sha(root/member.name),'source drift '+member.name)
pin=json.loads((root/'qualification/broker-upgrade/pins.json').read_bytes())['candidate']
identity=json.loads((root/'evidence/2026-10-18/broker-upgrade/verification.json').read_bytes())['provenance']['candidate']
binary=Path(manifest['broker_path'])
attestation_path=binary.with_name('candidate.provenance.json')
attestation=json.loads(attestation_path.read_bytes())
metadata=subprocess.check_output(['go','version','-m',str(binary)]).decode().splitlines()[1:]
need(identity['source_commit']==manifest['broker_source_commit']=='5446438985e31cb739f2048a2aa2176918831a7e','wrong broker commit')
need(identity['sha256']==sha(binary)==manifest['broker_sha256']=='1bf1ce898d6d10e574ab7b2f79a830fa94fa740867e208470bfc15790dc41bb1','wrong broker binary')
need(identity['source_sha256']==pin['source_sha256'] and identity['source_tree']==pin['tree']=='b65c2c381a282f221e0cb8acfa2a47bf8b3fca4e','wrong source identity')
need(identity['build_manifest_sha256']==sha(attestation_path)=='8edd1334a8560c3c94a0e5d59f4804ac03566630f3d034a96d8930edbcf16e31','wrong attestation')
need(identity['build_metadata']==metadata==attestation['build_metadata']==manifest['prerequisites']['broker_build_metadata'],'wrong metadata')
need(attestation['format']=='waypoint-broker-build-v1' and attestation['pin']==pin and attestation['binary_sha256']==sha(binary) and attestation['pins_sha256']==sha(root/'qualification/broker-upgrade/pins.json'),'wrong attestation binding')
for name,expected in pin['source_sha256'].items():
    need(sha(binary.parent/'candidate-source'/name)==expected,'broker source drift '+name)
old=root/'evidence/2026-10-18/scoped-docker/pre-review/accounting-absence'
for name,info in json.loads((old/'preservation.json').read_bytes())['retained_files'].items():
    need(sha(old/name)==info['sha256']==sha(Path('/tmp')/name),'old red probe drift')
need(not git('diff','--name-only','--diff-filter=MDR','292431fa9ae3044db5f8cd82ddeda2cd33e750c8','HEAD','--','research-workspace/evidence'),'old evidence modified')
need(not git('diff','--name-only','0efc6fd734e7f812cabc3822d2ea93b8c1063752','HEAD','--','research-workspace/research_workspace','.github/workflows','research-workspace/images.lock.json','research-workspace/environments.json'),'accepted source boundary drift')
need(not git('status','--porcelain'),'owner checkout dirty')
need(not Path(manifest['output']).exists() and not Path(manifest['restart_marker']).exists(),'actual exercise started')
head=git('rev-parse','HEAD')
report=dict(verdict='CLEAR', scope='Independent exact candidate03 broker/accounting pre-execution source review only. Combined coordinating review remains required before bounded local execution.', attachment_commit=head,
    harness_commit=manifest['source_commit'],manifest_commit='ded6f113ea9149f947a41cb7a6d58841cd965211', manifest_sha256=sha(candidate/'manifest.json'),source_archive_sha256=sha(candidate/'source.tar.gz'),
    source_files_verified=len(manifest['sources']), broker_path=str(binary), broker_commit=identity['source_commit'],broker_tree=pin['tree'],broker_binary_sha256=sha(binary),broker_attestation_sha256=sha(attestation_path),broker_source_files_verified=len(pin['source_sha256']),
    findings=[], prior_findings={'wrong_broker_pin':'CLOSED: current binary, accepted report, attestation, metadata, pin and all 22 compiled sources agree with Outfitter PR16.', 'charge_loss_after_absence_failure':'CLOSED: exact scratch witness now persists 30 CPU seconds, dispatch entered and cleanup independently unverified before failure. Lower/missing/invalid/unrelated later observations cannot reduce known charge.'},
    tests={'pure_oracle_tests_normal':12,'pure_oracle_tests_optimized':12,'independent_scratch_probes_normal':3,'independent_scratch_probes_optimized':3,'marker_creating_tests_excluded':True,'full_457_suite':'Owner-reported only; not independently rerun in this review.'},
    observed_boundaries={'owner_checkout_clean':True,'owner_checkout_edits':0,'prior_failed_evidence_unchanged':True,'accepted_PR46_runtime_workflows_images_catalog_unchanged':True,'exercise_marker_exists':False,'exercise_output_exists':False,'broker_executions':0,'docker_commands':0,'actual_docker_attempts':0,'markers_created':0,'builds':0,'merges':0,'live_jetstream2':'BLOCKED pending user approval','export_review':'Coordinating reviewer owns complete export/provenance review; this review includes core final HTTP lineage oracle only.'},
    limitations=['Synthetic source/scratch evidence only; not actual Docker qualification, browser acceptance, a capacity guarantee or execution authorization by itself.','Exactly four attempts, two 60-second budgets, 120 conservative CPU seconds; no fifth, retry, resume, refund, build or pull.','Late revocation race remains NOT QUALIFIED; queued cancellation/access removal controls precede Worker.execute.','Source environment candidates remain disabled; available capacity UNKNOWN; quota_enforced false; historical fifth-headroom FAIL unchanged.'])
out=Path('/Users/gregchism/Documents/Codex/2026-10-18/waypoint-v4-candidate03-broker-accounting-review')
out.mkdir(exist_ok=False)
for source in ('waypoint-v4-c03-broker-accounting-probe.py','waypoint-v4-c03-broker-accounting-probe.log','waypoint-v4-c03-broker-accounting-probe-optimized.log','waypoint-v4-c03-broker-verify.py'):
    shutil.copyfile(Path('/tmp')/source,out/source)
report['evidence']={p.name:dict(sha256=sha(p),bytes=p.stat().st_size) for p in sorted(out.iterdir())}
(out/'review.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
print(json.dumps(dict(verdict=report['verdict'], attachment_commit=head,review_path=str(out/'review.json'),review_sha256=sha(out/'review.json'),manifest_sha256=report['manifest_sha256'],source_files_verified=report['source_files_verified'],broker_source_files_verified=report['broker_source_files_verified'],owner_clean=True,exercise_marker_exists=False,exercise_output_exists=False),indent=2))
