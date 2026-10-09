"""Deployment-owned frozen export plus pinned integration union; no live selection."""
from pathlib import Path
import json
import tomllib
import pm
from scripts.bundles.payload import seal_pm_runtime
root = Path('/app')
from pm.store import current_target
pm.prepare_tools(['python', 'uv', 'ffmpeg', 'node', 'npm', 'ripgrep'], out=root / 'tools', target=current_target(), cache=Path('/opt/hermes/build-cache'))
python = pm.installed_package('python').binary
pm.stage_manager_runtime(python=python, destination=root / 'pm-runtime', project=root / 'pm', cache=Path('/opt/hermes/build-cache'))
seal_pm_runtime(root, python)
requirements = root / 'deployment/darkserver/core-requirements.txt'
pm.export_requirements(root, requirements, extras=['all', 'telegram', 'matrix'], python=python, cache=Path('/opt/hermes/build-cache'), explicit=True)
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
executable = pm.build_requirements_environment(specs, out=root / 'venv', python=python, cache=Path('/opt/hermes/build-cache'), explicit=True)
(root / 'install-stamp.json').write_text(json.dumps({'schemaVersion': 2, 'distribution':'docker', 'source':'darkserver-native-candidate', 'updateMechanism':'external', 'pmRuntime':'/app/pm-runtime'}) + '\n')
for command, entry in tomllib.loads((root / 'pyproject.toml').read_text())['project']['scripts'].items():
    module, function = entry.split(':')
    launcher = root / 'venv/bin' / command
    launcher.write_text(f'#!/app/venv/bin/python3\nfrom {module} import {function}\nif __name__ == \"__main__\":\n    raise SystemExit({function}())\n')
    launcher.chmod(0o755)
print(executable)
