import importlib.util
from pathlib import Path
import sys
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('ci_controller', Path(__file__).resolve().parents[1] / 'openbsd/controller.py')
controller = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = controller
SPEC.loader.exec_module(controller)


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.config = controller.validate_config({
            'repo': 'example/agent', 'auth': {'app_id': 1, 'installation_id': 2,
                                          'private_key_file': '/etc/ci-runner/app.pem'},
            'workflows': ['.github/workflows/runner-smoke.yml'],
        })
        self.run = {'id': 1, 'path': '.github/workflows/runner-smoke.yml',
                    'head_repository': {'full_name': 'example/agent'}, 'event': 'workflow_dispatch'}

    def test_only_compatible_queued_jobs_are_eligible(self):
        job = {'id': 2, 'status': 'queued', 'labels': ['self-hosted', 'Linux', 'X64', 'openbsd-vmm']}
        self.assertTrue(controller.eligible_job(job, self.config))
        for changes in [{'status': 'in_progress'}, {'labels': ['ubuntu-latest']},
                        {'labels': ['self-hosted', 'openbsd-vmm', 'gpu']}, {'id': True}]:
            self.assertFalse(controller.eligible_job({**job, **changes}, self.config))

    def test_forks_other_workflows_and_privileged_events_are_rejected(self):
        self.assertTrue(controller.eligible_run(self.run, self.config))
        for changes in [{'head_repository': {'full_name': 'stranger/fork'}},
                        {'path': '.github/workflows/deploy.yml'}, {'event': 'pull_request_target'},
                        {'head_repository': None}]:
            self.assertFalse(controller.eligible_run({**self.run, **changes}, self.config))

    def test_untrusted_strings_never_become_shell_commands(self):
        with self.assertRaises(ValueError):
            controller.validate_config({**self.config, 'repo': 'owner/repo;reboot'})

    def test_jit_is_created_only_for_the_expected_repo_and_labels(self):
        api = controller.GitHub(self.config)
        with patch.object(api, 'request', return_value={
            'runner': {'id': 7, 'name': 'ci-vmm-test'}, 'encoded_jit_config': 'one-job-capability'
        }) as request:
            self.assertEqual(api.jit('ci-vmm-test'), (7, 'one-job-capability'))
            method, path, body = request.call_args.args
        self.assertEqual(method, 'POST')
        self.assertEqual(path, '/repos/example/agent/actions/runners/generate-jitconfig')
        self.assertEqual(body['runner_group_id'], 1)
        self.assertEqual(body['labels'], self.config['labels'])
        self.assertEqual(body['work_folder'], '_work')

    def test_api_capability_never_enters_guest_seed(self):
        seed = controller.guest_seed('one-time-config')
        user = seed['users'][0]
        self.assertEqual(user['name'], 'gha-runner')
        self.assertIsNone(user['sudo'])
        self.assertEqual(user['groups'], '')
        self.assertNotIn('ssh_authorized_keys', user)
        unit = next(f['content'] for f in seed['write_files'] if f['path'].endswith('.service'))
        self.assertIn('User=gha-runner', unit)
        self.assertIn('ExecStopPost=+/usr/bin/systemctl --no-block poweroff', unit)
        self.assertNotIn('Administration', str(seed))
        self.assertEqual(seed['write_files'][0]['content'], 'one-time-config\n')

    def test_empty_vm_status_is_accepted_but_connection_errors_are_not(self):
        header = ' ID PID VCPUS MAXMEM CURMEM TTY OWNER STATE NAME\n'
        self.assertEqual(controller.parse_vm_status(1, header, ''), [])
        for output, error in [('', 'Connection refused'), ('wrong response', '')]:
            with self.assertRaises(RuntimeError):
                controller.parse_vm_status(1, output, error)

    def test_downstream_queued_job_in_running_workflow_is_found(self):
        api = controller.GitHub(self.config)
        paths = []
        def response(method, path, body=None):
            paths.append(path)
            if 'status=queued' in path:
                return {'workflow_runs': []}
            if 'status=in_progress' in path:
                return {'workflow_runs': [self.run]}
            return {'jobs': [{'id': 4, 'status': 'queued', 'labels': ['self-hosted', 'openbsd-vmm']}]}
        api.request = response
        self.assertEqual(api.queued(), 4)
        self.assertTrue(any('status=in_progress' in path for path in paths))

    def test_default_preflight_matches_smoke_yml_without_creating_resources(self):
        cfg = controller.validate_config({key: value for key, value in self.config.items() if key != 'workflows'})
        self.assertEqual(cfg['workflows'], ['.github/workflows/smoke.yml'])
        api = controller.GitHub(cfg)
        def response(method, path, body=None):
            self.assertEqual(method, 'GET')
            if '/workflows?' in path:
                return {'workflows': [{'path': '.github/workflows/smoke.yml'}]}
            if '/runners?' in path:
                return {'runners': []}
            if '/runs?' in path:
                return {'workflow_runs': []}
            return {}
        with patch.object(api, 'request', side_effect=response):
            api.check()

    def test_preflight_reports_actual_paths_without_widening_policy(self):
        api = controller.GitHub(self.config)
        def response(method, path, body=None):
            self.assertEqual(method, 'GET')
            if '/workflows?' in path:
                return {'workflows': [{'path': '.github/workflows/smoke.yml'}]}
            return {'runners': []}
        with patch.object(api, 'request', side_effect=response):
            with self.assertRaisesRegex(RuntimeError, 'Available:.*smoke.yml'):
                api.check()
        self.assertEqual(self.config['workflows'], ['.github/workflows/runner-smoke.yml'])


