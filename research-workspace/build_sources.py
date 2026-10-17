#!/usr/bin/env python3
"""Bounded clean source builds; never promote images or prune shared Docker state."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
INPUTS = ROOT / 'source-builds'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def command(args, timeout=30):
    return subprocess.check_output(args, timeout=timeout, stderr=subprocess.STDOUT).decode()


def bounded(args, log, *, timeout, maximum):
    """Drain output continuously, kill the client at the finite limit, retain logs."""
    started, size = time.monotonic(), 0
    with log.open('xb') as stream, selectors.DefaultSelector() as selector:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                if time.monotonic() - started > timeout:
                    raise TimeoutError('Command exceeded its absolute time bound.')
                for key, _ in selector.select(0.25):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(data)
                    stream.write(data[:max(0, maximum - (size - len(data)))])
                    stream.flush()
                    if size > maximum:
                        raise ValueError('Build output exceeded its retained log bound.')
            if process.wait(timeout=10):
                raise ValueError('Command failed; inspect retained log.')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdout.close()
    return round(time.monotonic() - started, 3)


def preflight(output, contract, platform):
    info = json.loads(command(['docker', 'info', '--format', '{{json .}}']))
    native = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(info['Architecture'], info['Architecture'])
    if platform != 'linux/' + native:
        raise ValueError('Only native builds are accepted; emulated architectures require separate evidence.')
    free = shutil.disk_usage(output).free
    if free < contract['limits']['minimum_free_bytes']:
        raise ValueError('Insufficient free storage; no build started.')
    if info['NCPU'] < 2 or info['MemTotal'] < 4 * 1024**3:
        raise ValueError('Insufficient Docker capacity; no build started.')
    return {'host_free_bytes': free, 'docker_cpus': info['NCPU'], 'docker_memory_bytes': info['MemTotal'],
            'docker_architecture': info['Architecture'], 'docker_server_version': info['ServerVersion']}


def fetch(runtime, destination):
    with urllib.request.urlopen(runtime['url'], timeout=60) as response:
        payload = response.read(64 * 1024**2 + 1)
    if len(payload) > 64 * 1024**2 or sha(payload) != runtime['sha256']:
        raise ValueError('Upstream source size or SHA-256 mismatch; no source was executed.')
    destination.write_bytes(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True, choices=['linux/arm64', 'linux/amd64'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, choices=[1, 2], default=1)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    contract = json.loads((INPUTS / 'inputs.json').read_text())
    packaged = {name: (INPUTS/name).read_bytes() for name in ('Dockerfile', 'build.sh', 'inputs.json')}
    report = {'schema_version': 1, 'status': 'NOT RUN', 'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'platform': args.platform, 'repetitions': args.repetitions, 'builds': [],
              'promotion': 'DISABLED', 'independent_human_second_machine': 'NOT RUN',
              'bit_identical_images': 'NOT CLAIMED', 'base_os_built_from_source': False,
              'runner_sha256': sha(Path(__file__).read_bytes()),
              'source_revision': command(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip(),
              'tracked_changes': command(['git', '-C', str(ROOT), 'status', '--short']),
              'inputs': contract, 'input_hashes': {name: sha(data) for name, data in packaged.items()}}
    builder = 'waypoint-source-' + uuid.uuid4().hex[:12]
    report['builder'] = builder
    created = False
    try:
        report['preflight'] = preflight(output, contract, args.platform)
        report['docker_version'] = json.loads(command(['docker', 'version', '--format', '{{json .}}']))
        report['buildx_version'] = command(['docker', 'buildx', 'version']).strip()
        write(output / 'builds.json', report)
        # A fresh dedicated builder has no imported build cache. No --use: the
        # user's current builder remains selected. Stop, retain, never prune it.
        command(['docker', 'buildx', 'create', '--name', builder, '--driver', 'docker-container',
                 '--driver-opt', 'image=' + contract['buildkit_image'],
                 '--driver-opt', 'memory=3g', '--driver-opt', 'memory-swap=3g',
                 '--driver-opt', 'cpu-period=100000', '--driver-opt', 'cpu-quota=200000'], timeout=60)
        created = True
        report['status'] = 'FAIL'
        bounded(['docker', 'buildx', 'inspect', builder, '--bootstrap'], output / 'builder.log', timeout=180, maximum=1024**2)
        for repetition in range(1, args.repetitions + 1):
            for runtime_id, runtime in contract['runtimes'].items():
                record = {'runtime': runtime_id, 'runtime_version': runtime['version'], 'repetition': repetition,
                          'status': 'FAIL', 'platform': args.platform, 'base_image': contract['base_images'][args.platform]}
                report['builds'].append(record)
                directory = output / f'{runtime_id}-{repetition}'
                context = directory / 'context'
                context.mkdir(parents=True)
                record['preflight'] = preflight(output, contract, args.platform)
                for filename in ('Dockerfile', 'build.sh', 'inputs.json'):
                    (context / filename).write_bytes(packaged[filename])
                fetch(runtime, context / 'source.tar.xz')
                record['context_sha256'] = {p.name: sha(p.read_bytes()) for p in sorted(context.iterdir())}
                tag = f'waypoint-source-{runtime_id}:{builder}-{repetition}'
                cmd = ['docker', 'buildx', 'build', '--builder', builder, '--platform', args.platform,
                       '--no-cache', '--pull', '--provenance=false', '--load', '--progress=plain',
                       '--metadata-file', str(directory / 'metadata.json'), '--tag', tag,
                       '--build-arg', 'BASE_IMAGE=' + record['base_image'],
                       '--build-arg', 'SNAPSHOT=' + contract['debian_snapshot'],
                       '--build-arg', 'RUNTIME=' + runtime_id, str(context)]
                record['command'] = cmd
                write(output / 'builds.json', report)
                record['seconds'] = bounded(cmd, directory / 'build.log', timeout=contract['limits']['build_seconds'], maximum=contract['limits']['log_bytes'])
                inspection = json.loads(command(['docker', 'image', 'inspect', tag]))[0]
                metadata = json.loads((directory / 'metadata.json').read_text())
                record.update(image=inspection['Id'], image_id=inspection['Id'], repo_digests=inspection['RepoDigests'],
                              oci_manifest_digest=metadata.get('containerimage.digest'),
                              oci_config_digest=metadata.get('containerimage.config.digest'),
                              architecture=inspection['Architecture'], os=inspection['Os'], size=inspection['Size'])
                if inspection['Architecture'] != args.platform.split('/')[1] or inspection['Os'] != 'linux':
                    raise ValueError('Built image platform mismatch.')
                probe = ['python', '-c', 'import sys; print(".".join(map(str,sys.version_info[:3])))'] if runtime_id == 'python' else ['Rscript', '--vanilla', '-e', 'cat(as.character(getRversion()),"\\n")']
                run = ['docker', 'run', '--rm', '--network=none', '--cpus=1', '--memory=256m', '--pids-limit=64', '--read-only', record['image']]
                record['observed_runtime_version'] = command(run + probe).strip()
                if record['observed_runtime_version'] != runtime['version']:
                    raise ValueError('Observed runtime version mismatch.')
                for filename in ('packages.tsv', 'configure.log', 'config.log'):
                    data = command(run + ['cat', '/usr/local/share/waypoint-build/' + filename])
                    (directory / filename).write_text(data)
                record['package_inventory_sha256'] = sha((directory / 'packages.tsv').read_bytes())
                record['log_sha256'] = sha((directory / 'build.log').read_bytes())
                record['status'] = 'PASS'
                write(directory / 'build.json', record)
                write(output / 'builds.json', report)
                print(json.dumps({k: record[k] for k in ('runtime', 'repetition', 'status', 'image', 'seconds')}), flush=True)
        report['status'] = 'PASS'
    except Exception as exc:
        report['failure'] = str(exc)
        raise
    finally:
        if created:
            try:
                command(['docker', 'buildx', 'stop', builder], timeout=60)
                report['builder_stopped'] = True
            except Exception as exc:
                report['builder_stopped'] = False
                report['stop_error'] = str(exc)
                report['status'] = 'FAIL'
        for record in report['builds']:
            directory = output / f"{record['runtime']}-{record['repetition']}"
            if (directory / 'build.log').exists():
                record['log_sha256'] = sha((directory / 'build.log').read_bytes())
            write(directory / 'build.json', record)
        write(output / 'builds.json', report)


if __name__ == '__main__':
    main()
