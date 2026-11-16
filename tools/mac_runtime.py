"""Reject Python/OpenSSL drift before packaging and after py2app rewrites/signs it."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

PIN = Path(__file__).with_name('mac_runtime_pin.json')


def output(*args):
    return subprocess.check_output(args, text=True)


def fingerprint(path):
    result = {}
    for arch in ('arm64', 'x86_64'):
        uuids = output('/usr/bin/dwarfdump', '--uuid', str(path))
        uuid = re.search(r'UUID: ([A-F0-9-]+) \(' + arch + r'\)', uuids)
        if uuid is None:
            raise ValueError(f'{path.name} lacks {arch}')
        # Signing and install-name relocation change load commands, not executable code.
        dump = output('/usr/bin/otool', '-arch', arch, '-s', '__TEXT', '__text', str(path))
        code = ''.join(''.join(line.split()[1:]) for line in dump.splitlines()
                       if re.match(r'^[0-9a-fA-F]{8,16}\s', line))
        if not code:
            raise ValueError(f'{path.name}: no executable code for {arch}')
        libraries = output('/usr/bin/otool', '-arch', arch, '-L', str(path)).splitlines()[1:]
        dependencies = [line.strip().split(' (')[0] for line in libraries if line.startswith('\t')]
        result[arch] = {'uuid': uuid.group(1),
                        'text_sha256': hashlib.sha256(bytes.fromhex(code)).hexdigest(),
                        'libraries': sorted(Path(name).name for name in dependencies)}
    return result


def describe(root, bundle=False):
    root = Path(root)
    if bundle:
        framework = root / 'Contents/Frameworks/Python.framework/Versions/3.12'
        extensions = root / 'Contents/Resources/lib/python3.12/lib-dynload'
        libraries = root / 'Contents/Frameworks'
    else:
        framework = root
        extensions = root / 'lib/python3.12/lib-dynload'
        libraries = root / 'lib'
    def extension(name):
        matches = list(extensions.glob(name + '*.so'))
        if len(matches) != 1:
            raise ValueError(f'Expected one {name} extension, found {len(matches)}')
        return matches[0]
    paths = {'Python': framework / 'Python',
             'PythonLauncher': (root / 'Contents/MacOS/python' if bundle else framework / 'Resources/Python.app/Contents/MacOS/Python'),
             '_ssl': extension('_ssl'),
             '_hashlib': extension('_hashlib'),
             'libssl.3.dylib': libraries / 'libssl.3.dylib',
             'libcrypto.3.dylib': libraries / 'libcrypto.3.dylib'}
    for path in paths.values():
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'Missing or external pinned runtime file: {path.name}')
    result = {name: fingerprint(path) for name, path in paths.items()}
    if bundle:
        for path in paths.values():
            for arch in ('arm64', 'x86_64'):
                lines = output('/usr/bin/otool', '-arch', arch, '-L', str(path)).splitlines()[1:]
                ids = set(line.strip() for line in output('/usr/bin/otool', '-arch', arch, '-D', str(path)).splitlines()
                          if line.strip() and not line.endswith(':'))
                for line in lines:
                    dependency = line.strip().split(' (')[0]
                    if not line.startswith('\t') or dependency in ids or dependency.startswith(('/usr/lib/', '/System/')):
                        continue
                    if dependency.startswith('@loader_path/'):
                        target = path.parent / dependency[len('@loader_path/'):]
                    elif dependency.startswith('@executable_path/'):
                        target = root / 'Contents/MacOS' / dependency[len('@executable_path/'):]
                    else:
                        raise ValueError(f'{path.name}: unresolved library: {dependency}')
                    if not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
                        raise ValueError(f'{path.name}: missing or external library: {dependency}')
                    expected = paths.get(Path(dependency).name)
                    if expected is None or target.resolve() != expected.resolve():
                        raise ValueError(f'{path.name}: unpinned dependency target: {dependency}')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--builder', action='store_true')
    args = parser.parse_args()
    if bool(args.bundle) == args.builder:
        parser.error('choose --builder or --bundle APP')
    if args.builder and sys.version_info[:3] != (3, 12, 10):
        raise ValueError(f'Python.org 3.12.10 universal2 required; found {sys.version}')
    pin = json.loads(PIN.read_text())
    actual = describe(args.bundle or sys.base_prefix, bundle=bool(args.bundle))
    if actual != pin['binaries']:
        changed = [name for name in pin['binaries'] if actual.get(name) != pin['binaries'][name]]
        raise ValueError('Pinned Python.org runtime differs: ' + ', '.join(changed))
    print('Pinned Python.org 3.12.10 / dynamic OpenSSL: PASS (arm64, x86_64)')


if __name__ == '__main__':
    main()
