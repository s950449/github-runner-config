#!/usr/local/bin/python3
"""Single-capacity GitHub App/JIT controller. No third-party Python packages."""
import argparse
import base64
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path('/home/vmm-ci')
HEADER = 'ID PID VCPUS MAXMEM CURMEM TTY OWNER STATE NAME'.split()
STOP = False


def positive(value):
    return type(value) is int and value > 0


def validate_config(data):
    cfg = dict(data)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', cfg.get('repo', '')):
        raise ValueError('Invalid repository')
    auth = cfg.get('auth', {})
    if not positive(auth.get('app_id')) or not positive(auth.get('installation_id')):
        raise ValueError('Numeric GitHub App and installation IDs required')
    if not isinstance(auth.get('private_key_file'), str) or not auth['private_key_file'].startswith('/'):
        raise ValueError('Absolute private key path required')
    cfg.setdefault('workflows', ['.github/workflows/smoke.yml'])
    if not isinstance(cfg['workflows'], list) or not cfg['workflows'] or any(
        not isinstance(p, str) or not re.fullmatch(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml', p)
        for p in cfg['workflows']
    ):
        raise ValueError('Explicit workflow path allowlist required')
    cfg.setdefault('labels', ['self-hosted', 'Linux', 'X64', 'openbsd-vmm'])
    if (not isinstance(cfg['labels'], list) or
        any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', x) for x in cfg['labels']) or
        not {'self-hosted', 'Linux', 'X64', 'openbsd-vmm'} <= set(cfg['labels'])):
        raise ValueError('Invalid runner labels')
    for key, default, low, high in [('poll_seconds', 60, 60, 900),
                                     ('idle_seconds', 300, 120, 1800),
                                     ('runner_group_id', 1, 1, 1000000)]:
        cfg.setdefault(key, default)
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('Invalid ' + key)
    return cfg


def eligible_run(run, cfg):
    return (isinstance(run, dict) and positive(run.get('id')) and
            isinstance(run.get('head_repository'), dict) and
            isinstance(run['head_repository'].get('full_name'), str) and
            run['head_repository'].get('full_name', '').lower() == cfg['repo'].lower() and
            run.get('event') in {'push', 'workflow_dispatch', 'pull_request'} and
            isinstance(run.get('path'), str) and run['path'].split('@', 1)[0] in cfg['workflows'])


def eligible_job(job, cfg):
    labels = job.get('labels', []) if isinstance(job, dict) else []
    return (isinstance(job, dict) and positive(job.get('id')) and job.get('status') == 'queued' and
            isinstance(labels, list) and all(isinstance(x, str) for x in labels) and
            {'self-hosted', 'openbsd-vmm'} <= set(labels) <= set(cfg['labels']))


def guest_seed(jit):
    # JIT is a one-job runner capability. The App key and API token never enter here.
    wrapper = '''#!/bin/bash
set -eu
IFS= read -r jit < /home/gha-runner/.ci-jit
rm /home/gha-runner/.ci-jit
exec ./run.sh --jitconfig "$jit"
'''
    unit = '''[Unit]
Description=Single-job GitHub JIT runner
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=gha-runner
Group=gha-runner
WorkingDirectory=/home/gha-runner/actions-runner
ExecStart=/bin/bash /usr/local/libexec/ci-run-jit
ExecStopPost=+/usr/bin/systemctl --no-block poweroff
Restart=no
RuntimeMaxSec=3300
TimeoutStopSec=30
KillMode=control-group
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/gha-runner
UMask=0077
'''
    return {'disable_root': True, 'ssh_pwauth': False,
            'users': [{'name': 'gha-runner', 'lock_passwd': True, 'shell': '/bin/bash',
                       'groups': '', 'sudo': None}],
            'write_files': [
                {'path': '/home/gha-runner/.ci-jit', 'owner': 'gha-runner:gha-runner',
                 'permissions': '0600', 'content': jit + '\n', 'defer': True},
                {'path': '/usr/local/libexec/ci-run-jit', 'permissions': '0755', 'content': wrapper},
                {'path': '/etc/systemd/system/ci-jit.service', 'permissions': '0644', 'content': unit}],
            'runcmd': [['/bin/sh', '-ec', 'cloud-init schema --system; systemctl daemon-reload; systemctl start ci-jit.service']]}


def read_private(path, secret=True):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        forbidden = 0o077 if secret else 0o022
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & forbidden:
            raise ValueError('Unsafe ownership or permissions on configuration/key')
        text = stream.read(1024 * 1024 + 1)
        if len(text) > 1024 * 1024:
            raise ValueError('Configuration/key too large')
        return text


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__('GitHub API HTTP ' + str(status))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(method, path, token, body=None):
    if not path.startswith('/') or path.startswith('//'):
        raise ValueError('Invalid API path')
    request = urllib.request.Request('https://api.github.com' + path,
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'openbsd-ci-controller',
                 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise RuntimeError('Oversized API response')
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raise APIError(error.code) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('GitHub API network/TLS failure') from None


class GitHub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = '/repos/' + cfg['repo']
        self.cached_token = None
        self.token_deadline = 0

    def token(self):
        if self.cached_token and time.time() < self.token_deadline:
            return self.cached_token
        auth = self.cfg['auth']
        read_private(auth['private_key_file'])  # enforce root-only regular file
        encode = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=')
        now = int(time.time())
        claim = {'iat': now - 60, 'exp': now + 540, 'iss': str(auth['app_id'])}
        signed = encode(b'{"alg":"RS256","typ":"JWT"}') + b'.' + encode(json.dumps(claim).encode())
        signature = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-sign', auth['private_key_file']],
                                   input=signed, capture_output=True, check=True).stdout
        jwt = (signed + b'.' + encode(signature)).decode()
        result = http('POST', '/app/installations/' + str(auth['installation_id']) + '/access_tokens', jwt,
                      {'repositories': [self.cfg['repo'].split('/')[1]],
                       'permissions': {'actions': 'read', 'administration': 'write'}})
        if not isinstance(result, dict) or not isinstance(result.get('token'), str):
            raise RuntimeError('Invalid installation token response')
        self.cached_token = result['token']
        self.token_deadline = time.time() + 2700
        return self.cached_token

    def request(self, method, path, body=None):
        try:
            return http(method, path, self.token(), body)
        except APIError as error:
            if error.status != 401:
                raise
            self.cached_token = None
            return http(method, path, self.token(), body)

    def pages(self, path, field):
        for page in range(1, 6):
            result = self.request('GET', path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            values = result.get(field) if isinstance(result, dict) else None
            if not isinstance(values, list):
                raise RuntimeError('Invalid paginated response')
            yield from values
            if len(values) < 100:
                return
        raise RuntimeError('Pagination safety limit reached')

    def queued(self):
        for status in ['queued', 'in_progress']:
            for run in self.pages(self.base + '/actions/runs?status=' + status, 'workflow_runs'):
                if eligible_run(run, self.cfg):
                    for job in self.pages(self.base + '/actions/runs/' + str(run['id']) + '/jobs', 'jobs'):
                        if eligible_job(job, self.cfg):
                            return job['id']
        return None

    def check(self):
        self.request('GET', self.base)
        list(self.pages(self.base + '/actions/runners', 'runners'))
        paths = {w.get('path') for w in self.pages(self.base + '/actions/workflows', 'workflows')
                 if isinstance(w, dict) and isinstance(w.get('path'), str)}
        missing = set(self.cfg['workflows']) - paths
        if missing:
            raise RuntimeError('Workflow allowlist does not match repository workflows. Missing: ' +
                               json.dumps(sorted(missing)) + '; Available: ' + json.dumps(sorted(paths)))
        self.queued()

    def jit(self, name):
        result = self.request('POST', self.base + '/actions/runners/generate-jitconfig',
            {'name': name, 'runner_group_id': self.cfg['runner_group_id'],
             'labels': self.cfg['labels'], 'work_folder': '_work'})
        if (not isinstance(result, dict) or not isinstance(result.get('runner'), dict) or
            not positive(result['runner'].get('id')) or result['runner'].get('name') != name or
            not isinstance(result.get('encoded_jit_config'), str) or
            len(result['encoded_jit_config']) > 512000):
            raise RuntimeError('Invalid JIT configuration response')
        return result['runner']['id'], result['encoded_jit_config']

    def remove(self, state):
        runner_id = state.get('runner_id')
        if runner_id is None:
            # Covers a controller crash between JIT creation and saving its ID.
            for runner in self.pages(self.base + '/actions/runners', 'runners'):
                if isinstance(runner, dict) and runner.get('name') == state['runner_name'] and positive(runner.get('id')):
                    runner_id = runner['id']
                    break
        if runner_id is not None:
            try:
                self.request('DELETE', self.base + '/actions/runners/' + str(runner_id))
            except APIError as error:
                if error.status != 404:
                    raise


def parse_vm_status(code, output, error):
    lines = output.splitlines()
    if code not in [0, 1] or error.strip() or not lines or lines[0].split() != HEADER:
        raise RuntimeError('Cannot confirm vmd state')
    if code == 1 and len(lines) != 1:
        raise RuntimeError('Unexpected vmctl error response')
    rows = [line.split() for line in lines[1:] if line.strip()]
    if any(len(row) != len(HEADER) for row in rows):
        raise RuntimeError('Invalid vmctl state row')
    return rows


def command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=45)


def running_vms():
    result = command(['/usr/sbin/vmctl', 'status', '-r'])
    return parse_vm_status(result.returncode, result.stdout, result.stderr)


def save_state(state):
    fd, temporary = tempfile.mkstemp(dir=ROOT, prefix='.state-')
    with os.fdopen(fd, 'w') as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, ROOT / 'auto-state.json')


