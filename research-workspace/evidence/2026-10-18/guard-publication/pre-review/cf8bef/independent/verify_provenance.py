"""Verify immutable Git blobs, acceptance hashes, and all prior evidence, read-only."""
from pathlib import Path
import hashlib,json,subprocess
BASE=Path(__file__).resolve().parent
REPO=Path('/Users/gregchism/dev/.educloud-worktrees/waypoint-guard-publication-design')
HEAD='cf8befca9cef418d7e1238945872bed45d7fc6f2'
PREVIOUS='66a48dfe29d61d3f6f48f016f2c474e1ac6af4f6'
FOLDER='research-workspace/evidence/2026-10-18/guard-publication/'
def git(*args):return subprocess.check_output(['git',*args],cwd=REPO)
def blob(rev,path):return git('show',rev+':'+path)
def sha(data):return hashlib.sha256(data).hexdigest()
manifest_bytes=blob(HEAD,FOLDER+'acceptance.json');manifest=json.loads(manifest_bytes)
failures=[]
for relative,digest in manifest['source_sha256'].items():
    if sha(blob(HEAD,relative))!=digest:failures.append('source:'+relative)
for relative,digest in manifest['files'].items():
    if sha(blob(HEAD,FOLDER+relative))!=digest:failures.append('evidence:'+relative)
prior=git('ls-tree','-r','--name-only',PREVIOUS,'research-workspace/evidence').decode().splitlines()
for path in prior:
    if blob(PREVIOUS,path)!=blob(HEAD,path):failures.append('prior:'+path)
reviewed=json.loads((BASE/'independent-source.json').read_text())
for path,digest in reviewed['source_sha256'].items():
    if sha(blob(HEAD,path))!=digest:failures.append('reviewed:'+path)
report={'head':HEAD,'base':PREVIOUS,'manifest_sha256':sha(manifest_bytes),'source_entries':len(manifest['source_sha256']),'evidence_entries':len(manifest['files']),'prior_evidence_files':len(prior),'reviewed_source_entries':len(reviewed['source_sha256']),'failures':failures,'result':'CLEAR' if not failures else 'MISMATCH'}
assert report['manifest_sha256']=='c8f04225df57beed2077d6c960e6765f636cc68167f9afd4795d13c31b638938'
(BASE/'provenance.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
raise SystemExit(bool(failures))
