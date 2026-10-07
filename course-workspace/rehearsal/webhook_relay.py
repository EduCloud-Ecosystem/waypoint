"""Private fixture relay: record actual Forgejo deliveries without exposing Cairn publicly."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading

import requests


class WebhookRelay:
    def __init__(self, target, gateway):
        self.deliveries=[]
        self.lock=threading.Lock()
        relay=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                if self.path!='/webhooks/forgejo':
                    self.send_error(404); return
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=1024*1024:
                    self.send_error(413); return
                body=self.rfile.read(length)
                headers={k:v for k,v in self.headers.items() if k.lower().startswith('x-')}
                headers['Content-Type']='application/json'
                try:
                    response=requests.post(target+self.path,data=body,headers=headers,timeout=15)
                    status=response.status_code
                except requests.RequestException:
                    status=502
                with relay.lock:
                    relay.deliveries.append((body,headers,status))
                self.send_response(status); self.end_headers()
        host=gateway if sys.platform=='linux' else '127.0.0.1'
        self.server=ThreadingHTTPServer((host,0),Handler)
        self.origin='http://'+(gateway if sys.platform=='linux' else 'host.docker.internal')+':'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def for_revision(self, revision):
        with self.lock:
            return [d for d in self.deliveries if json.loads(d[0]).get('after')==revision]

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
