#!/usr/bin/env python3
"""Check Git's staged blobs or history without printing matched secret values."""
import argparse
import ipaddress
from pathlib import PurePosixPath
import re
import subprocess
import sys


PATTERNS = {
    'private key': re.compile(rb'-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----'),
    'age identity': re.compile(rb'AGE-SECRET-KEY-1[0-9A-Z]{40,}'),
    'GitHub token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'AWS access key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'JWT': re.compile(rb'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}'),
}
PRIVATE_NETWORKS = [ipaddress.ip_network(network) for network in
                    ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']]
IPV4 = re.compile(rb'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')


def findings(filename, data):
    path = PurePosixPath(filename)
    name = path.name.lower()
    issues = []
    if (name == '.env' or name.startswith('.env.') or
        path.suffix.lower() in {'.env', '.pem', '.key', '.p8', '.p12', '.pfx', '.agekey',
                                '.qcow2', '.img', '.iso', '.raw'} or
        name in {'id_rsa', 'id_ed25519', 'id_ecdsa', 'keys.txt', '.ci-jit', '.runner'} or
        name.startswith('.credentials') or
        set(path.parts) & {'.local', '.secrets', 'secrets', '.ssh', '.aws', 'runtime', '_diag', '_work'}):
        issues.append('sensitive or runtime file must not be tracked')
    if ('inventory' in path.parts and
        set(path.parts) & {'group_vars', 'host_vars'} and
        not name.endswith(('.sops.yml', '.sops.yaml', '.example.yml', '.example.yaml'))):
        issues.append('inventory variables must be SOPS encrypted or explicit examples')
    if name != '.sops.yaml' and name.endswith(('.sops.yml', '.sops.yaml')) and (
            b'\nsops:\n' not in data or b'ENC[AES256_GCM,' not in data):
        issues.append('SOPS inventory is missing encryption metadata or ciphertext')
    for kind, pattern in PATTERNS.items():
        if pattern.search(data):
            issues.append(kind)
    for match in IPV4.finditer(data):
        # Network policy CIDRs are public configuration; individual LAN hosts are private.
        if data[match.end():match.end() + 1] == b'/':
            continue
        try:
            address = ipaddress.ip_address(match.group().decode('ascii'))
        except ValueError:
            continue
        if any(address in network for network in PRIVATE_NETWORKS):
            issues.append('unencrypted private host IP')
            break
    return issues


def git(*args):
    return subprocess.run(['git', *args], capture_output=True, check=True).stdout


def staged_blobs():
    for entry in git('ls-files', '--stage', '-z').split(b'\0'):
        if not entry:
            continue
        metadata, filename = entry.split(b'\t', 1)
        mode, oid, stage = metadata.split()
        if stage != b'0' or mode not in {b'100644', b'100755'}:
            raise RuntimeError('Unmerged or non-regular staged file; review before committing')
        yield filename.decode('utf-8'), git('cat-file', 'blob', oid.decode('ascii'))


def history_blobs():
    seen = set()
    for commit in git('rev-list', '--all').splitlines():
        for entry in git('ls-tree', '-r', '-z', commit.decode('ascii')).split(b'\0'):
            if not entry:
                continue
            metadata, filename = entry.split(b'\t', 1)
            mode, kind, oid = metadata.split()
            if kind != b'blob' or (filename, oid) in seen:
                continue
            seen.add((filename, oid))
            yield filename.decode('utf-8'), git('cat-file', 'blob', oid.decode('ascii'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='scan every committed file version')
    args = parser.parse_args()
    count = 0
    failed = False
    for filename, data in history_blobs() if args.history else staged_blobs():
        count += 1
        for issue in findings(filename, data):
            print(f'{filename}: {issue}', file=sys.stderr)
            failed = True
    if failed:
        return 1
    print(f'No configured secret patterns or prohibited files found in {count} Git blobs.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
