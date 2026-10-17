"""Source provenance, disabled candidates, offline metadata and lineage controls."""
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import build_sources
import qualify_sources
from research_workspace import catalog, evidence, reproduction


class SourceBuildTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, self.root)
        contract = json.loads((build_sources.INPUTS/'inputs.json').read_text())
        self.report = {'inputs': contract, 'platform': 'linux/arm64', 'source_revision': 'a'*40}
        self.build = {'runtime': 'python', 'repetition': 1, 'image': 'sha256:'+'b'*64,
                      'observed_runtime_version': contract['runtimes']['python']['version'], 'architecture': 'arm64',
                      'base_image': contract['base_images']['linux/arm64'],
                      'context_sha256': {name: 'c'*64 for name in ('Dockerfile', 'build.sh', 'inputs.json')},
                      'package_inventory_sha256': 'd'*64, 'record_sha256': 'e'*64}
        self.entry = qualify_sources.candidate(self.build, self.report)

    def test_candidate_is_unavailable_and_does_not_replace_legacy(self):
        document = json.loads((catalog.ROOT/'environments.json').read_text())
        with patch.object(catalog, '_load', return_value=document['environments']+[self.entry]):
            entries = catalog.list_environments({'python': self.entry['image']})
        self.assertFalse(any(e['available'] for e in entries))
        self.assertEqual(document['environments'][0]['id'], 'summary-python-v1')

    def test_offline_snapshot_never_opens_packaged_source(self):
        with patch.object(catalog, '_read_packaged', side_effect=AssertionError('No file reads')):
            catalog.validate_snapshot(self.entry, self.entry['recipe'], self.entry['recipe_sha256'], self.entry['image'])
            with self.assertRaises(ValueError):
                catalog.validate_snapshot(self.entry, self.entry['recipe'], '0'*64, self.entry['image'])

    def test_bundle_retains_snapshot_and_rejects_lineage_substitution(self):
        from test_evidence import fixture, members
        project, runs, files = fixture()
        entry = deepcopy(self.entry)
        entry['recipe_sha256'] = runs[0]['recipe_sha256']
        runs[0]['environment'].update(image=entry['image'], specification=entry)
        payload = evidence.build_bundle(project, runs, files)
        with patch.object(catalog, '_read_packaged', side_effect=AssertionError('No file reads')):
            self.assertTrue(evidence.verify_bundle(payload)['valid'])
        exported = json.loads(members(payload)['runs.json'])[0]
        self.assertEqual(exported['environment']['specification']['dependencies']['additional_packages'], [])
        self.assertEqual(exported['environment']['specification']['provenance']['build_inputs'], entry['provenance']['build_inputs'])
        self.assertEqual(json.loads(members(payload)['comparison.json'])['runs'][0]['lineage'], evidence.lineage(runs[0]))
        runs[0]['environment']['specification']['image'] = 'sha256:'+'0'*64
        with self.assertRaises(ValueError):
            evidence.build_bundle(project, runs, files)

    def test_recorded_inputs_require_complete_immutable_provenance(self):
        for mutate in (
            lambda e: e['provenance']['build_inputs'].pop(0),
            lambda e: e['provenance']['build_inputs'][0].update(sha256='not-a-hash'),
            lambda e: e['provenance']['build_inputs'][0].update(kind=[]),
            lambda e: e.update(image='python:latest'),
            lambda e: e['dependencies'].update(additional_packages=['numpy']),
            lambda e: e['provenance']['build_inputs'].append(deepcopy(e['provenance']['build_inputs'][0])),
        ):
            entry = deepcopy(self.entry)
            mutate(entry)
            with self.assertRaises(ValueError):
                catalog.validate_snapshot(entry, entry['recipe'], entry['recipe_sha256'], entry['image'])

    def test_each_lineage_dimension_changes_independently(self):
        run = {'input_sha256': 'a'*64, 'recipe_sha256': 'b'*64, 'condition': 'baseline', 'parameters': {},
               'environment': {'runtime': 'python', 'image': 'sha256:'+'c'*64, 'runner': 'outfitter-docker-v1'}}
        baseline = evidence.lineage(run)
        changes = [('input_sha256', lambda r: r.update(input_sha256='d'*64)),
                   ('recipe_sha256', lambda r: r.update(recipe_sha256='d'*64)),
                   ('parameters_sha256', lambda r: r.update(condition='alternative')),
                   ('environment_sha256', lambda r: r['environment'].update(image='sha256:'+'d'*64))]
        for dimension, mutate in changes:
            changed = deepcopy(run)
            mutate(changed)
            self.assertEqual([k for k,v in evidence.lineage(changed).items() if v != baseline[k]], [dimension])

    def test_source_checksum_mismatch_cannot_create_context_source(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'untrusted source')):
            with self.assertRaises(ValueError):
                build_sources.fetch({'url': 'https://www.python.org/test', 'sha256': '0'*64}, self.root/'source')
        self.assertFalse((self.root/'source').exists())

    def test_foreign_architecture_and_capacity_fail_before_build(self):
        contract = self.report['inputs']
        for architecture, memory, cpus in [('x86_64', 8*1024**3, 8), ('aarch64', 1024**3, 8), ('aarch64', 8*1024**3, 1)]:
            info = {'Architecture': architecture, 'MemTotal': memory, 'NCPU': cpus}
            with patch.object(build_sources, 'command', return_value=json.dumps(info)), self.assertRaises(ValueError):
                build_sources.preflight(self.root, contract, 'linux/arm64')

    def test_output_and_time_limits_retain_failure_logs(self):
        with self.assertRaises(ValueError):
            build_sources.bounded([sys.executable, '-c', 'print("x"*20000)'], self.root/'large.log', timeout=5, maximum=100)
        self.assertEqual((self.root/'large.log').stat().st_size, 100)
        with self.assertRaises(TimeoutError):
            build_sources.bounded([sys.executable, '-c', 'import time; time.sleep(5)'], self.root/'timeout.log', timeout=0.1, maximum=100)
        self.assertTrue((self.root/'timeout.log').is_file())

    def test_probe_timeout_cleans_only_the_exact_owned_container(self):
        def inspect(args, **kwargs):
            name = args[-1]
            return subprocess.CompletedProcess(args, 0, json.dumps([{'Config': {'Labels': {'org.educloud.source-probe': name}}}]).encode(), b'')
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if args[1] == 'run':
                raise subprocess.TimeoutExpired(args, 30)
            return ''
        with patch.object(build_sources, 'command', side_effect=command), patch('subprocess.run', side_effect=inspect):
            with self.assertRaises(subprocess.TimeoutExpired):
                build_sources.probe(self.entry['image'], ['python', '--version'])
        self.assertEqual(calls[-1][1:4], ['container', 'rm', '--force'])
        self.assertEqual(calls[-1][-1], calls[0][calls[0].index('--name')+1])

    def test_probe_cleanup_refuses_foreign_ownership(self):
        inspected = subprocess.CompletedProcess([], 0, b'[{"Config":{"Labels":{"org.educloud.source-probe":"foreign"}}}]', b'')
        with patch.object(build_sources, 'command', return_value='') as command, patch('subprocess.run', return_value=inspected):
            with self.assertRaises(ValueError):
                build_sources.probe(self.entry['image'], ['python', '--version'])
        self.assertEqual(command.call_count, 1)

    def test_historical_bundle_still_verifies_without_source_build_metadata(self):
        payload = (catalog.ROOT/'evidence/2026-10-17/browser-evidence.zip').read_bytes()
        result = evidence.verify_bundle(payload)
        self.assertTrue(result['valid'])
        self.assertEqual(result['bundle_sha256'], '888bbb96e611e6ba4a9b6181b2d8a1b314ac6167bf39d2642fb2e8c07ab20d87')


if __name__ == '__main__':
    unittest.main()
