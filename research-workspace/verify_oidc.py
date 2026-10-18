"""Real, owned synthetic browser acceptance. Never prints credentials or raw HTTP traces."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

CLI = os.environ.get('PLAYWRIGHT_CLI', str(Path.home() / '.codex/skills/playwright/scripts/playwright_cli.sh'))


class Pilot:
    def __init__(self, root, cli=CLI):
        self.root, self.cli = Path(root).resolve(), cli
        self.manifest = json.loads((self.root / 'pilot.json').read_text())
        self.operator = json.loads((self.root / 'operator.private.json').read_text())
        self.config = json.loads((self.root / 'state/config.json').read_text())
        self.origin = self.manifest['origin']
        self.session = self.manifest['name']
        self.cookies = {}
        self.state_name = 'state'
        self.checks = []

    def check(self, name, condition):
        self.checks.append({'name': name, 'status': 'PASS' if condition else 'FAIL'})
        print(name + ': ' + self.checks[-1]['status'], flush=True)
        if not condition: raise AssertionError(name)

    def cli_call(self, *args):
        result = subprocess.run([self.cli, '--session', self.session, *args], capture_output=True, text=True, timeout=60)
        if result.returncode or '### Error' in result.stdout:
            detail = result.stdout.split('### Error',1)[-1].strip().splitlines()[0] if '### Error' in result.stdout else 'CLI process failed'
            import re
            detail = re.sub(r'https?://[^\s]+', '[URL omitted]', detail)
            for user in self.operator['users'].values(): detail = detail.replace(user['password'], '[credential omitted]')
            raise RuntimeError('Browser operation failed: ' + detail[:180])
        return result.stdout

    def browser(self, body):
        # A private ephemeral file avoids credentials in process arguments and
        # terminal history. Raw CLI output is parsed in memory and discarded.
        with tempfile.NamedTemporaryFile(mode='w', suffix='.private.js', dir=self.root, delete=False) as source:
            source.write('async (page) => { ' + body + ' }')
            path = Path(source.name)
        try: output = self.cli_call('run-code', '--filename', str(path))
        finally: path.unlink()
        if '### Result\n' in output:
            return json.loads(output.split('### Result\n', 1)[1].split('\n###', 1)[0])

    def private(self, body, values=None, admin=False):
        code = 'VALUES=' + repr(values or {}) + '\nROOT=' + repr(str(self.root)) + '\nSTATE=' + repr(self.state_name) + '\n'
        code += '''import json, time, urllib.request as u, urllib.parse as p
from pathlib import Path
from research_workspace.store import Store
config=json.loads(Path(ROOT+'/'+STATE+'/config.json').read_text())
identity=config['identity']
def request(method,url,data=None,headers=None):
    req=u.Request(url,data=data,method=method,headers=headers or {})
    with u.urlopen(req,timeout=6) as f: return json.loads(f.read() or 'null')
'''
        if admin:
            code += 'PASSWORD=' + repr(self.operator['admin_password']) + '\nREALM=' + repr(self.manifest['realm']) + '\n'
            code += '''base='http://keycloak:8080/idp'
data=p.urlencode({'client_id':'admin-cli','username':'operator','password':PASSWORD,'grant_type':'password'}).encode()
token=request('POST',base+'/realms/master/protocol/openid-connect/token',data)['access_token']
def admin(method,path,data=None):
    return request(method,base+'/admin/realms/'+REALM+path,json.dumps(data).encode() if data is not None else None,{'Authorization':'Bearer '+token,'Content-Type':'application/json'})
'''
        result = subprocess.run(['docker', 'compose', '-f', str(self.root / 'compose.private.json'), 'exec', '-T', 'app', 'python', '-'],
                                input=code + body, capture_output=True, text=True, timeout=30)
        if result.returncode: raise RuntimeError('Private verification operation failed; raw diagnostics discarded.')
        return json.loads(result.stdout)

    def login(self, role, expected=200):
        self.browser('await page.context().clearCookies(); await page.goto(' + json.dumps(self.origin) + ');')
        self.cli_call('snapshot')
        self.browser('await page.getByRole("button", {name:"Sign in with identity provider"}).press("Enter");')
        self.cli_call('snapshot')
        code = 'await page.getByRole("textbox", {name:"Username or email"}).fill(' + json.dumps(role) + ');'
        code += 'await page.getByRole("textbox", {name:"Password",exact:true}).fill(' + json.dumps(self.operator['users'][role]['password']) + ');'
        code += 'await page.getByRole("textbox", {name:"Password",exact:true}).press("Enter"); await page.waitForURL(url => url.origin === ' + json.dumps(self.origin) + ' && url.pathname === "/");'
        code += 'await page.waitForFunction(() => !document.getElementById("boot") || document.getElementById("boot").hidden);'
        self.browser(code)
        status = self.api('/api/session')['status']
        self.check(role + ' real browser sign-in ' + str(expected), status == expected)
        self.cookies[role] = self.browser('return await page.context().cookies();')
        return status

    def use(self, role):
        self.browser('await page.context().clearCookies(); await page.context().addCookies(' + json.dumps(self.cookies[role]) + ');')

    def api(self, path, method='GET', data=None, headers=None):
        options = {'method': method, 'headers': {'X-Research-Request': '1', **(headers or {})}}
        if data is not None:
            options['headers']['Content-Type'] = 'application/json'; options['data'] = json.dumps(data)
        return self.browser('const r=await page.request.fetch(' + json.dumps(self.origin + path) + ',' + json.dumps(options) + '); return {status:r.status(), headers:r.headers(), body: (r.headers()["content-type"]||"").includes("json") ? await r.json() : null};')

    def wait_run(self, pid, rid):
        for _ in range(50):
            response = self.api('/api/projects/' + pid + '/runs/' + rid)
            if response['status'] != 200: raise RuntimeError('Run observation denied with HTTP '+str(response['status']))
            if response['body']['status'] not in ('queued', 'running'): return response
            time.sleep(0.3)
        raise RuntimeError('Finite run observation timed out.')

    def stack(self, *args):
        subprocess.run(['docker', 'compose', '-f', str(self.root / 'compose.private.json'), *args], capture_output=True, check=True, timeout=90)

    def ready(self):
        import urllib.request
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen(self.origin + '/api/auth-config',timeout=2) as response:
                    if response.status != 200: raise OSError('Application unavailable')
                identity=self.private("url=identity['introspection_url'].split('/protocol/openid-connect/')[0]+'/.well-known/openid-configuration'\na=request('GET',url)\nwith u.urlopen('http://oauth2-proxy:4180/ping',timeout=6) as f: ready=f.status==200\nprint(json.dumps({'ready':ready and a['issuer']==identity['issuer']}))")
                if identity['ready']: return
            except (OSError,RuntimeError): pass
            time.sleep(1)
        raise RuntimeError('Owned application/identity services did not become ready within 120 seconds.')

    def token_response_control(self):
        import secrets, hashlib, base64
        from urllib.parse import urlencode
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        endpoint = self.config['identity']['issuer'] + '/protocol/openid-connect/auth?'
        url = endpoint + urlencode({'response_type': 'code', 'client_id': 'research-pilot', 'redirect_uri': self.origin + '/oauth2/callback',
              'scope': 'openid profile email', 'code_challenge': challenge, 'code_challenge_method': 'S256',
              'state': secrets.token_urlsafe(24), 'nonce': secrets.token_urlsafe(24), 'prompt': 'login'})
        self.browser('await page.context().clearCookies(); await page.route(' + json.dumps(self.origin + '/oauth2/callback?**') + ', route => route.fulfill({status:200,body:"Synthetic token-response control"})); await page.goto(' + json.dumps(url) + ');')
        self.cli_call('snapshot')
        code = self.browser('await page.getByRole("textbox", {name:"Username or email"}).fill("owner"); await page.getByRole("textbox", {name:"Password",exact:true}).fill(' + json.dumps(self.operator['users']['owner']['password']) + '); await page.getByRole("textbox", {name:"Password",exact:true}).press("Enter"); await page.waitForURL(url => url.pathname === "/oauth2/callback"); return new URL(page.url()).searchParams.get("code");')
        result = self.private('''endpoint=identity['introspection_url'].removesuffix('/introspect')
data=p.urlencode({'grant_type':'authorization_code','client_id':identity['client_id'],'client_secret':identity['client_secret'], 'code':VALUES['code'],'code_verifier':VALUES['verifier'],'redirect_uri':identity['public_origin']+'/oauth2/callback'}).encode()
response=request('POST',endpoint,data)
print(json.dumps({'access_token_issued':bool(response.get('access_token')),'refresh_token_issued':bool(response.get('refresh_token'))}))
''', {'code': code, 'verifier': verifier})
        self.check('real PKCE token response has no refresh token', result['access_token_issued'] and not result['refresh_token_issued'])
        self.browser('await page.unroute(' + json.dumps(self.origin + '/oauth2/callback?**') + ');')

    def credential_scan(self):
        self.use('owner')
        cookie = '; '.join(c['name'] + '=' + c['value'] for c in self.cookies['owner'])
        result = self.private('''import io, zipfile, subprocess
req=u.Request(identity['proxy_auth_url'],headers={'Cookie':VALUES['cookie']})
with u.urlopen(req,timeout=5) as f: access=f.headers['X-Auth-Request-Access-Token']
needles=[access,identity['client_secret'],identity['boundary_secret']]+[x.split('=',1)[1] for x in VALUES['cookie'].split('; ')]
blobs=[]
for folder in ['runs','broker']:
 for path in (Path(ROOT)/STATE/folder).rglob('*'):
  if path.is_file(): blobs.append(path.read_bytes())
req=u.Request('http://127.0.0.1:18765/api/projects/'+VALUES['project']+'/export',headers={'Cookie':VALUES['cookie'],'Host':p.urlsplit(identity['public_origin']).netloc,'X-Research-Proxy':identity['boundary_secret']})
with u.urlopen(req,timeout=6) as f: payload=f.read()
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
 blobs.extend(archive.read(name) for name in archive.namelist())
for name in VALUES['containers']:
 log=subprocess.run(['docker','logs',name],capture_output=True,timeout=5)
 if log.returncode: raise ValueError('A required owned service log was unavailable.')
 blobs.extend([log.stdout,log.stderr])
print(json.dumps({'clean':not any(needle.encode() in blob for needle in needles for blob in blobs),'scanned_blobs':len(blobs)}))
''', {'cookie': cookie, 'project': self.operator['projects'][0], 'containers': [self.manifest['name'] + '-' + s + '-1' for s in ('app','oauth2-proxy','keycloak','traefik')]})
        self.check('current owner token and cookies plus client/boundary secrets absent from runs broker export and four verified service logs', result['clean'])

    def install_gate(self):
        # Test-only wrapper pauses delivery of a real completed broker receipt.
        # No recipe/image is modified, and the gate self-releases after 45 s.
        script = '''#!/usr/local/bin/python
import json,sys,subprocess,time
from pathlib import Path
args=sys.argv[1:]
state=Path(args[args.index('--state-dir')+1]).parent
if 'cancel' in args:
 (state/('cancel-observed-'+args[args.index('--request-id')+1])).write_text('observed')
 deadline=time.monotonic()+10
 while (state/'hold-cancel').exists() and time.monotonic()<deadline: time.sleep(0.05)
result=subprocess.run(['/opt/outfitter',*args],capture_output=True)
if 'run' in args and (state/'gate-enabled').exists():
 request=json.loads(Path(args[args.index('--request')+1]).read_text())
 marker=state/('gate-'+request['request_id'])
 marker.write_text('ready')
 deadline=time.monotonic()+45
 while marker.exists() and time.monotonic()<deadline: time.sleep(0.05)
sys.stdout.buffer.write(result.stdout);sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
'''
        path = self.root / 'state/gate.private.py'; path.write_text(script); path.chmod(0o700)
        config = json.loads((self.root / 'state/config.json').read_text()); config['broker'] = str(path)
        (self.root / 'state/config.json').write_text(json.dumps(config))
        self.stack('restart', 'app'); self.ready()

    def gate_ready(self, rid):
        marker = self.root / 'state' / ('gate-' + rid)
        for _ in range(150):
            if marker.exists(): return marker
            time.sleep(0.1)
        raise RuntimeError('Real broker receipt did not reach its synchronization gate.')

    def active_compute_revocation(self):
        import threading
        self.login('editor')
        ledger=json.loads((self.root/'state/broker/ledger.json').read_text())
        owner=ledger['owner']
        hold=self.root/'state/hold-cancel'; hold.touch()
        event=subprocess.Popen(['docker','events','--filter','event=start','--filter','label=org.educloud.outfitter.owner='+owner,'--format','{{json .}}'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        observed={}; ready=threading.Event()
        def pause_owned():
            try:
                for line in event.stdout:
                    record=json.loads(line); cid=record.get('Actor',{}).get('ID') or record.get('id')
                    attrs=record.get('Actor',{}).get('Attributes',{}); rid=attrs.get('org.educloud.outfitter.request','')
                    if attrs.get('org.educloud.outfitter.owner')!=owner or len(rid)!=32 or not all(c in '0123456789abcdef' for c in rid): continue
                    request=self.root/'state/runs'/rid/'request.private.json'
                    if not request.is_file() or json.loads(request.read_text())['project_id']!=self.operator['projects'][0]: continue
                    check=subprocess.run(['docker','inspect',cid],capture_output=True,text=True,timeout=5)
                    if check.returncode: continue
                    obj=json.loads(check.stdout)[0]
                    if obj['Config']['Labels'].get('org.educloud.outfitter.owner')!=owner: continue
                    observed.update(cid=cid,rid=rid)
                    paused=subprocess.run(['docker','pause',cid],capture_output=True,timeout=5)
                    if paused.returncode: continue
                    check=json.loads(subprocess.run(['docker','inspect',cid],capture_output=True,text=True,check=True,timeout=5).stdout)[0]
                    observed.update(cid=cid,rid=rid,running=check['State']['Running'],paused=check['State']['Paused'])
                    ready.set(); return
            except Exception:
                ready.set()
        thread=threading.Thread(target=pause_owned,daemon=True); thread.start()
        rid=None
        try:
            payload={'recipe':'summary-r','condition':'baseline','csv':'group,value\n'+''.join('A,'+str(i)+'\n' for i in range(1000))}
            response=self.api('/api/projects/'+self.operator['projects'][0]+'/runs','POST',payload)
            rid=response['body']['id']
            ready.wait(5)
            self.check('exact owned real compute observed running and paused',observed.get('rid')==rid and observed.get('running') and observed.get('paused'))
            self.private("admin('POST','/users/'+VALUES['sub']+'/logout',{})\nprint('{}')",{'sub':self.operator['users']['editor']['sub']},admin=True)
            cancel=self.root/'state'/('cancel-observed-'+rid)
            for _ in range(80):
                if cancel.exists(): break
                time.sleep(0.1)
            obj=json.loads(subprocess.run(['docker','inspect',observed['cid']],capture_output=True,text=True,check=True,timeout=5).stdout)[0]
            self.check('worker requested cancellation while real workload remained running and paused',cancel.exists() and obj['State']['Running'] and obj['State']['Paused'])
            hold.unlink()
            for _ in range(50):
                if subprocess.run(['docker','inspect',observed['cid']],capture_output=True,timeout=3).returncode: break
                time.sleep(0.1)
        finally:
            event.terminate(); event.wait(timeout=5)
            thread.join(timeout=5)
            if event.stdout: event.stdout.close()
            hold.unlink(missing_ok=True)
            if observed.get('cid'):
                subprocess.run(['docker','unpause',observed['cid']],capture_output=True,timeout=5)
        self.use('owner'); row=self.wait_run(self.operator['projects'][0],rid)['body']
        self.check('revocation cancels active compute with retained charge and cleanup',row['status']=='cancelled' and not row['metrics'] and row['cleanup_verified'] and row['compute']['charged_cpu_seconds']==30)
        if observed.get('cid'):
            absent=subprocess.run(['docker','inspect',observed['cid']],capture_output=True,timeout=5).returncode!=0
            self.check('paused owned workload absent after cancellation',absent)
        self.login('editor')

    def extended(self):
        first = self.operator['projects'][0]
        self.token_response_control()
        self.login('owner'); self.credential_scan()
        # Real wrong-audience access token, issued by the same reviewed provider.
        mapper = self.private("client=admin('GET','/clients?clientId=research-pilot')[0]\nmapper=next(x for x in admin('GET','/clients/'+client['id']+'/protocol-mappers/models') if x['name']=='audience')\nprint(json.dumps({'client':client['id'],'mapper':mapper}))", admin=True)
        changed = json.loads(json.dumps(mapper['mapper'])); changed['config']['included.client.audience'] = 'wrong-audience'
        self.private("admin('PUT','/clients/'+VALUES['client']+'/protocol-mappers/models/'+VALUES['mapper']['id'],VALUES['mapper'])\nprint('{}')", {'client':mapper['client'],'mapper':changed}, admin=True)
        try: self.login('outsider', expected=401)
        finally: self.private("admin('PUT','/clients/'+VALUES['client']+'/protocol-mappers/models/'+VALUES['mapper']['id'],VALUES['mapper'])\nprint('{}')", mapper, admin=True)
        # Short real access-token lifetime; the production pilot default stays 10m.
        client = self.private("print(json.dumps(admin('GET','/clients?clientId=research-pilot')[0]))", admin=True)
        attrs = dict(client['attributes']); attrs['access.token.lifespan'] = '30'
        self.private("admin('PUT','/clients/'+VALUES['id'],{'attributes':VALUES['attributes']})\nprint('{}')", {'id':client['id'],'attributes':attrs}, admin=True)
        try:
            self.login('viewer'); time.sleep(32)
            self.check('expired real credential rejected', self.api('/api/session')['status'] == 401)
        finally: self.private("admin('PUT','/clients/'+VALUES['id'],{'attributes':VALUES['attributes']})\nprint('{}')", {'id':client['id'],'attributes':{**client['attributes'],'access.token.lifespan':client['attributes'].get('access.token.lifespan','600')}}, admin=True)
        self.install_gate()
        self.login('owner'); self.login('editor')
        self.active_compute_revocation()
        enabled = self.root / 'state/gate-enabled'; enabled.touch()
        payload = {'recipe':'summary-python','condition':'baseline','csv':'group,value\nA,1\nA,2\n'}
        self.use('owner')
        holder = self.api('/api/projects/'+first+'/runs','POST',payload)['body']['id']; marker = self.gate_ready(holder)
        self.use('editor'); queued = self.api('/api/projects/'+first+'/runs','POST',payload)['body']['id']
        self.use('owner'); self.api('/api/projects/'+first+'/members','POST',{'subject':self.operator['users']['editor']['subject'],'role':'viewer'})
        marker.unlink(); self.wait_run(first,holder)
        row=self.wait_run(first,queued)['body']
        self.check('queued demotion prevents broker admission', row['status']=='cancelled' and row['compute']['charged_cpu_seconds']==0)
        self.api('/api/projects/'+first+'/members','POST',{'subject':self.operator['users']['editor']['subject'],'role':'editor'})
        self.use('editor'); rid=self.api('/api/projects/'+first+'/runs','POST',payload)['body']['id']; marker=self.gate_ready(rid)
        self.private("admin('POST','/users/'+VALUES['sub']+'/logout',{})\nprint('{}')", {'sub':self.operator['users']['editor']['sub']}, admin=True)
        time.sleep(2)
        self.check('revoked real credential immediately denied',self.api('/api/session')['status']==401)
        marker.unlink(); self.use('owner'); row=self.wait_run(first,rid)['body']
        self.check('post-execution revocation suppresses publication and retains charge',row['status']=='cancelled' and not row['metrics'] and row['cleanup_verified'] and row['compute']['charged_cpu_seconds']==30)
        # A real outage during the observation interval remains denied after recovery.
        self.login('viewer'); self.use('owner')
        rid=self.api('/api/projects/'+first+'/runs','POST',payload)['body']['id']; marker=self.gate_ready(rid)
        self.stack('stop','-t','5','keycloak')
        self.check('identity outage returns JSON 503',self.api('/api/projects')['status']==503)
        cancel=self.root/'state'/('cancel-observed-'+rid)
        for _ in range(100):
            if cancel.exists(): break
            time.sleep(0.1)
        self.check('worker observed identity outage before recovery',cancel.exists())
        self.use('viewer')
        self.check('separate-session logout works during identity outage',self.api('/api/session','DELETE')['status']==200)
        self.stack('start','keycloak'); time.sleep(8); marker.unlink(); enabled.unlink()
        self.login('owner'); row=self.wait_run(first,rid)['body']
        self.check('outage denial stays latched after service recovery',row['status']=='cancelled' and not row['metrics'] and row['compute']['charged_cpu_seconds']==30)
        self.restore_control()

    def restore_control(self):
        from research_workspace import recovery
        first=self.operator['projects'][0]
        # Preserve a current removal as well as compute charges through cutover.
        self.api('/api/projects/'+first+'/members/'+self.operator['users']['viewer']['subject'],'DELETE')
        self.login('owner'); old=list(self.cookies['owner'])
        self.stack('stop','-t','5','app')
        state=self.root/'state'; backup=self.root/'checkpoint'; dest=self.root/'replacement'
        broker=self.operator['host_broker']
        report=recovery.checkpoint(state,backup,broker=broker)
        recovery.restore(backup,dest,trusted_sha256=report['checkpoint_sha256'],broker=broker)
        recovery.activate(dest,state,backup,trusted_sha256=report['checkpoint_sha256'])
        c=json.loads((dest/'config.json').read_text()); self.check('restore preserves OIDC authority mode',c['mode']=='local-synthetic-oidc')
        c['broker']='/opt/outfitter'; (dest/'config.json').write_text(json.dumps(c))
        spec=json.loads((self.root/'compose.private.json').read_text()); service=spec['services']['app']
        service['command']=['python','-m','research_workspace','serve','--state',str(dest),'--host','0.0.0.0']
        service['volumes']=[str(dest)+':'+str(dest),'/var/run/docker.sock:/var/run/docker.sock']
        (self.root/'compose.private.json').write_text(json.dumps(spec)); self.stack('up','-d','app'); self.state_name='replacement'; self.ready()
        self.browser('await page.context().clearCookies(); await page.context().addCookies('+json.dumps(old)+');')
        self.check('restore invalidates previous app session',self.api('/api/session')['status']==401)
        self.check('restore rejects old proxy cookie',self.api('/api/session','POST',{})['status']==401)
        self.login('owner')
        self.check('fresh identity can export restored evidence',self.api('/api/projects/'+first+'/export')['status']==200)
        self.browser('await page.context().clearCookies(); await page.context().addCookies('+json.dumps(old)+');')
        self.check('old proxy stays rejected after fresh reauthentication',self.api('/api/session','POST',{})['status']==401)
        self.login('viewer'); self.check('restore preserves member removal',self.api('/api/projects/'+first+'/export')['status']==403)

    def keyboard_control(self):
        self.login('owner')
        self.cli_call('snapshot')
        self.browser('await page.getByRole("button",{name:"Synthetic study 1 Active project"}).press("Enter"); await page.getByRole("tab",{name:"Run workspace"}).press("ArrowRight");')
        self.check('keyboard project selection and detail tab',self.browser('return await page.getByRole("tab",{name:/Project details/}).getAttribute("aria-selected") === "true";'))
        self.cli_call('snapshot')
        self.browser('await page.getByLabel("Operator-provisioned account identifier").fill('+json.dumps(self.operator['users']['viewer']['subject'])+'); await page.getByLabel("Project role").selectOption("viewer"); await page.getByRole("button",{name:"Add or update collaborator"}).press("Enter");')
        self.check('keyboard membership update',any(m['subject']==self.operator['users']['viewer']['subject'] and m['role']=='viewer' for m in self.api('/api/projects/'+self.operator['projects'][0])['body']['members']))
        self.browser('await page.getByRole("tab",{name:/Project details/}).press("ArrowLeft"); await page.getByRole("textbox",{name:"Input data"}).fill("group,value\\nA,invalid\\n"); await page.getByRole("button",{name:"Start run"}).press("Enter");')
        self.check('keyboard invalid input yields visible failure',self.browser('await page.locator("#global-error-text").waitFor({state:"visible"}); return (await page.locator("#global-error-text").innerText()).length > 0;'))
        before={r['id'] for r in self.api('/api/projects/'+self.operator['projects'][0])['body']['runs']}
        self.browser('await page.getByRole("button",{name:"Use example data"}).press("Enter"); await page.getByRole("button",{name:"Start run"}).press("Enter");')
        rows=self.api('/api/projects/'+self.operator['projects'][0])['body']['runs']; rid=next(r['id'] for r in rows if r['id'] not in before)
        self.check('keyboard reviewed run succeeds',self.wait_run(self.operator['projects'][0],rid)['body']['status']=='succeeded')
        self.browser('const download=page.waitForEvent("download"); await page.getByRole("button",{name:"Export evidence"}).press("Enter"); await (await download).saveAs('+json.dumps(str(self.root/'keyboard-evidence.zip'))+');')
        self.check('keyboard export download exists',(self.root/'keyboard-evidence.zip').is_file())
        self.browser('await page.getByRole("button",{name:"Sign out",exact:true}).press("Enter"); await page.getByRole("link",{name:"End identity-provider session"}).waitFor();')
        self.check('keyboard local sign-out explains separate provider session',self.browser('return (await page.locator("#sign-in-error").innerText()).includes("identity-provider session may still be open");'))

    def run(self):
        self.ready()
        self.cli_call('open', self.origin, '--headed')
        self.browser('await page.route("**/api/auth-config",route=>route.fulfill({status:503,contentType:"application/json",body:JSON.stringify({error:"Injected configuration outage"})})); await page.goto(' + json.dumps(self.origin) + '); await page.getByRole("button",{name:"Retry connection"}).waitFor();')
        self.cli_call('snapshot')
        self.check('configuration outage offers retry without access-key fallback',self.browser('return !(await page.locator("#access-key").isVisible());'))
        self.browser('await page.unroute("**/api/auth-config"); await page.getByRole("button",{name:"Retry connection"}).press("Enter"); await page.getByRole("button",{name:"Sign in with identity provider"}).waitFor();')
        for role in ('owner', 'editor', 'viewer', 'outsider'): self.login(role)
        first, second = self.operator['projects']
        for role, visible in [('owner', first), ('editor', first), ('viewer', first), ('outsider', second)]:
            self.use(role)
            index = self.api('/api/projects')
            self.check(role + ' sees only provisioned project', [p['id'] for p in index['body']['projects']] == [visible])
            other = second if visible == first else first
            self.check(role + ' cross-project export denied', self.api('/api/projects/' + other + '/export')['status'] == 403)
        self.use('viewer')
        payload = {'recipe': 'summary-python', 'condition': 'baseline', 'csv': 'group,value\nA,1\nA,2\nB,3\n'}
        self.check('viewer cannot submit', self.api('/api/projects/' + first + '/runs', 'POST', payload)['status'] == 403)
        self.check('viewer cannot manage membership', self.api('/api/projects/' + first + '/members', 'POST', {'subject': self.operator['users']['outsider']['subject'], 'role': 'editor'})['status'] == 403)
        self.use('owner')
        for recipe in ('summary-python', 'summary-r'):
            data = dict(payload, recipe=recipe)
            run = self.api('/api/projects/' + first + '/runs', 'POST', data)
            self.check(recipe + ' admitted', run['status'] == 202)
            result = self.wait_run(first, run['body']['id'])
            self.check(recipe + ' real compute and cleanup', result['body']['status'] == 'succeeded' and result['body']['cleanup_verified'] and result['body']['compute']['charged_cpu_seconds'] == 30)
        self.check('current member export', self.api('/api/projects/' + first + '/export')['status'] == 200)
        self.use('editor')
        self.check('editor initial export', self.api('/api/projects/' + first + '/export')['status'] == 200)
        self.use('owner')
        self.check('owner removes editor', self.api('/api/projects/' + first + '/members/' + self.operator['users']['editor']['subject'], 'DELETE')['status'] == 200)
        self.use('editor')
        self.check('removed member immediate export denial', self.api('/api/projects/' + first + '/export')['status'] == 403)
        self.use('owner')
        self.check('owner restores editor', self.api('/api/projects/' + first + '/members', 'POST', {'subject': self.operator['users']['editor']['subject'], 'role': 'editor'})['status'] == 200)
        self.browser('await page.context().clearCookies();')
        forged = self.api('/api/projects', headers={'X-Research-Proxy': self.config['identity']['boundary_secret'], 'X-Auth-Request-User': self.operator['users']['owner']['subject'], 'Authorization': 'Bearer forged'})
        self.check('forged identity headers cannot authenticate', forged['status'] == 401)
        for path in ['/oauth2/auth', '/oauth2/sign_out', '/oauth2/%61uth', '/idp/admin/', '/idp/realms/master/', '/idp/health']:
            response = self.api(path)
            self.check('private route unavailable ' + path, response['status'] == 404 and 'x-auth-request-access-token' not in response['headers'])
        self.check('access keys cannot authenticate OIDC mode', self.api('/api/session', 'POST', {'access_key': 'forged'})['status'] == 401)
        self.use('owner'); old = self.cookies['owner']
        self.check('workspace logout succeeds', self.api('/api/session', 'DELETE')['body']['identity_provider_session_ended'] is False)
        self.browser('await page.context().addCookies(' + json.dumps(old) + ');')
        self.check('signed-out cookie replay fails', self.api('/api/session')['status'] == 401)
        self.check('signed-out proxy cannot begin replacement session', self.api('/api/session', 'POST', {})['status'] == 401)
        self.extended()
        self.keyboard_control()
        return self.checks


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--root', required=True); parser.add_argument('--cli', default=CLI)
    args = parser.parse_args(); pilot = Pilot(args.root, args.cli)
    status = 'FAIL'
    try:
        pilot.run(); status = 'PASS'
    finally:
        report = {'status': status, 'checks': pilot.checks, 'scope': 'Local synthetic OIDC, real browser and private service checks.',
                  'not_run': ['manual screen reader', 'independent human', 'hosted capacity', 'private-data admission']}
        (pilot.root / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
        try: pilot.cli_call('close')
        except Exception: pass
