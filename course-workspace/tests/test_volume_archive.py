"""Archive metadata preservation must retain data-filter containment checks."""
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import volume_archive as archive


class VolumeArchiveTests(unittest.TestCase):
    def write_archive(self, path, members):
        with tarfile.open(path, 'w:gz') as output:
            for member, data in members:
                output.addfile(member, io.BytesIO(data) if data is not None else None)

    def file(self, name, mode=0o660, data=b'synthetic saved bytes'):
        member = tarfile.TarInfo(name); member.mode = mode; member.size = len(data)
        member.uid = 12345; member.gid = 23456
        return member, data

    def directory(self, name, mode):
        member = tarfile.TarInfo(name); member.type = tarfile.DIRTYPE; member.mode = mode
        return member, None

    def link(self, name, target, hard=False):
        member = tarfile.TarInfo(name); member.type = tarfile.LNKTYPE if hard else tarfile.SYMTYPE
        member.mode = 0o777; member.linkname = target
        return member, None

    def test_actual_pack_unpack_preserves_permissions_bytes_and_literal_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); original = root/'source'; original.mkdir(mode=0o700)
            (original/'private').mkdir(mode=0o700)
            saved=original/'private/saved'; saved.write_bytes(b'actual synthetic archive bytes'); saved.chmod(0o660)
            (original/'literal').symlink_to('private/../private/saved')
            os.link(saved,original/'hard')
            archive_path=root/'actual.tar.gz'; target=root/'restored'; target.mkdir(mode=0o700)
            archive.pack(original,archive_path,os.geteuid(),os.getegid())
            self.assertEqual(archive_path.stat().st_mode & 0o7777,0o600)
            archive.unpack(target,archive_path,os.geteuid(),os.getegid())
            self.assertEqual(target.stat().st_mode & 0o7777,0o700)
            self.assertEqual((target/'private').stat().st_mode & 0o7777,0o700)
            self.assertEqual((target/'private/saved').read_bytes(),saved.read_bytes())
            self.assertEqual((target/'private/saved').stat().st_mode & 0o7777,0o660)
            self.assertEqual((target/'hard').stat().st_mode & 0o7777,0o660)
            self.assertEqual((target/'hard').stat().st_ino,(target/'private/saved').stat().st_ino)
            self.assertEqual(os.readlink(target/'literal'),'private/../private/saved')

    def test_constructed_group_writable_hardlink_keeps_target_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); destination=root/'home'; destination.mkdir()
            source=root/'archive.tar.gz'; link,_=self.link('hard','saved',True); link.mode=0o660
            self.write_archive(source,[self.file('saved',0o660),(link,None)])
            archive.unpack(destination,source,os.geteuid(),os.getegid())
            self.assertEqual((destination/'saved').stat().st_mode & 0o7777,0o660)
            self.assertEqual((destination/'hard').stat().st_mode & 0o7777,0o660)
            self.assertEqual((destination/'saved').stat().st_ino,(destination/'hard').stat().st_ino)

    def test_private_readonly_group_writable_and_executable_modes_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); destination = root / 'home'; destination.mkdir(mode=0o700)
            source = root / 'archive.tar.gz'
            members = [self.directory('.', 0o700), self.directory('private', 0o700),
                       self.directory('readonly', 0o550), self.directory('shared', 0o775)]
            for mode in (0o400, 0o600, 0o640, 0o660, 0o664, 0o666, 0o751):
                members.append(self.file('private/file-'+oct(mode), mode))
            self.write_archive(source, members)
            archive.unpack(destination, source, os.geteuid(), os.getegid())
            self.assertEqual(destination.stat().st_mode & 0o7777, 0o700)
            for name, mode in (('private',0o700),('readonly',0o550),('shared',0o775)):
                self.assertEqual((destination / name).stat().st_mode & 0o7777, mode)
            for mode in (0o400, 0o600, 0o640, 0o660, 0o664, 0o666, 0o751):
                path = destination / 'private' / ('file-'+oct(mode))
                self.assertEqual(path.stat().st_mode & 0o7777, mode)
                self.assertEqual(path.read_bytes(), b'synthetic saved bytes')
                self.assertEqual((path.stat().st_uid,path.stat().st_gid),(os.geteuid(),os.getegid()))

    def test_safe_literal_symlink_and_hardlink_remain_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); destination = root / 'home'; destination.mkdir()
            source = root / 'archive.tar.gz'
            self.write_archive(source, [self.directory('private',0o700),self.file('private/saved'),
                self.link('literal','private/../private/saved'),self.link('hard','private/saved',True)])
            archive.unpack(destination, source, os.geteuid(), os.getegid())
            self.assertEqual(os.readlink(destination / 'literal'),'private/../private/saved')
            self.assertEqual((destination / 'literal').read_bytes(),b'synthetic saved bytes')
            self.assertEqual((destination / 'hard').stat().st_ino,(destination / 'private/saved').stat().st_ino)

    def test_paths_links_and_special_files_still_refuse_without_outside_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); outside = root / 'outside'; outside.write_bytes(b'protected')
            special = tarfile.TarInfo('fifo'); special.type = tarfile.FIFOTYPE
            cases = [self.file('../outside'),self.link('escape',str(outside)),
                     self.link('escape','../outside'),self.link('escape','../outside',True),(special,None)]
            for index, item in enumerate(cases):
                destination = root / ('home-'+str(index)); destination.mkdir()
                source = root / ('archive-'+str(index)+'.tar.gz'); self.write_archive(source,[item])
                with self.assertRaises((tarfile.TarError,ValueError)):
                    archive.unpack(destination,source,os.geteuid(),os.getegid())
                self.assertEqual(outside.read_bytes(),b'protected')
                self.assertFalse((destination/'escape').exists())

    def test_original_link_containment_is_checked_before_preserving_literal_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); destination = root / 'home'; destination.mkdir()
            # A normalized target appears internal; its original interpretation
            # through an existing symlink leads outside the volume.
            (destination / 'redirect').symlink_to(root / 'elsewhere')
            member,_ = self.link('literal','redirect/../outside')
            with self.assertRaises((tarfile.TarError,ValueError)):
                archive.preserved_data_filter(member,str(destination))

    def test_special_permission_bits_refuse_at_pack_and_unpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); destination = root / 'home'; destination.mkdir()
            source = root / 'archive.tar.gz'
            for mode in (0o4751,0o2755,0o1700):
                member, data = self.file('unsafe',mode)
                self.write_archive(source,[(member,data)])
                with self.assertRaisesRegex(ValueError,'special permission'):
                    archive.unpack(destination,source,os.geteuid(),os.getegid())
            original = root / 'original'; original.mkdir(); (original/'unsafe').write_bytes(b'data')
            (original/'unsafe').chmod(0o4755)
            with self.assertRaisesRegex(ValueError,'special permission'):
                archive.pack(original,source,os.geteuid(),os.getegid())

    def test_existing_or_symlink_destination_refuses_before_ownership_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); destination = root / 'home'; destination.mkdir()
            (destination/'retained').write_bytes(b'do not overwrite')
            link = root/'link'; link.symlink_to(destination,target_is_directory=True)
            with patch('volume_archive.os.lchown') as ownership:
                for target in (destination,link):
                    with self.assertRaisesRegex(RuntimeError,'empty real directory'):
                        archive.unpack(target,root/'missing.tar.gz',os.geteuid(),os.getegid())
                ownership.assert_not_called()
            self.assertEqual((destination/'retained').read_bytes(),b'do not overwrite')

    def test_empty_preowned_root_temporarily_owned_then_normalized_without_following_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); destination = root / 'home'; destination.mkdir(mode=0o700)
            source = root / 'archive.tar.gz'
            self.write_archive(source,[self.file('saved'),self.link('literal','saved')])
            with patch('volume_archive.os.lchown') as ownership:
                archive.unpack(destination,source,1000,1000)
            self.assertEqual(ownership.call_args_list[0].args,(destination,os.geteuid(),os.getegid()))
            self.assertEqual(ownership.call_args_list[-1].args,(str(destination),1000,1000))
            self.assertIn((str(destination/'literal'),1000,1000),[call.args for call in ownership.call_args_list])


if __name__ == '__main__': unittest.main()