class RecoveryTest(unittest.TestCase):
    def setup_session(self, root, owned=True, launching=True):
        (root / 'runtime/lock/seed').mkdir(parents=True)
        session = 'a' * 32
        state = {'session': session, 'runner_name': 'ci-vmm-' + session,
                 'runner_id': 99, 'created_at': time.time() - 120}
        for path, value in [('auto-state.json', json.dumps(state)),
                            ('runtime/lock/session', session if owned else 'b' * 32),
                            ('runtime/job.qcow2', 'disk')]:
            destination = root / path
            destination.write_text(value)
            destination.chmod(0o600)
        if launching:
            (root / 'runtime/lock/launching').touch()
        return state

    def test_foreign_session_is_never_stopped_or_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self.setup_session(root, owned=False)
            removed = []
            api = type('API', (), {'remove': lambda self, value: removed.append(value)})()
            with patch.object(controller, 'ROOT', root), patch.object(controller, 'command') as command:
                controller.recover(api)
                command.assert_not_called()
            self.assertTrue((root / 'runtime/job.qcow2').exists())
            self.assertTrue((root / 'runtime/lock/session').exists())
            self.assertEqual(removed, [state])

    def test_vm_stop_failure_preserves_disk_state_and_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_session(root)
            api = type('API', (), {'remove': lambda self, value: self.fail('must not delete registration')})()
            with patch.object(controller, 'ROOT', root), \
                 patch.object(controller, 'running_vms', return_value=[['ci-runner']]), \
                 patch.object(controller, 'command', return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaises(RuntimeError):
                    controller.recover(api)
            self.assertTrue((root / 'runtime/job.qcow2').exists())
            self.assertTrue((root / 'auto-state.json').exists())

    def test_unlaunched_session_cannot_stop_an_existing_vm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_session(root, launching=False)
            with patch.object(controller, 'ROOT', root), \
                 patch.object(controller, 'running_vms', return_value=[['ci-runner']]), \
                 patch.object(controller, 'command') as command:
                with self.assertRaises(RuntimeError):
                    controller.recover(None)
                command.assert_not_called()

    def test_successful_recovery_stops_vm_before_disk_and_api_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self.setup_session(root)
            order = []
            def run(args):
                order.append(args[1])
                return subprocess.CompletedProcess(args, 0)
            def remove(api, value):
                self.assertFalse((root / 'runtime/job.qcow2').exists())
                self.assertTrue((root / 'auto-state.json').exists())
                self.assertEqual(value, state)
                order.append('api-remove')
            api = type('API', (), {'remove': remove})()
            with patch.object(controller, 'ROOT', root), \
                 patch.object(controller, 'running_vms', side_effect=[[['ci-runner']], []]), \
                 patch.object(controller, 'command', side_effect=run):
                controller.recover(api)
            self.assertEqual(order, ['stop', 'reload', 'api-remove'])
            self.assertFalse((root / 'auto-state.json').exists())
            self.assertFalse((root / 'runtime/lock').exists())

    def test_still_active_previous_manager_is_not_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self.setup_session(root)
            pid = root / 'runtime/lock/manager_pid'
            pid.write_text('12345')
            pid.chmod(0o600)
            result = subprocess.CompletedProcess([], 0,
                '/bin/sh /usr/local/sbin/ci-run-vm github --auto ' + state['session'])
            with patch.object(controller, 'ROOT', root), \
                 patch.object(controller, 'command', return_value=result) as command, \
                 patch.object(controller, 'running_vms') as running:
                with self.assertRaisesRegex(RuntimeError, 'still active'):
                    controller.recover(None)
                running.assert_not_called()
                self.assertEqual(command.call_args.args[0][0], '/bin/ps')
            self.assertTrue((root / 'runtime/job.qcow2').exists())


class PrivateFileTest(unittest.TestCase):
    def test_public_or_symlinked_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'
            key.write_text('test-only-key')
            key.chmod(0o644)
            with self.assertRaises(ValueError):
                controller.read_private(key)
            key.chmod(0o600)
            link = Path(directory) / 'link'
            link.symlink_to(key)
            with self.assertRaises(OSError):
                controller.read_private(link)


class AppAuthTest(unittest.TestCase):
    def test_real_rsa_jwt_is_signed_and_installation_grant_is_repo_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key.pem'
            public = Path(directory) / 'public.pem'
            subprocess.run(['/usr/bin/openssl', 'genrsa', '-out', str(key), '2048'],
                           capture_output=True, check=True)
            key.chmod(0o600)
            subprocess.run(['/usr/bin/openssl', 'rsa', '-in', str(key), '-pubout', '-out', str(public)],
                           capture_output=True, check=True)
            cfg = controller.validate_config({'repo': 'example/agent', 'auth': {
                'app_id': 1, 'installation_id': 2, 'private_key_file': str(key)}})
            api = controller.GitHub(cfg)
            with patch.object(controller, 'http', return_value={'token': 'host-only-api-capability'}) as transport:
                self.assertEqual(api.token(), 'host-only-api-capability')
                self.assertEqual(api.token(), 'host-only-api-capability')
                transport.assert_called_once()
                method, path, jwt, body = transport.call_args.args
            self.assertEqual(method, 'POST')
            self.assertEqual(path, '/app/installations/2/access_tokens')
            self.assertEqual(body, {'repositories': ['agent'],
                                   'permissions': {'actions': 'read', 'administration': 'write'}})
            header, claims, signature = jwt.split('.')
            decode = lambda s: controller.base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
            self.assertEqual(json.loads(decode(claims))['iss'], '1')
            sig_path = Path(directory) / 'signature'
            sig_path.write_bytes(decode(signature))
            result = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-verify', str(public),
                                     '-signature', str(sig_path)], input=(header + '.' + claims).encode(),
                                    capture_output=True)
            self.assertEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
