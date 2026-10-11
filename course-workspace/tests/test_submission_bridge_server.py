"""Optional real Jupyter/Chromium integration; course rehearsal also exercises Docker image.
Run in a venv with the pinned JupyterLab course version and Playwright installed.
"""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ('jupyterlab', 'jupyterhub', 'playwright'))


@unittest.skipUnless(AVAILABLE, 'requires course JupyterLab/JupyterHub and Playwright integration dependencies')
class JupyterBridgeTests(unittest.TestCase):
    def test_authenticated_lab_navigation_xsrf_and_origin_bound_saved_file_handoff(self):
        from playwright.sync_api import sync_playwright
        import requests
        class Receiver(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
                self.wfile.write(b'''<!doctype html><html><body><button id="receive">Receive saved files</button><script>
const setup=JSON.parse(decodeURIComponent(location.hash.split('=')[1]));
window.received=null;
document.getElementById('receive').onclick=()=>opener.postMessage({type:'cairn-ready',version:1,nonce:setup.nonce},setup.origin);
window.addEventListener('message',event=>{if(event.origin===setup.origin && event.source===opener && event.data.nonce===setup.nonce){window.received=event.data;opener.postMessage({type:'cairn-received',version:1,nonce:setup.nonce},setup.origin)}});
</script></body></html>''')
            def log_message(self, *_args): pass
        receiver = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        receiver.daemon_threads = True
        threading.Thread(target=receiver.serve_forever, daemon=True).start()
        self.addCleanup(receiver.server_close); self.addCleanup(receiver.shutdown)
        cairn = f'http://127.0.0.1:{receiver.server_port}'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); work = root / 'work'; work.mkdir()
            extension_root = root / 'labextensions'
            (extension_root / '@educloud').mkdir(parents=True)
            (extension_root / '@educloud/waypoint-cairn').symlink_to(ROOT / 'course/labextension', target_is_directory=True)
            saved = b'print("saved synthetic work")\n'
            (work / 'solution.py').write_bytes(saved)
            token = secrets.token_hex(32)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            origin = f'http://127.0.0.1:{port}'
            cfg = root / 'jupyter_config.py'
            cfg.write_text("\n".join([
                # Reproduce the pinned Hub's cookie-fetch XSRF predicate while
                # retaining a standalone local login fixture. Hub's full OAuth
                # lifecycle remains covered by the Docker course rehearsal.
                'from jupyterhub._xsrf_utils import _needs_check_xsrf',
                'from jupyter_server.base.handlers import APIHandler',
                'from tornado.web import RequestHandler',
                '_original_prepare = APIHandler.prepare',
                'async def _hub_fetch_prepare(self):',
                '    await _original_prepare(self)',
                '    if self.request.path.startswith("/cairn/") and _needs_check_xsrf(self):',
                '        RequestHandler.check_xsrf_cookie(self)',
                'APIHandler.prepare = _hub_fetch_prepare',
                'c = get_config()', "c.ServerApp.ip = '127.0.0.1'", f'c.ServerApp.port = {port}',
                'c.ServerApp.port_retries = 0', 'c.ServerApp.open_browser = False',
                f'c.ServerApp.root_dir = {str(work)!r}', f'c.IdentityProvider.token = {token!r}',
                "c.ServerApp.jpserver_extensions = {'waypoint_cairn': True}",
                f'c.LabApp.extra_labextensions_path = {[str(extension_root)]!r}',
            ]))
            cfg.chmod(0o600)
            env = dict(os.environ, PYTHONPATH=str(ROOT / 'course'), WORKSPACE_CAIRN_ORIGIN=cairn, WORKSPACE_CAIRN_LOCAL_TEST='1', JUPYTER_RUNTIME_DIR=str(root / 'runtime'))
            with (root / 'server.log').open('w') as log:
                process = subprocess.Popen([sys.executable, '-m', 'jupyterlab', '--config', str(cfg)], env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    for _ in range(150):
                        if process.poll() is not None: self.fail('Jupyter fixture exited; inspect private fixture log while debugging')
                        try:
                            if requests.get(origin + '/api', timeout=.2).status_code in (200, 403): break
                        except requests.RequestException: pass
                        time.sleep(.1)
                    else: self.fail('Jupyter fixture did not become ready')
                    response = requests.get(origin + '/cairn/config', allow_redirects=False, timeout=2)
                    self.assertIn(response.status_code, (302, 403))
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch()
                        try:
                            page = browser.new_page()
                            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                            console_errors = []; page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
                            page.goto(origin + '/lab')
                            page.locator('#password_input').fill(token)
                            page.get_by_role('button', name='Log in', exact=True).click()
                            try:
                                page.get_by_role('link', name='Submit saved files to Cairn').wait_for(timeout=15000)
                            except Exception:
                                self.fail('Launcher unavailable: ' + json.dumps({'errors': errors, 'console': console_errors}).replace(token, '[redacted]'))
                            with page.expect_popup() as bridge_opened:
                                page.get_by_role('link', name='Submit saved files to Cairn').click()
                            bridge = bridge_opened.value
                            try:
                                bridge.get_by_role('checkbox', name='solution.py').check(timeout=5000)
                            except Exception:
                                self.fail('Bridge unavailable: ' + bridge.locator('body').inner_text() + '\n' + (root / 'server.log').read_text()[-3000:].replace(token, '[redacted]'))
                            # Existing Jupyter cookie auth must not bypass XSRF on POST.
                            missing = bridge.evaluate("""async () => (await fetch('files',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:['solution.py']})})).status""")
                            self.assertEqual(missing, 403)
                            # This is the real Hub predicate absent from vanilla
                            # Jupyter: GET fetches also require an XSRF header.
                            for route in ('config', 'list'):
                                missing_get = bridge.evaluate('async path => (await fetch(path)).status', route)
                                self.assertEqual(missing_get, 403)
                            # Cancel while preparation is in flight: the late response must
                            # not restore a snapshot or re-enable sending cancelled files.
                            pending = []
                            bridge.route(origin + '/cairn/files', lambda route: pending.append(route))
                            bridge.get_by_role('button', name='Prepare saved files').click()
                            for _ in range(100):
                                if pending: break
                                bridge.wait_for_timeout(10)
                            self.assertEqual(len(pending), 1)
                            bridge.get_by_role('button', name='Clear selection').click()
                            pending[0].fulfill(status=200, json={'files': [{'path': 'solution.py', 'size': len(saved), 'sha256': hashlib.sha256(saved).hexdigest(), 'content_base64': base64.b64encode(saved).decode()}]})
                            bridge.wait_for_timeout(150)
                            self.assertTrue(bridge.locator('#review').is_hidden())
                            self.assertTrue(bridge.get_by_role('button', name='Prepare saved files').is_disabled())
                            self.assertEqual(bridge.locator('#selected li').count(), 0)
                            bridge.unroute(origin + '/cairn/files')
                            bridge.get_by_role('checkbox', name='solution.py').check()
                            bridge.get_by_role('button', name='Prepare saved files').click()
                            bridge.get_by_role('button', name='Continue in Cairn').wait_for()
                            (work / 'solution.py').write_bytes(b'print("later save; not prepared")\n')
                            with bridge.expect_popup() as opened:
                                bridge.get_by_role('button', name='Continue in Cairn').click()
                            popup = opened.value; popup.wait_for_load_state()
                            # Same window/origin but wrong nonce cannot trigger bytes.
                            popup.evaluate("opener.postMessage({type:'cairn-ready',version:1,nonce:'0'.repeat(64)},setup.origin)")
                            bridge.wait_for_timeout(150)
                            self.assertIsNone(popup.evaluate('window.received'))
                            # A second real popup at the correct origin has the wrong window identity.
                            with bridge.expect_popup() as other_opened:
                                bridge.evaluate('url => window.open(url, "_blank")', popup.url)
                            other = other_opened.value
                            other.get_by_role('button', name='Receive saved files').click()
                            bridge.wait_for_timeout(150)
                            self.assertIsNone(other.evaluate('window.received'))
                            self.assertIsNone(popup.evaluate('window.received'))
                            other.close()
                            # The expected popup window at a different origin is also rejected.
                            original_url = popup.url
                            popup.goto(original_url.replace('127.0.0.1', 'localhost'))
                            popup.get_by_role('button', name='Receive saved files').click()
                            bridge.wait_for_timeout(150)
                            self.assertIsNone(popup.evaluate('window.received'))
                            popup.close()
                            bridge.get_by_text('Cairn was closed or the handoff expired.', exact=False).wait_for()
                            with bridge.expect_popup() as retry_opened:
                                bridge.get_by_role('button', name='Continue in Cairn').click()
                            popup = retry_opened.value
                            self.assertNotEqual(popup.url, original_url)
                            popup.get_by_role('button', name='Receive saved files').click()
                            popup.wait_for_function('window.received !== null')
                            data = popup.evaluate('window.received')
                            self.assertEqual(set(data), {'type','version','nonce','files'})
                            self.assertEqual(base64.b64decode(data['files'][0]['content_base64']), saved)
                            self.assertEqual(data['files'][0]['path'], 'solution.py')
                            bridge.get_by_text('Cairn received the saved files for review.', exact=False).wait_for()
                            output = ROOT.parent / 'output/playwright'; output.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(output / 'jupyter-cairn-launcher.png'))
                            bridge.screenshot(path=str(output / 'jupyter-cairn-saved-files.png'), full_page=True)
                            self.assertFalse(errors, 'JupyterLab plugin must load without browser exceptions')
                        finally:
                            browser.close()
                finally:
                    process.terminate()
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


if __name__ == '__main__': unittest.main()
