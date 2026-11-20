import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / 'openbsd' / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = load_module('status_test_core', 'controller.py')
status = load_module('status_test_report', 'controller_status.py')


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.cfg = core.validate_config({
            'repo': 'example/agent',
            'auth': {'app_id': 1, 'installation_id': 2,
                     'private_key_file': '/etc/ci-runner/app.pem'},
        })
        self.api = core.GitHub(self.cfg)

    def test_completed_job_is_reported_without_dispatch_or_extra_api_fields(self):
        def response(method, path, body=None):
            self.assertEqual(method, 'GET')
            self.assertIsNone(body)
            if '/actions/runners?' in path:
                return {'runners': [{'id': 3, 'name': 'ci-vmm-test', 'status': 'offline',
                                     'busy': False, 'token': 'must-not-export'}]}
            if path.endswith('/actions/jobs/136197174717'):
                return {'id': 136197174717, 'status': 'completed', 'conclusion': 'success',
                        'runner_name': 'ci-vmm-test', 'labels': self.cfg['labels'],
                        'extra': 'must-not-export'}
            if '/actions/runs?' in path:
                return {'workflow_runs': [{'id': 4, 'status': 'completed'}]}
            self.fail('Unexpected API request: ' + path)

        with patch.object(self.api, 'request', side_effect=response):
            report = status.collect_status(core, self.api, self.cfg, 136197174717)
        self.assertEqual(report['requested_job']['conclusion'], 'success')
        self.assertFalse(report['requested_job']['queued_and_labels_compatible'])
        self.assertEqual(report['active_runs'], [])
        self.assertNotIn('must-not-export', json.dumps(report))
        self.assertNotIn('auth', report)

    def test_waiting_run_and_incompatible_job_remain_visible(self):
        run = {'id': 4, 'status': 'waiting', 'path': '.github/workflows/smoke.yml',
               'head_repository': {'full_name': self.cfg['repo']}, 'event': 'workflow_dispatch'}

        def response(method, path, body=None):
            self.assertEqual(method, 'GET')
            if '/actions/runners?' in path:
                return {'runners': []}
            if '/actions/runs/4/jobs?' in path:
                return {'jobs': [{'id': 5, 'status': 'queued', 'labels': ['ubuntu-latest']}]}
            if '/actions/runs?' in path:
                return {'workflow_runs': [run]}
            self.fail('Unexpected API request: ' + path)

        with patch.object(self.api, 'request', side_effect=response):
            report = status.collect_status(core, self.api, self.cfg)
        active = report['active_runs'][0]
        self.assertEqual(active['status'], 'waiting')
        self.assertTrue(active['matches_start_policy'])
        self.assertFalse(active['jobs'][0]['queued_and_labels_compatible'])

    def test_invalid_run_list_is_an_error_instead_of_an_empty_queue(self):
        with patch.object(self.api, 'pages', return_value=iter([])), \
             patch.object(self.api, 'request', return_value={'workflow_runs': None}):
            with self.assertRaisesRegex(RuntimeError, 'Invalid workflow runs response'):
                status.collect_status(core, self.api, self.cfg)


if __name__ == '__main__':
    unittest.main()
