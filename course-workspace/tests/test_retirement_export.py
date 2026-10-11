"""Portable coursework never publishes runtime/auth state or unverifiable archives."""
from contextlib import nullcontext
from copy import deepcopy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
import retirement_export as e
import retirement as r
from maintenance import digest, write
from public_course import render


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='export-test-'); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
    def archive(self, members, name='input.tar.gz', trailing=b''):
        path=self.root/name
        with tarfile.open(path,'w:gz',format=tarfile.PAX_FORMAT) as archive:
            for member, content in members:
                archive.addfile(member,io.BytesIO(content) if content is not None else None)
        if trailing:
            with gzip.open(path,'ab') as stream: stream.write(trailing)
        path.chmod(0o600); return path
    def directory(self,name='work'):
        info=tarfile.TarInfo(name); info.type=tarfile.DIRTYPE; return info,None
    def file(self,name='work/answer.py',content=b'answer=5\n'):
        info=tarfile.TarInfo(name); info.size=len(content); return info,content
    def test_source_inventory_hashes_hidden_nested_files_and_counts_outside_work(self):
        home=self.root/'home'; (home/'work/.project').mkdir(parents=True)
        (home/'work/.project/solution.py').write_bytes(b'answer=5\n')
        (home/'.local/share/jupyter/runtime').mkdir(parents=True)
        (home/'.local/share/jupyter/runtime/secret.json').write_text('synthetic runtime token')
        value=e.inventory(home,{'entries':0,'bytes':0})
        self.assertTrue(value['work_present']); self.assertEqual(value['outside_work_entries'],5)
        self.assertEqual(value['entries'][-1]['sha256'],hashlib.sha256(b'answer=5\n').hexdigest())
        self.assertNotIn('secret',json.dumps(value))
        empty=self.root/'empty'; empty.mkdir()
        self.assertEqual(e.inventory(empty,{'entries':0,'bytes':0}),{'work_present':False,'outside_work_entries':0,'entries':[]})
    def test_source_work_links_and_fifo_refuse_without_blocking(self):
        for kind in ('root-link','file-link','fifo'):
            home=self.root/kind; home.mkdir()
            if kind=='root-link': (home/'work').symlink_to(self.root,target_is_directory=True)
            else:
                (home/'work').mkdir()
                if kind=='file-link': (home/'work/secret').symlink_to('/etc/passwd')
                else: os.mkfifo(home/'work/pipe')
            with self.subTest(kind=kind), self.assertRaises(e.ExportError): e.inventory(home,{'entries':0,'bytes':0})
    def test_archive_source_hash_pax_path_and_nested_work_match(self):
        home=self.root/'home'; (home/'work').mkdir(parents=True)
        name='x'*150+'.py'; (home/'work'/name).write_bytes(b'longpath')
        archive=self.root/'work.tar.gz'
        with tarfile.open(archive,'w:gz',format=tarfile.PAX_FORMAT) as out: out.add(home/'work',arcname='work')
        archive.chmod(0o600)
        self.assertEqual(e.archive_inventory(archive)['entries'],e.inventory(home,{'entries':0,'bytes':0})['entries'])
    def test_archive_traversal_links_special_duplicates_and_parent_conflicts_refuse(self):
        cases=[]
        for name in ('../secret','/work/a','work/../a','work//a','work/./a','.env','work\\a'):
            cases.append([self.directory(),self.file(name)])
        for type in (tarfile.SYMTYPE,tarfile.LNKTYPE,tarfile.FIFOTYPE,tarfile.CHRTYPE):
            info=tarfile.TarInfo('work/link'); info.type=type; info.linkname='/etc/passwd'; cases.append([self.directory(),(info,None)])
        cases += [[self.directory(),self.file(),self.file()], [self.file('work')], [self.file('work/a')],
                  [self.directory(),self.file('work/a'),self.file('work/a/b')]]
        for index,members in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(e.ExportError): e.archive_inventory(self.archive(members,name=f'bad-{index}.tar.gz'))
    def test_giant_pax_gnu_sparse_and_trailing_payload_refuse(self):
        header=tarfile.TarInfo('pax'); header.type=tarfile.XHDTYPE; header.size=2*1024**2
        path=self.root/'giant.tar.gz'
        with gzip.open(path,'wb') as out: out.write(header.tobuf()); out.write(b'0'*header.size); out.write(b'\0'*1024)
        path.chmod(0o600)
        with self.assertRaisesRegex(e.ExportError,'metadata_budget'): e.archive_inventory(path)
        for kind in (tarfile.GNUTYPE_LONGNAME,tarfile.GNUTYPE_LONGLINK,tarfile.GNUTYPE_SPARSE):
            info=tarfile.TarInfo('work/sparse'); info.type=kind
            with self.assertRaises(e.ExportError): e.archive_inventory(self.archive([(info,None)],name='gnu-'+kind.decode()+'.tar.gz'))
        with self.assertRaisesRegex(e.ExportError,'trailing_archive_payload'):
            e.archive_inventory(self.archive([self.directory(),self.file()],name='trailing.tar.gz',trailing=b'hidden data'))
    def test_archive_budget_private_permissions_corruption_and_missing_refuse(self):
        good=self.archive([self.directory(),self.file()])
        with patch.object(e,'MAX_FILE_BYTES',1), self.assertRaises(e.ExportError): e.archive_inventory(good)
        good.chmod(0o644)
        with self.assertRaises(e.ExportError): e.archive_inventory(good)
        good.chmod(0o600); good.write_bytes(good.read_bytes()[:-5])
        with self.assertRaises((e.ExportError,EOFError)): e.archive_inventory(good)
        with self.assertRaises(OSError): e.archive_inventory(self.root/'missing')
    def fixture(self):
        deployment=self.root/'deployment'; deployment.mkdir(mode=0o700)
        homes=self.root/'homes'; homes.mkdir()
        names=['course-home-'+'a'*24,'course-home-'+'b'*24]
        for index,name in enumerate(names):
            (homes/name/'work').mkdir(parents=True)
            (homes/name/'work/answer.py').write_text('answer='+str(index)+'\n')
            (homes/name/'.jupyter').mkdir(); (homes/name/'.jupyter/secret').write_text('synthetic runtime token')
        _,_,env=render('work.example.test','https://auth.example.test/realms/course','course','course',['current-bob'],str(homes))
        env['WORKSPACE_ENV_FILE']=str(deployment/'.env'); env['WORKSPACE_OIDC_CLIENT_SECRET']='synthetic-secret'
        write(deployment/'.env',''.join(k+'='+v+'\n' for k,v in env.items()).encode())
        ledger={'version':r.VERSION,'status':'RETIRED','source':'snapshot_restore','snapshot_id':'a'*64,'retention':'pending'}
        write(deployment/r.MARKER,ledger)
        state={'names':names,'course_image':'sha256:'+'a'*64,'policy_sha256':'b'*64,'source_manifest_sha256':'c'*64,'stamp':{'stable':True}}
        def pack(image,name,scratch):
            with tarfile.open(scratch/'data.tar.gz','w:gz',format=tarfile.PAX_FORMAT) as out: out.add(homes/name/'work',arcname='work')
            (scratch/'data.tar.gz').chmod(0o600)
        return deployment,homes,names,state,pack
    def test_export_all_retained_homes_only_work_independent_verification_and_no_overwrite(self):
        deployment,homes,names,state,pack=self.fixture(); destination=self.root/'portable'
        with patch.object(e,'locked',return_value=nullcontext()), patch.object(e,'home_lock',return_value=nullcontext()), patch.object(e,'retained_state',return_value=state), patch.object(e,'pack_work',side_effect=pack):
            result=e.export_course(deployment,destination)
        self.assertEqual(result['retained_homes'],2); self.assertEqual(result['work_files'],2)
        self.assertEqual(result['scope'],'learner-work-directories'); self.assertEqual(result['outside_work_entries'],4)
        self.assertEqual(e.verify_export(destination)['status'],'VERIFIED')
        self.assertEqual(destination.stat().st_mode&0o777,0o700)
        self.assertTrue(all(path.stat().st_mode&0o777==0o600 for path in destination.iterdir()))
        manifest=(destination/'manifest.json').read_text()
        for value in ('synthetic-secret','auth.example','current-bob','.jupyter','secret'): self.assertNotIn(value,manifest)
        self.assertTrue((homes/names[0]/'.jupyter/secret').exists())
        with self.assertRaises(e.ExportError): e.export_course(deployment,destination)
        archive=destination/('home-'+'a'*24+'.tar.gz'); archive.write_bytes(b'corrupted')
        with self.assertRaises(e.ExportError): e.verify_export(destination)
        archive.unlink()
        with self.assertRaises(e.ExportError): e.verify_export(destination)
    def test_archive_source_mismatch_and_state_drift_leave_no_destination(self):
        deployment,homes,names,state,pack=self.fixture()
        for issue in ('bytes','state'):
            dest=self.root/issue
            def mismatch(image,name,scratch):
                pack(image,name,scratch)
                if issue=='bytes':
                    with tarfile.open(scratch/'data.tar.gz','w:gz') as out:
                        directory=tarfile.TarInfo('work'); directory.type=tarfile.DIRTYPE; out.addfile(directory)
                        file=tarfile.TarInfo('work/answer.py'); file.size=3; out.addfile(file,io.BytesIO(b'bad'))
                    (scratch/'data.tar.gz').chmod(0o600)
            after=deepcopy(state); after['stamp']['stable']=False
            with patch.object(e,'locked',return_value=nullcontext()), patch.object(e,'home_lock',return_value=nullcontext()), patch.object(e,'retained_state',side_effect=[state,after] if issue=='state' else [state]), patch.object(e,'pack_work',side_effect=mismatch):
                with self.assertRaises(e.ExportError): e.export_course(deployment,dest)
            self.assertFalse(dest.exists()); self.assertFalse(list(self.root.glob('.coursework-export-*')))
    def test_concurrent_empty_destination_is_preserved_and_pending_or_hub_overlap_refuse(self):
        deployment,homes,names,state,pack=self.fixture(); dest=self.root/'raced'
        def race(staging,destination): destination.mkdir(mode=0o700); return original(staging,destination)
        original=e._publish_directory
        with patch.object(e,'locked',return_value=nullcontext()), patch.object(e,'home_lock',return_value=nullcontext()), patch.object(e,'retained_state',return_value=state), patch.object(e,'pack_work',side_effect=pack), patch.object(e,'_publish_directory',side_effect=race):
            with self.assertRaises(e.ExportError): e.export_course(deployment,dest)
        self.assertEqual(list(dest.iterdir()),[])
        write(deployment/r.PENDING,{})
        with patch.object(e,'locked',return_value=nullcontext()), self.assertRaises(e.ExportError): e.export_course(deployment,self.root/'pending')
        (deployment/r.PENDING).unlink()
        (self.root/'hub-data').mkdir(mode=0o700)
        before_hub=(self.root/'hub-data').stat().st_ctime_ns
        with patch.object(e,'locked',return_value=nullcontext()), patch.object(e,'home_lock',return_value=nullcontext()), patch.object(r,'read_retirement',return_value={'source':'operator_retirement','images':{}}), patch.object(e,'retained_state',return_value=state), patch.object(e.upgrade,'hub_data',return_value=self.root/'hub-data'), patch.object(e,'pack_work') as pack:
            with self.assertRaisesRegex(e.ExportError,'overlaps_hub_data'): e.export_course(deployment,self.root/'hub-data/export')
            self.assertEqual((self.root/'hub-data').stat().st_ctime_ns,before_hub)
            pack.assert_not_called()


if __name__=='__main__': unittest.main()