def recover(api, just_finished=False):
    path = ROOT / 'auto-state.json'
    if not path.exists():
        return
    state = json.loads(read_private(path))
    session = state.get('session', '')
    if not re.fullmatch('[a-f0-9]{32}', session) or state.get('runner_name') != 'ci-vmm-' + session:
        raise RuntimeError('Invalid managed session state')
    lock = ROOT / 'runtime/lock'
    owned = (lock / 'session').exists() and read_private(lock / 'session').strip() == session
    if owned:
        if not just_finished and time.time() - state.get('created_at', time.time()) < 60:
            raise RuntimeError('Recent interrupted session; deferring recovery')
        pid_file = lock / 'manager_pid'
        if pid_file.exists():
            pid_text = read_private(pid_file).strip()
            if not pid_text.isdecimal():
                raise RuntimeError('Invalid manager PID; preserving session')
            process = command(['/bin/ps', '-p', pid_text, '-o', 'command='])
            if process.returncode == 0 and '/usr/local/sbin/ci-run-vm' in process.stdout and session in process.stdout:
                raise RuntimeError('Previous session manager still active; waiting for it to finish')
        rows = running_vms()
        active = any(row[-1] == 'ci-runner' for row in rows)
        if active:
            if not (lock / 'launching').exists():
                raise RuntimeError('Unowned running VM; refusing recovery')
            result = command(['/usr/sbin/vmctl', 'stop', '-fw', 'ci-runner'])
            if result.returncode or any(row[-1] == 'ci-runner' for row in running_vms()):
                raise RuntimeError('VM stop failed; preserving session')
        if command(['/usr/sbin/vmctl', 'reload']).returncode:
            raise RuntimeError('VM reload failed; preserving session')
        for name in ['job.qcow2', 'job-seed.iso']:
            (ROOT / 'runtime' / name).unlink(missing_ok=True)
        for name in ['user-data', 'network-config', 'meta-data']:
            (lock / 'seed' / name).unlink(missing_ok=True)
        if (lock / 'seed').exists():
            (lock / 'seed').rmdir()
        for name in ['status', 'status.err', 'launching', 'session', 'manager_pid']:
            (lock / name).unlink(missing_ok=True)
        lock.rmdir()
    api.remove(state)  # VM must be gone before deleting a live runner registration.
    path.unlink()
    logging.info('recovered session %s', session)


