# SPDX-License-Identifier: Apache-2.0
"""Prepare a separate, opt-in accessibility candidate from the staged NHANES site."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile
from io import BytesIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'course-workspace/classroom'))
from browser_bundle import bundle  # noqa: E402


def prepare(source, output, mobile=False):
    config = json.loads((source / 'jupyter-lite.json').read_text())
    data = config['jupyter-config-data']
    kernel = data['litePluginSettings']['@jupyterlite/pyodide-kernel-extension:kernel']
    if (data['appVersion'] != '0.8.7'
            or data['versionInfo']['@jupyterlab/application']['version'] != '4.6.5'
            or kernel.get('pyodideUrl') != '/pyodide/pyodide.mjs'
            or kernel.get('disablePyPIFallback') is not True):
        raise ValueError('Expected the pinned, locally staged cold-run site.')
    manifest = json.loads((source / 'files/bundle-manifest.json').read_text())
    expected, report = bundle('nhanes-v1', manifest['hosted_fallback']['origin'])
    with zipfile.ZipFile(BytesIO(expected)) as archive:
        staged = source / 'files'
        actual = {p.relative_to(staged).as_posix() for p in staged.rglob('*') if not p.is_dir()}
        if actual != set(archive.namelist()) or any(p.is_symlink() for p in staged.rglob('*')):
            raise ValueError('Staged content is not the exact reviewed file set.')
        for name in archive.namelist():
            candidate = source / 'files' / name
            if not candidate.is_file() or candidate.is_symlink() or candidate.read_bytes() != archive.read(name):
                raise ValueError(f'Staged starter differs from reviewed bundle: {name}')
    html = (source / 'lab/index.html').read_text()
    if html.count('</head>') != 1 or 'waypoint-browser.js' in html:
        raise ValueError('Expected the unmodified Lab entry page.')
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(source.resolve()):
        raise ValueError('Choose a new destination outside the input site.')
    if mobile:
        runtime = source / 'pyodide'
        # Hashes from the NHANES cold-run asset inventory, Pyodide 314.0.6.
        digests = {
            'pyodide-lock.json': '3fdaef09e9e365c85e002737720f8d0ab8f278c1c244a2dde6a37663cf488ad4',
            'pyodide.mjs': '69e3f6ccec3e14b465df60be577ca62f536251406b9a00cce019eac5252a2495',
            'pyodide.asm.mjs': '2ac5eba365ec12839c75c03b39b3be1dd63b798852cc460b014b52238be042f7',
            'pyodide.asm.wasm': '3a0a00dfeaa348ac20f9ef09904233d32d33f644339662d4af368f8a2010f37a',
            'python_stdlib.zip': '80c5be6babfe03297069703410c3c29404dcf2525d2b128746bae5536f94831f',
        }
        for name, digest in digests.items():
            if hashlib.sha256((runtime / name).read_bytes()).hexdigest() != digest:
                raise ValueError(f'Changed Pyodide 314.0.6 runtime file: {name}')
        packages = json.loads((runtime / 'pyodide-lock.json').read_text())['packages']
        pending = {'pandas'}
        checked = set()
        while pending:
            name = pending.pop()
            if name in checked:
                continue
            checked.add(name)
            package = packages[name]
            wheel = runtime / package['file_name']
            if not wheel.is_file() or hashlib.sha256(wheel.read_bytes()).hexdigest() != package['sha256']:
                raise ValueError(f'Missing or changed local wheel: {name}')
            pending.update(package['depends'])
    # Keep the app API opt-in and scoped to Lab. The upstream site remains intact.
    lab_config = source / 'lab/jupyter-lite.json'
    lab = json.loads(lab_config.read_text()) if lab_config.exists() else {}
    lab_data = lab.setdefault('jupyter-config-data', {})
    lab_data['exposeAppInBrowser'] = True
    tracker = lab_data.setdefault('settingsOverrides', {}).setdefault('@jupyterlab/notebook-extension:tracker', {})
    tracker.setdefault('codeCellConfig', {})['lineWrap'] = True
    tracker['windowingMode'] = 'none'  # Seven cells; keep focus/reading order stable.
    shutil.copytree(source, output)
    (output / 'lab/jupyter-lite.json').write_text(json.dumps(lab, indent=2) + '\n')
    assets = Path(__file__).parent
    for name in ('waypoint-browser.js', 'waypoint-browser.css'):
        shutil.copyfile(assets / name, output / 'lab' / name)
    (output / 'lab/waypoint-browser.json').write_text(json.dumps({
        'fallback': report['hosted_fallback']['origin'],
        'files': report['submission_files'],
        'notebook': 'nhanes-wrangling.ipynb',
    }, indent=2) + '\n')
    if mobile:
        shutil.copytree(assets / 'mobile', output / 'mobile')
    tags = '<link rel="stylesheet" href="./waypoint-browser.css">\n'
    tags += '<script defer src="./waypoint-browser.js"></script>\n'
    (output / 'lab/index.html').write_text(html.replace('</head>', tags + '</head>'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--mobile', action='store_true', help='Also stage the simple notebook view.')
    args = parser.parse_args()
    try:
        prepare(args.source, args.output, mobile=args.mobile)
    except (OSError, KeyError, ValueError) as exc:
        parser.exit(2, f'{exc}\n')
