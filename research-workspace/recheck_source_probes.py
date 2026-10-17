#!/usr/bin/env python3
"""Recheck retained completed builds after a probe failure; never rebuild or promote."""
import argparse
import copy
import json
from pathlib import Path
import shutil

from build_sources import INPUTS, command, probe, sha, write


def recheck(source, output):
    original_bytes = source.read_bytes()
    original = json.loads(original_bytes)
    contract = json.loads((INPUTS/'inputs.json').read_text())
    if original['inputs'] != contract:
        raise ValueError('The retained construction inputs differ from the packaged contract.')
    builds = original['builds']
    if len(builds) != 2 or {b['runtime'] for b in builds} != {'python', 'r'} or any(b['repetition'] != 1 for b in builds):
        raise ValueError('Recheck requires one complete retained Python/R construction pair.')
    output.mkdir(parents=True, exist_ok=False)
    result = copy.deepcopy(original)
    result.update(status='FAIL', repetitions=1, builds=[], operation='post-build-probe-recheck',
                  original_build_report_sha256=sha(original_bytes), original_status=original['status'],
                  original_failure=original.get('failure'),
                  boundary='No image construction in this recheck. Original failed report and images remain unchanged. Only completed recorded construction outputs may be rechecked.')
    result.pop('failure', None)
    try:
        for prior in builds:
            runtime = prior['runtime']
            source_dir = source.parent/f'{runtime}-1'
            if not prior.get('image') or not prior.get('seconds') or not prior.get('oci_manifest_digest'):
                raise ValueError('Image construction did not finish; a clean rebuild is required.')
            if set(prior['context_sha256']) != {'Dockerfile','build.sh','inputs.json','source.tar.xz'}:
                raise ValueError('Unexpected construction context.')
            for name, expected in prior['context_sha256'].items():
                content = (source_dir/'context'/name).read_bytes()
                if sha(content) != expected:
                    raise ValueError('Retained context integrity failed.')
                if name != 'source.tar.xz' and content != (INPUTS/name).read_bytes():
                    raise ValueError('Packaged construction recipe has changed; recheck refused.')
            if prior['context_sha256']['source.tar.xz'] != contract['runtimes'][runtime]['sha256']:
                raise ValueError('Runtime source pin mismatch.')
            metadata = json.loads((source_dir/'metadata.json').read_text())
            if metadata.get('containerimage.digest') != prior['oci_manifest_digest'] or sha((source_dir/'build.log').read_bytes()) != prior['log_sha256']:
                raise ValueError('Completed construction evidence changed.')
            observed = json.loads(command(['docker','image','inspect',prior['image']]))[0]
            if observed['Id'] != prior['image_id'] or observed['Architecture'] != prior['architecture'] or observed['Os'] != 'linux':
                raise ValueError('Retained image identity or architecture mismatch.')
            destination = output/f'{runtime}-1'
            destination.mkdir()
            shutil.copytree(source_dir/'context', destination/'context')
            for name in ('build.log','metadata.json'):
                shutil.copyfile(source_dir/name, destination/name)
            record = copy.deepcopy(prior)
            record.update(status='FAIL', original_record_sha256=sha((source_dir/'build.json').read_bytes()), original_status=prior['status'])
            result['builds'].append(record)
            version_args = ['python','-c','import sys; print(".".join(map(str,sys.version_info[:3])))'] if runtime == 'python' else ['Rscript','--vanilla','-e','cat(as.character(getRversion()),"\\n")']
            record['observed_runtime_version'] = probe(prior['image'], version_args).strip()
            if record['observed_runtime_version'] != contract['runtimes'][runtime]['version']:
                raise ValueError('Runtime version mismatch.')
            for name in ('packages.tsv','configure.log','config.log'):
                (destination/name).write_text(probe(prior['image'], ['cat','/usr/local/share/waypoint-build/'+name]))
            record['package_inventory_sha256'] = sha((destination/'packages.tsv').read_bytes())
            record['status'] = 'PASS'
            write(destination/'build.json', record)
        result['status'] = 'PASS'
    except Exception as exc:
        result['failure'] = str(exc)
        raise
    finally:
        for record in result['builds']:
            write(output/f"{record['runtime']}-1"/'build.json', record)
        write(output/'builds.json', result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    recheck(args.builds.resolve(), args.output.resolve())
