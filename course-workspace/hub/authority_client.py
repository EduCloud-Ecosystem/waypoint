"""Bounded synchronous mTLS client; each call is fresh, no reusable grants."""
import http.client
import json
from pathlib import Path
import re
import ssl
import stat
import time
from urllib.parse import urlsplit
import uuid

VERSION='educloud-authority-access-v1'
PATH='/v1/authority'
MAX_FRAME=64*1024
HEX32=re.compile(r'[0-9a-f]{32}\Z')
SLUG=re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
OPERATIONS=frozenset({'ownership.preview','ownership.apply','ownership.reconcile_preview','ownership.reconcile_apply','ownership.inventory',
                      'hub.authorize','hub.spawn','hub.poll','hub.stop','guardian.observe'})
ERRORS=frozenset({'authority_access_refused','authority_frame_invalid','authority_replay_refused','authority_identity_or_version_mismatch',
                 'authority_operation_refused','authority_replay_state_unavailable','authority_response_invalid','authority_transport_unavailable'})


class AuthorityError(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise AuthorityError('authority_frame_invalid')
        result[key]=value
    return result


def parsed(raw):
    return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(AuthorityError('authority_frame_invalid')))


def material(path,secret=False):
    path=Path(path)
    if not path.is_absolute() or path.resolve()!=path:raise AuthorityError('authority_access_refused')
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 1<=info.st_size<=256*1024 or secret and info.st_mode&0o077:
        raise AuthorityError('authority_access_refused')
    return str(path)


class AuthorityClient:
    def __init__(self,url,authority_id,ca_file,certfile,keyfile,worker_id,course_id,timeout=3):
        try:
            if not isinstance(url,str) or len(url)>1024 or any(ord(c)<33 or ord(c)>126 for c in url):raise AuthorityError('authority_access_refused')
            parts=urlsplit(url)
            if (parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
                    or parts.path not in ('',PATH) or not isinstance(authority_id,str) or not HEX32.fullmatch(authority_id)
                    or not isinstance(worker_id,str) or not SLUG.fullmatch(worker_id) or not isinstance(course_id,str) or not SLUG.fullmatch(course_id)
                    or type(timeout) not in (int,float) or not 0<timeout<=10):raise AuthorityError('authority_access_refused')
            self.host=parts.hostname;self.port=parts.port or 443;self.authority_id=authority_id;self.worker_id=worker_id;self.course_id=course_id;self.timeout=timeout
            self.context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.context.load_verify_locations(cafile=material(ca_file))
            self.context.minimum_version=ssl.TLSVersion.TLSv1_2
            self.context.check_hostname=True;self.context.verify_mode=ssl.CERT_REQUIRED
            self.context.load_cert_chain(material(certfile),material(keyfile,True))
        except AuthorityError:raise
        except Exception:raise AuthorityError('authority_access_refused') from None

    def call(self,operation,payload):
        connection=None
        try:
            if not isinstance(operation,str) or operation not in OPERATIONS or not isinstance(payload,dict):raise AuthorityError('authority_frame_invalid')
            frame={'version':VERSION,'authority_id':self.authority_id,'nonce':uuid.uuid4().hex,'issued_at':int(time.time()),
                   'operation':operation,'worker_id':self.worker_id,'course_id':self.course_id,'payload':payload}
            raw=encoded(frame)
            if len(raw)>MAX_FRAME:raise AuthorityError('authority_frame_invalid')
            connection=http.client.HTTPSConnection(self.host,self.port,context=self.context,timeout=self.timeout)
            connection.request('POST',PATH,body=raw,headers={'Content-Type':'application/json','Content-Length':str(len(raw)),'Connection':'close'})
            response=connection.getresponse();lengths=response.headers.get_all('Content-Length') or []
            if (response.status not in (200,400,403,409,503) or response.getheader('Content-Type')!='application/json'
                    or response.getheader('Transfer-Encoding') is not None or response.getheader('Content-Encoding') is not None or len(lengths)!=1 or not re.fullmatch(r'[0-9]{1,6}',lengths[0])
                    or not 0<int(lengths[0])<=MAX_FRAME):raise AuthorityError('authority_response_invalid')
            raw=response.read(MAX_FRAME+1)
            if len(raw)!=int(lengths[0]):raise AuthorityError('authority_response_invalid')
            value=parsed(raw);expected={'version','authority_id','nonce','operation','worker_id','course_id','ok','result','error'}
            if (not isinstance(value,dict) or set(value)!=expected or any(value[key]!=frame[key] for key in ('version','authority_id','nonce','operation','worker_id','course_id'))
                    or type(value['ok']) is not bool):raise AuthorityError('authority_response_invalid')
            if value['ok']:
                if response.status!=200 or value['error'] is not None or not isinstance(value['result'],dict):raise AuthorityError('authority_response_invalid')
                return value['result']
            if response.status==200 or value['result'] is not None or value['error'] not in ERRORS:raise AuthorityError('authority_response_invalid')
            raise AuthorityError(value['error'])
        except AuthorityError:raise
        except Exception:raise AuthorityError('authority_transport_unavailable') from None
        finally:
            if connection is not None:connection.close()
