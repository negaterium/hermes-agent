"""Deployment-owned frozen export plus pinned integration union; no live selection."""
from pathlib import Path
import json
import os
import re
import tomllib
import pm
from scripts.bundles.payload import seal_pm_runtime
root = Path('/app')
from pm.store import current_target
pm.prepare_tools(['python', 'uv', 'ffmpeg', 'node', 'npm', 'ripgrep', 'chromium'], out=root / 'tools', target=current_target(), cache=Path('/opt/hermes/build-cache'))
python = pm.installed_package('python').binary
pm.stage_manager_runtime(python=python, destination=root / 'pm-runtime', project=root / 'pm', cache=Path('/opt/hermes/build-cache'))
seal_pm_runtime(root, python)
requirements = root / 'deployment/darkserver/core-requirements.txt'
pm.export_requirements(root, requirements, extras=['all', 'telegram', 'matrix', 'audio-io', 'edge-tts'], python=python, cache=Path('/opt/hermes/build-cache'), explicit=True)
specs = [line.strip() for line in requirements.read_text().splitlines() if line.strip() and not line.startswith('#')]
member = root / 'deployment/darkserver/integration-member'
# This command runs under the base image's system Python, before the Hermes
# runtime environment exists.  The native declaration reader validates
# requirements through ``packaging``, which is available to the application
# runtime but not guaranteed in that bootstrap interpreter.  Parse this
# deployment-owned member with stdlib TOML instead, then keep the checked
# declaration/manifest parity gate here and in pm_operator.py.
member_document = tomllib.loads((member / 'pyproject.toml').read_text(encoding='utf-8-sig'))
member_specs = member_document.get('project', {}).get('dependencies')
if (not isinstance(member_specs, list) or not member_specs
        or any(not isinstance(item, str) for item in member_specs)):
    raise RuntimeError('deployment integration member has invalid project.dependencies')
member_specs = list(member_specs)
manifest_specs = [line.strip() for line in (root / 'deployment/darkserver/integrations.txt').read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith('#')]
if manifest_specs != member_specs:
    raise RuntimeError('integrations.txt must exactly match the native PM integration member')
specs += member_specs
if (root / 'tinker-atropos/pyproject.toml').is_file():
    member_name = tomllib.loads((root / 'tinker-atropos/pyproject.toml').read_text())['project']['name']
    specs += [f'{member_name} @ file:///app/tinker-atropos']
executable = pm.build_requirements_environment(specs, out=root / 'venv', python=python, cache=Path('/opt/hermes/build-cache'), explicit=True, lock=root / 'uv.lock')
stamp_path = root / 'install-stamp.json'
if stamp_path.is_file():
    stamp = json.loads(stamp_path.read_text(encoding='utf-8-sig'))
    if not isinstance(stamp, dict) or not stamp.get('commit'):
        raise RuntimeError('pre-seeded install stamp must contain a commit identity')
else:
    context_sha = os.environ.get('HERMES_CONTEXT_SHA256', '').strip().lower()
    measured_context = Path('/opt/hermes/build-context.sha256')
    if measured_context.is_file():
        measured_sha = measured_context.read_text(encoding='ascii').strip().lower()
        if not re.fullmatch(r'[0-9a-f]{64}', measured_sha):
            raise RuntimeError('measured COPY payload identity must be a SHA-256 digest')
        if context_sha and context_sha != measured_sha:
            raise RuntimeError('supplied context identity differs from the measured COPY payload')
        context_sha = measured_sha
    build_commit = os.environ.get('HERMES_BUILD_COMMIT', '').strip().lower()
    if context_sha and not re.fullmatch(r'[0-9a-f]{64}', context_sha):
        raise RuntimeError('HERMES_CONTEXT_SHA256 must be a 64-character hexadecimal digest')
    if build_commit and not re.fullmatch(r'[0-9a-f]{40}', build_commit):
        raise RuntimeError('HERMES_BUILD_COMMIT must be a 40-character hexadecimal commit')
    if not build_commit and not context_sha:
        raise RuntimeError('supply HERMES_BUILD_COMMIT or HERMES_CONTEXT_SHA256 for Docker provenance')
    commit = build_commit or context_sha[:40]
    stamp = {
        'schemaVersion': 2,
        'commit': commit,
        'baseVersion': 'unknown',
        'displayVersion': (f"context.{context_sha[:12]}" if context_sha else None),
        'dirty': bool(context_sha and not build_commit),
        'source': 'docker',
        'distribution': 'docker',
        'updateMechanism': 'external',
    }
    if context_sha:
        stamp['buildContextSha256'] = context_sha
stamp['pmRuntime'] = '/app/pm-runtime'
stamp_path.write_text(json.dumps(stamp, sort_keys=True) + '\n', encoding='utf-8')
(root / '.install_method').write_text('docker\n', encoding='utf-8')
for command, entry in tomllib.loads((root / 'pyproject.toml').read_text())['project']['scripts'].items():
    module, function = entry.split(':')
    launcher = root / 'venv/bin' / command
    launcher.write_text(f'#!/app/venv/bin/python3\nfrom {module} import {function}\nif __name__ == \"__main__\":\n    raise SystemExit({function}())\n')
    launcher.chmod(0o755)
print(executable)
