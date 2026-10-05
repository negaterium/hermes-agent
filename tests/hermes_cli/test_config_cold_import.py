"""Fresh native strict loading, with the tripwire active before project imports."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


COLD_PROBE = r'''
import hashlib, json, os, pathlib, sys, traceback
root, home, managed = map(pathlib.Path, sys.argv[1:4])
case = sys.argv[4]
# Interpreter/library source is read-only; no blanket home allowance.
libraries = [pathlib.Path(sys.prefix).resolve(), pathlib.Path(sys.base_prefix).resolve()]
allowed_yaml = {home / 'config.yaml', managed / 'config.yaml'}
attempts = []
calls = []
reads = []

def deny(event, detail):
    attempts.append([event, str(detail)])
    raise PermissionError('synthetic cold-import tripwire')

def audit(event, args):
    if event == 'open':
        target, mode, flags = args
        if isinstance(target, int):
            return
        p = pathlib.Path(os.fsdecode(target)).absolute()
        resolved = p.resolve()
        if (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            deny(event, p)
        if p.name in {'.env', 'SOUL.md', 'auth.json'}:
            deny(event, p)
        source = p.is_relative_to(root) and p.suffix in {'.py', '.pyc', '.so'}
        library = any(resolved.is_relative_to(lib) for lib in libraries)
        if p in allowed_yaml or source or library:
            reads.append(str(p))
        else:
            deny(event, p)
    elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.rename', 'os.link',
                   'os.symlink', 'os.chmod', 'os.chown', 'os.truncate', 'os.utime',
                   'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.fork',
                   'sqlite3.connect', 'socket.connect', 'socket.bind', 'socket.getaddrinfo'}:
        deny(event, args[0] if args else '')
    elif event == 'import' and (args[0] == 'providers' or args[0].startswith('providers.')
                              or args[0] in {'hermes_cli.plugins', 'hermes_cli.plugins_discovery'}):
        calls.append(['import', args[0]])

def profile(frame, event, arg):
    if event != 'call':
        return
    module = frame.f_globals.get('__name__', '')
    name = frame.f_code.co_name
    forbidden = {
        'providers': {'list_providers', 'discover_providers'},
        'hermes_cli.plugins': {'discover_plugins'},
        'hermes_cli.plugins_discovery': {'discover_plugins'},
        'hermes_cli.config': {'ensure_hermes_home', '_inject_profile_env_vars'},
        'hermes_cli.env_loader': {'load_hermes_dotenv', '_apply_external_secret_sources',
                                 'hydrate_profile_secret_sources', 'get_secret_source_values'},
        'agent.secret_scope': {'build_profile_secret_scope', 'load_env_file', 'get_secret'},
    }
    if name in forbidden.get(module, set()):
        calls.append([module, name])
        # Observe these inherited calls but let the I/O tripwire deny their effects.

assert 'hermes_cli.config' not in sys.modules
sys.addaudithook(audit)
sys.setprofile(profile)
error = None
result = None
try:
    from hermes_cli.config_defaulted import load_config_strict, StrictConfigError
    if case.startswith('ref-'):
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        values = {'TERMINAL_CWD': 'assigned/value', 'SCOPE_VALUE': 'assigned/value'}
        if case == 'ref-empty':
            values = {key: '' for key in values}
        elif case == 'ref-missing':
            values = {}
        elif case == 'ref-invalid':
            values['TERMINAL_CWD'] = 42
        if case == 'ref-reintroduced':
            values['SCOPE_VALUE'] = '${UNRESOLVED_SYNTHETIC}'
        stamp = None if case == 'ref-unstamped' else str(home / 'foreign') if case == 'ref-foreign' else str(home)
        token = set_secret_scope(values, profile_home=stamp)
        try:
            result = load_config_strict(home)
        finally:
            reset_secret_scope(token)
    else:
        result = load_config_strict(home)
except Exception as exc:
    error = [type(exc).__name__, str(exc)]
finally:
    sys.setprofile(None)
print(json.dumps({'attempts': attempts, 'calls': calls, 'reads': reads,
                  'error': error, 'result': result,
                  'facade_imported': 'hermes_cli.config' in sys.modules,
                  'native_path': str(sys.modules['hermes_cli.config_defaulted'].__file__)}))
'''


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
            for p in root.rglob('*')}


@pytest.mark.parametrize('case', [
    'literal', 'defaults', 'aliases', 'ref-user', 'ref-managed', 'ref-empty',
    'ref-missing', 'ref-invalid', 'ref-unstamped', 'ref-foreign', 'unbound',
    'ref-source', 'ref-reintroduced', 'malformed', 'nonmapping', 'absent',
    'managed-malformed', 'managed-nonmapping', 'managed-missing-directory',
])
def test_cold_native_load_has_zero_forbidden_attempts(tmp_path, case):
    root = Path(__file__).resolve().parents[2]
    home = tmp_path / 'assigned'
    managed = tmp_path / 'managed'
    home.mkdir()
    managed.mkdir()
    (home / 'config.yaml').write_text('max_turns: 7\nmodel: {name: user/model}\n')
    (managed / 'config.yaml').write_text('model: managed/model\n')
    reference_body = (
        "model: {name: '${SCOPE_VALUE}'}\n"
        "task3a:\n  '${TERMINAL_CWD}':\n"
        "    - '${TERMINAL_CWD}'\n    - nested: '${env: SCOPE_VALUE }'\n"
        "    - 7\n    - null\n"
    )
    if case == 'defaults':
        (home / 'config.yaml').write_text('{}')
        (managed / 'config.yaml').unlink()
    elif case == 'aliases':
        (home / 'config.yaml').write_text('max_turns: 7\nagent: {max_turns: null}\nmodel: {name: user/model}\n')
        (managed / 'config.yaml').write_text('provider: 2\napi_base: https://managed.invalid\nmodel: {name: managed/model}\n')
    elif case.startswith('ref-') or case == 'unbound':
        (managed / 'config.yaml').write_text('{}')
        (home / 'config.yaml').write_text(reference_body)
        if case == 'ref-managed':
            (home / 'config.yaml').write_text('model: user/model\n')
            (managed / 'config.yaml').write_text(reference_body)
        elif case == 'ref-source':
            (home / 'config.yaml').write_text("model: '${vault:synthetic}'\n")
        elif case == 'ref-reintroduced':
            reference_body += "extra: '${SCOPE_VALUE}'\n"
            (home / 'config.yaml').write_text(reference_body)
    elif case == 'malformed':
        (home / 'config.yaml').write_text('[')
    elif case == 'nonmapping':
        (home / 'config.yaml').write_text('[]')
    elif case == 'absent':
        (home / 'config.yaml').unlink()
    elif case == 'managed-malformed':
        (managed / 'config.yaml').write_text('[')
    elif case == 'managed-nonmapping':
        (managed / 'config.yaml').write_text('[]')
    elif case == 'managed-missing-directory':
        (managed / 'config.yaml').unlink()
        managed.rmdir()
    before = _snapshot(tmp_path)
    env = {key: os.environ[key] for key in ('HOME', 'PATH', 'TMPDIR') if key in os.environ}
    env.update(TERMINAL_CWD='conflicting/parent', SCOPE_VALUE='conflicting/parent',
               HERMES_HOME=str(home), HERMES_MANAGED_DIR=str(managed),
               PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    completed = subprocess.run([sys.executable, '-B', '-c', COLD_PROBE,
                                str(root), str(home), str(managed), case],
                               cwd=root, env=env, text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout.splitlines()[-1])
    print('COLD_RECEIPT=' + json.dumps(receipt, sort_keys=True))
    assert _snapshot(tmp_path) == before, receipt
    assert receipt['attempts'] == [], receipt
    assert receipt['calls'] == [], receipt
    assert not receipt['facade_imported'], receipt
    assert receipt['native_path'] == str(root / 'hermes_cli/config_defaulted.py')
    refused = {'ref-missing', 'ref-invalid', 'ref-unstamped', 'ref-foreign', 'unbound',
               'ref-source', 'ref-reintroduced', 'malformed', 'nonmapping', 'absent',
               'managed-malformed', 'managed-nonmapping', 'managed-missing-directory'}
    if case in refused:
        assert receipt['error'][0] == 'StrictConfigError', receipt
        assert 'conflicting/parent' not in receipt['error'][1]
        assert receipt['result'] is None
    else:
        assert receipt['error'] is None, receipt
        result = receipt['result']
        if case == 'defaults':
            from hermes_cli.config_defaults import DEFAULT_CONFIG
            assert result == DEFAULT_CONFIG
        elif case.startswith('ref-'):
            value = '' if case == 'ref-empty' else 'assigned/value'
            assert result['task3a'] == {'${TERMINAL_CWD}': [value, {'nested': value}, 7, None]}
            assert result['model']['default'] == value
        else:
            assert result['agent']['max_turns'] == 7
            assert result['model']['default'] == 'managed/model'
            if case == 'aliases':
                assert result['model']['provider'] == '2'
                assert result['model']['base_url'] == 'https://managed.invalid'
                assert 'api_base' not in result and 'name' not in result['model']


def test_ordinary_and_strict_use_actual_shared_owner(tmp_path, monkeypatch):
    from hermes_cli import config, config_defaulted
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_MANAGED_DIR', raising=False)
    (tmp_path / 'config.yaml').write_text('max_turns: 7\nmodel: {name: native/model}\n')
    seen = []
    real = config_defaulted.DefaultedConfigComposition.resolve
    def observe(self, **kwargs):
        seen.append(self.dependencies)
        result = real(self, **kwargs)
        result[1]['shared_owner_control'] = True
        return result
    monkeypatch.setattr(config_defaulted.DefaultedConfigComposition, 'resolve', observe)
    ordinary = config.load_config()
    strict = config_defaulted.load_config_strict(tmp_path)
    assert ordinary == strict
    assert ordinary['shared_owner_control']
    assert ordinary['agent']['max_turns'] == 7
    assert ordinary['model']['default'] == 'native/model'
    assert seen == [config, config_defaulted.config_transforms]


def test_ordinary_one_argument_match_and_recursive_patch_seams(monkeypatch):
    from hermes_cli import config
    monkeypatch.setattr(config, '_env_expand_match', lambda match: 'matched')
    assert config._expand_env_vars({'${key}': ['${VALUE}', 7, None]}) == {
        '${key}': ['matched', 7, None]}
    original = config._expand_env_vars
    visited = []
    def recursive(value):
        visited.append(value)
        return original(value)
    monkeypatch.setattr(config, '_expand_env_vars', recursive)
    assert original({'nested': ['${VALUE}']}) == {'nested': ['matched']}
    assert visited == [['${VALUE}'], '${VALUE}']
    monkeypatch.setattr(config, '_is_non_env_secret_ref', lambda ref: True)
    assert config._env_ref_var_name('BARE') is None


def test_ordinary_normalizer_and_returned_merge_patch_seams(tmp_path, monkeypatch):
    from hermes_cli import config
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_MANAGED_DIR', raising=False)
    (tmp_path / 'config.yaml').write_text('model: {provider: 2, name: user/model}\n')
    monkeypatch.setattr(config, 'coerce_provider_id', lambda value: 'patched-provider')
    merge = config._deep_merge
    seen = []
    def returned(base, update):
        seen.append(update)
        return dict(merge(base, update), returned_merge=True)
    monkeypatch.setattr(config, '_deep_merge', returned)
    result = config.load_config()
    assert result['model']['provider'] == 'patched-provider'
    assert result['returned_merge'] and seen


# Native target policy: corresponding-root links allowed; outside targets refused.
SYMLINK_PROBE = r'''
import json, os, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
spec = json.loads(sys.argv[2])
libraries = [pathlib.Path(sys.prefix).resolve(), pathlib.Path(sys.base_prefix).resolve()]
pairs = {tuple(pair) for pair in spec['pairs']}
attempts, calls, reads, opens = [], [], [], []

def deny(event, detail):
    attempts.append([event, str(detail)])
    raise PermissionError('synthetic cold symlink tripwire')

def audit(event, args):
    if event == 'open':
        target, mode, flags = args
        if isinstance(target, int):
            return
        p = pathlib.Path(os.fsdecode(target)).absolute()
        resolved = p.resolve()
        pair = (str(p), str(resolved))
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            deny(event, p)
        if p.name in {'.env', 'SOUL.md', 'auth.json'}:
            deny(event, p)
        source = (p.is_relative_to(root) and resolved.is_relative_to(root)
                  and p.suffix in {'.py', '.pyc', '.so'})
        library = any(resolved.is_relative_to(lib) for lib in libraries)
        if pair in pairs or source or library:
            reads.append(list(pair))
        else:
            deny(event, p)
    elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.rename', 'os.link',
                   'os.symlink', 'os.chmod', 'os.chown', 'os.truncate', 'os.utime',
                   'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.fork',
                   'sqlite3.connect', 'socket.connect', 'socket.bind', 'socket.getaddrinfo'}:
        deny(event, args[0] if args else '')
    elif event == 'import' and (args[0] == 'providers' or args[0].startswith('providers.')
                              or args[0] in {'hermes_cli.plugins', 'hermes_cli.plugins_discovery'}):
        calls.append(['import', args[0]])

def profile(frame, event, arg):
    if event != 'call':
        return
    module = frame.f_globals.get('__name__', '')
    name = frame.f_code.co_name
    forbidden = {
        'providers': {'list_providers', 'discover_providers'},
        'hermes_cli.plugins': {'discover_plugins'},
        'hermes_cli.plugins_discovery': {'discover_plugins'},
        'hermes_cli.config': {'ensure_hermes_home', '_inject_profile_env_vars'},
        'hermes_cli.env_loader': {'load_hermes_dotenv', '_apply_external_secret_sources',
                                 'hydrate_profile_secret_sources', 'get_secret_source_values'},
        'agent.secret_scope': {'build_profile_secret_scope', 'load_env_file', 'get_secret'},
    }
    if name in forbidden.get(module, set()):
        calls.append([module, name])

original_open = pathlib.Path.open
def observe(self, *args, **kwargs):
    pair = [str(self.absolute()), str(self.resolve())]
    if tuple(pair) not in pairs:
        return original_open(self, *args, **kwargs)
    item = {'lexical': pair[0], 'resolved': pair[1], 'outcome': None}
    opens.append(item)
    try:
        # Deterministic narrow injection, not chmod/OS permission evidence.
        if pair[1] == spec['fault']:
            raise PermissionError('synthetic-private-symlink-detail')
        stream = original_open(self, *args, **kwargs)
    except OSError as exc:
        item['outcome'] = type(exc).__name__
        raise
    item['outcome'] = 'opened'
    return stream

assert not any(name.startswith('hermes_cli') for name in sys.modules)
sys.addaudithook(audit)
sys.setprofile(profile)
pathlib.Path.open = observe
error, result = None, None
try:
    from hermes_cli.config_defaulted import load_config_strict
    kwargs = {} if spec['config_path'] is None else {'config_path': spec['config_path']}
    result = load_config_strict(spec['home'], **kwargs)
except Exception as exc:
    error = {'type': type(exc).__name__, 'message': str(exc),
             'cause': type(exc.__cause__).__name__ if exc.__cause__ else None}
finally:
    sys.setprofile(None)
print(json.dumps({'attempts': attempts, 'calls': calls, 'reads': reads, 'opens': opens,
                  'error': error, 'result': result,
                  'facade_imported': 'hermes_cli.config' in sys.modules,
                  'native_path': str(sys.modules['hermes_cli.config_defaulted'].__file__)}))
'''


def _symlink_snapshot(root):
    """Do not follow directory aliases; preserve bytes and lstat/link identities."""
    import stat
    result = {}
    def visit(directory):
        for p in directory.iterdir():
            info = p.lstat()
            identity = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
            if stat.S_ISLNK(info.st_mode):
                content = os.readlink(p)
            elif stat.S_ISREG(info.st_mode):
                content = p.read_bytes()
            else:
                content = None
            result[str(p.relative_to(root))] = (identity, content)
            if stat.S_ISDIR(info.st_mode):
                visit(p)
    visit(root)
    return result


def _symlink_case(tmp_path, layer, kind, mode):
    assert tmp_path == tmp_path.resolve(), 'runner-owned canonical tmp_path required'
    home, managed, outside = (tmp_path / name for name in ('assigned', 'managed', 'outside-layer'))
    for directory in (home, managed, outside):
        directory.mkdir()
    for name, directory in (('user', home), ('managed', managed)):
        (directory / 'config.yaml').write_text(
            f'model: baseline/{name}\nsymlink_{name}: baseline/{name}\nagent: {{max_turns: 7}}\n')
    directory = home if layer == 'user' else managed
    lexical = directory / 'config.yaml'
    marker = f'consumed/{layer}/{kind}/{mode}'
    target = {
        'regular': lexical,
        'same-root': directory / 'same-root.yaml',
        'outside-layer': outside / f'{layer}.yaml',
        'dangling': directory / 'missing-target.yaml',
        'unreadable-injected': directory / 'permission-target.yaml',
    }[kind]
    if kind != 'regular':
        lexical.unlink()
        lexical.symlink_to(target)
    if kind != 'dangling':
        target.write_text(f'model: {marker}\nsymlink_{layer}: {marker}\nagent: {{max_turns: 11}}\n')
    alias_home, alias_managed = tmp_path / 'assigned-alias', tmp_path / 'managed-alias'
    alias_home.symlink_to(home, target_is_directory=True)
    alias_managed.symlink_to(managed, target_is_directory=True)
    selected_home = alias_home if mode == 'aliases' else home
    selected_managed = alias_managed if mode == 'aliases' else managed
    config_path = None if mode == 'implicit' else str(selected_home / 'config.yaml')
    requests = [selected_home / 'config.yaml', selected_managed / 'config.yaml']
    # Exact lexical and resolved requests/counterparts, no owned-home blanket.
    pairs = sorted({(str(p), str(p.resolve())) for p in [*requests, home / 'config.yaml',
                   managed / 'config.yaml', target]})
    assert all(Path(p).is_relative_to(tmp_path) for pair in pairs for p in pair)
    assert selected_home.resolve() == home and selected_managed.resolve() == managed
    assert target.is_relative_to(directory) == (kind != 'outside-layer')
    return {'home': str(selected_home), 'managed': str(selected_managed),
            'config_path': config_path, 'pairs': pairs,
            'fault': str(target) if kind == 'unreadable-injected' else None,
            'target': str(target), 'requests': [str(p) for p in requests],
            'marker': marker, 'layer': layer, 'kind': kind, 'mode': mode}


def _assert_symlink_receipt(spec, receipt):
    index = 0 if spec['layer'] == 'user' else 1
    if spec['kind'] == 'outside-layer':
        assert receipt['result'] is None, receipt
        if index == 0:
            assert receipt['opens'] == [], receipt
        else:
            assert len(receipt['opens']) == 1, receipt
            assert receipt['opens'][0]['lexical'] == spec['requests'][0], receipt
            assert receipt['opens'][0]['outcome'] == 'opened', receipt
        assert receipt['error'] == {
            'type': 'StrictConfigError',
            'message': 'configuration target outside corresponding root',
            'cause': 'ConfigFileBoundaryError',
        }, receipt
        assert not any(pair[1] == spec['target'] for pair in receipt.get('reads', [])), receipt
        assert not any(item['resolved'] == spec['target'] for item in receipt['opens']), receipt
        for private in (spec['target'], spec['marker'], 'synthetic-private-symlink-detail'):
            assert private not in receipt['error']['message']
        return
    expected = {'lexical': spec['requests'][index], 'resolved': spec['target'],
                'outcome': {'dangling': 'FileNotFoundError',
                            'unreadable-injected': 'PermissionError'}.get(spec['kind'], 'opened')}
    assert expected in receipt['opens'], receipt  # Includes negative seam execution.
    assert len(receipt['opens']) == (1 if index == 0 and expected['outcome'] != 'opened' else 2)
    assert receipt['opens'][0]['lexical'] == spec['requests'][0]
    if expected['outcome'] != 'opened':
        assert receipt['result'] is None
        assert receipt['error'] == {'type': 'StrictConfigError',
                                    'message': 'assigned configuration unreadable or malformed',
                                    'cause': expected['outcome']}, receipt
        for private in (spec['target'], spec['marker'], 'synthetic-private-symlink-detail'):
            assert private not in receipt['error']['message']
    else:
        assert receipt['error'] is None, receipt
        result = receipt['result']
        assert result['symlink_' + spec['layer']] == spec['marker']
        other = 'managed' if index == 0 else 'user'
        assert result['symlink_' + other] == 'baseline/' + other
        assert result['model']['default'] == ('baseline/managed' if index == 0 else spec['marker'])
        assert result['agent']['max_turns'] == (7 if index == 0 else 11)


@pytest.mark.parametrize('layer', ['user', 'managed'])
@pytest.mark.parametrize('kind', ['regular', 'same-root', 'outside-layer', 'dangling', 'unreadable-injected'])
@pytest.mark.parametrize('mode', ['implicit', 'explicit', 'aliases'])
def test_cold_symlink_characterization(tmp_path, layer, kind, mode, record_property):
    root = Path(__file__).resolve().parents[2]
    spec = _symlink_case(tmp_path, layer, kind, mode)
    before = _symlink_snapshot(tmp_path)
    env = {key: os.environ[key] for key in ('HOME', 'PATH', 'TMPDIR') if key in os.environ}
    env.update(HERMES_HOME=spec['home'], HERMES_MANAGED_DIR=spec['managed'],
               PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    completed = subprocess.run([sys.executable, '-B', '-c', SYMLINK_PROBE, str(root), json.dumps(spec)],
                               cwd=root, env=env, text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout.splitlines()[-1])
    record_property('symlink_receipt', json.dumps({'spec': spec, 'receipt': receipt}, sort_keys=True))
    print('SYMLINK_COLD_RECEIPT=' + json.dumps({'spec': spec, 'receipt': receipt}, sort_keys=True))
    assert _symlink_snapshot(tmp_path) == before, receipt
    assert receipt['attempts'] == [] and receipt['calls'] == [], receipt
    assert not receipt['facade_imported'], receipt
    assert receipt['native_path'] == str(root / 'hermes_cli/config_defaulted.py')
    _assert_symlink_receipt(spec, receipt)
    for observation in receipt['opens']:
        pair = [observation['lexical'], observation['resolved']]
        assert (pair in receipt['reads']) == (observation['outcome'] != 'PermissionError'), receipt
