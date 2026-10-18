import pathlib,sys,json,copy
from unittest.mock import patch
BASE=pathlib.Path(__file__).resolve().parent;SOURCE=BASE/'source/research-workspace';sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
import qualify_scoped_docker as q
from test_scoped_docker_qualification import complete_report,synthetic_manifest,SimulatedWiringTests
results=[]
for case in ['missing-output-binding','wrong-output-binding','wrong-project-budget','extra-ledger-project']:
 original=q.Audit.ledger
 def changed_ledger(self):
  data=original(self)
  if case=='missing-output-binding':
   for row in data['runs'].values():
    row.pop('output_sha256',None);row.pop('output_bytes',None);row['output_error']='execution diagnostics could not be persisted'
  elif case=='wrong-output-binding':
   for row in data['runs'].values():
    row['output_sha256']='0'*64;row['output_bytes']=3;row['output_error']='missing diagnostics'
  elif case=='wrong-project-budget':
   for row in data['projects'].values():row['budget_cpu_seconds']=600
  elif case=='extra-ledger-project':
   data['projects']['unplanned-project']={'project_id':'unplanned-project','budget_cpu_seconds':600}
  return data
 with patch.object(q.Audit,'ledger',changed_ledger):
  report=SimulatedWiringTests().simulated()
 (BASE/(case+'-report.json')).write_text(json.dumps(report,indent=2)+'\n')
 results.append({'case':case,'observed_status':report['status'],'failure':report.get('failure'),'attempts':len(report['attempts']),'charged_cpu_seconds':report['charged_cpu_seconds'],'provider':'all external effects simulated by retained test fixture'})
print(json.dumps(results,indent=2));(BASE/'independent-ledger-probe-results.json').write_text(json.dumps(results,indent=2)+'\n')
