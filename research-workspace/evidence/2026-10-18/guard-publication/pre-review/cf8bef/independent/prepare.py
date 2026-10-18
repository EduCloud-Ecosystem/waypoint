"""Export immutable review source to a disposable directory; never modify source checkout."""
from pathlib import Path
import hashlib, io, json, subprocess, tarfile
ROOT = Path('/tmp/guard-publication-independent-20261018')
REPO = Path('/Users/gregchism/dev/.educloud-worktrees/waypoint-guard-publication-design')
HEAD = 'cf8befca9cef418d7e1238945872bed45d7fc6f2'
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip() == HEAD
assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=REPO)
DEST = ROOT / 'checkout'
if DEST.exists(): raise SystemExit('Disposable checkout already exists; preserve it and choose a fresh output directory.')
DEST.mkdir()
archive = subprocess.check_output(['git', 'archive', HEAD], cwd=REPO)
with tarfile.open(fileobj=io.BytesIO(archive)) as tar: tar.extractall(DEST, filter='data')
paths = subprocess.check_output(['git','ls-tree','-r','--name-only',HEAD,'research-workspace/research_workspace','research-workspace/tests','research-workspace/BOUNDED-GUARD-PUBLICATION-PROPOSAL.md'], cwd=REPO, text=True).splitlines()
report = {'head':HEAD, 'base':'66a48dfe29d61d3f6f48f016f2c474e1ac6af4f6', 'source_sha256': {p:hashlib.sha256((DEST/p).read_bytes()).hexdigest() for p in paths}}
(ROOT/'independent-source.json').write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps({'head':HEAD,'files':len(paths),'checkout':str(DEST)}))
