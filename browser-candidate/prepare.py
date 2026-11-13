# SPDX-License-Identifier: Apache-2.0
"""Prepare a separate, opt-in accessibility candidate from the staged NHANES site."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile
from io import BytesIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'course-workspace/classroom'))
from browser_bundle import bundle  # noqa: E402


def prepare(source, output):
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
    tags = '<link rel="stylesheet" href="./waypoint-browser.css">\n'
    tags += '<script defer src="./waypoint-browser.js"></script>\n'
    (output / 'lab/index.html').write_text(html.replace('</head>', tags + '</head>'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        prepare(args.source, args.output)
    except (OSError, KeyError, ValueError) as exc:
        parser.exit(2, f'{exc}\n')
