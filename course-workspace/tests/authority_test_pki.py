"""Disposable test-only certificates; no credentials printed or retained."""
import hashlib
from pathlib import Path
import ssl
import subprocess
import uuid


class TestPKI:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(mode=0o700)
        self.ca_cert=self.root/'ca.pem';self.ca_key=self.root/'ca.key'
        self.run('req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-days','2',
            '-subj','/CN=Disposable authority unit CA','-addext','basicConstraints=critical,CA:TRUE',
            '-addext','keyUsage=critical,keyCertSign,cRLSign','-keyout',self.ca_key,'-out',self.ca_cert)
        self.server_cert,self.server_key=self.issue('server','server',hostname='localhost')

    @staticmethod
    def run(*arguments):
        result=subprocess.run(['openssl',*[str(value) for value in arguments]],stdout=subprocess.PIPE,stderr=subprocess.PIPE,umask=0o077,timeout=10)
        if result.returncode:raise RuntimeError('disposable test certificate generation failed')

    def issue(self,name,purpose='client',hostname=None):
        key=self.root/(name+'.key');csr=self.root/(name+'.csr');certificate=self.root/(name+'.pem');extensions=self.root/(name+'.extensions')
        self.run('req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-subj','/CN='+name,'-keyout',key,'-out',csr)
        value='basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage='+('clientAuth' if purpose=='client' else 'serverAuth')+'\n'
        if hostname:value+='subjectAltName=DNS:'+hostname+',IP:127.0.0.1\n'
        extensions.write_text(value);extensions.chmod(0o600)
        self.run('x509','-req','-days','2','-in',csr,'-CA',self.ca_cert,'-CAkey',self.ca_key,'-set_serial','0x'+uuid.uuid4().hex,'-extfile',extensions,'-out',certificate)
        key.chmod(0o600);certificate.chmod(0o600)
        return certificate,key

    @staticmethod
    def fingerprint(certificate):return hashlib.sha256(ssl.PEM_cert_to_DER_cert(Path(certificate).read_text())).hexdigest()