def dispatch(api, job_id):
    lock = ROOT / 'runtime/lock'
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        return
    session = uuid.uuid4().hex
    with os.fdopen(os.open(lock / 'session', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as marker:
        marker.write(session + '\n')
    state = {'session': session, 'runner_name': 'ci-vmm-' + session,
             'runner_id': None, 'job_id': job_id, 'created_at': time.time()}
    save_state(state)
    child = None
    used = False
    try:
        state['runner_id'], jit = api.jit(state['runner_name'])
        save_state(state)
        seed = ('#cloud-config\n' + json.dumps(guest_seed(jit))).encode()
        child = subprocess.Popen(['/usr/local/sbin/ci-run-vm', 'github', '--auto', session],
                                 stdin=subprocess.PIPE,
                                 env={'PATH': '/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin',
                                      'HOME': '/root', 'LANG': 'C'})
        child.stdin.write(seed)
        child.stdin.close()
        logging.info('started runner_id=%s candidate_job=%s session=%s', state['runner_id'], job_id, session)
        started, last_check, busy = time.monotonic(), 0, False
        while child.poll() is None and not STOP:
            now = time.monotonic()
            if now - started > 3660:
                break
            if now - last_check >= 30:
                last_check = now
                try:
                    runner = api.request('GET', api.base + '/actions/runners/' + str(state['runner_id']))
                    if not isinstance(runner, dict) or type(runner.get('busy')) is not bool:
                        raise RuntimeError('Invalid runner status')
                    busy = busy or runner['busy']
                    used = used or runner['busy']
                    if not busy and now - started >= api.cfg['idle_seconds']:
                        logging.warning('unused runner exceeded boot/idle allowance')
                        break
                except APIError as error:
                    if error.status == 404:
                        used = True
                        break  # ephemeral runner finished/was removed
                    logging.warning('runner status check: %s', error)
                except RuntimeError as error:
                    logging.warning('runner status check: %s', error)
            time.sleep(2)
        if not used:
            # Fast jobs can finish between 30-second status samples.
            try:
                api.request('GET', api.base + '/actions/runners/' + str(state['runner_id']))
            except APIError as error:
                if error.status == 404:
                    used = True
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        recover(api, just_finished=True)
    return used


def main():
    global STOP
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--validate', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Controller must run on the OpenBSD host as root')
    os.umask(0o077)
    cfg = validate_config(json.loads(read_private(args.config, secret=False)))
    read_private(cfg['auth']['private_key_file'])
    if args.validate:
        return
    api = GitHub(cfg)
    if args.check:
        api.check()
        logging.info('GitHub App API read checks passed; no runner or VM created')
        return
    with open(ROOT / 'controller.lock', 'a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def stop(signum, frame):
            global STOP
            STOP = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        backoff = cfg['poll_seconds']
        while not STOP:
            try:
                recover(api)
                if (not (ROOT / 'build.lock').exists() and not (ROOT / 'runtime/lock').exists()
                        and not (ROOT / 'runtime/job.qcow2').exists() and not running_vms()):
                    job = api.queued()
                    if job is not None:
                        used = dispatch(api, job)
                        backoff = cfg['poll_seconds'] if used else min(900, backoff * 2)
                        if not used:
                            logging.warning('no job observed; retry backoff=%s seconds', backoff)
                    else:
                        backoff = cfg['poll_seconds']
            except Exception as error:
                # Do not print API response bodies, seed, credentials, or traceback locals.
                logging.error('controller operation failed: %s', type(error).__name__ + ': ' + str(error))
                backoff = min(900, backoff * 2)
            if args.once:
                return
            for _ in range(backoff):
                if STOP:
                    break
                time.sleep(1)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        main()
    except Exception as error:
        logging.error('startup failed: %s', type(error).__name__ + ': ' + str(error))
        raise SystemExit(1)
