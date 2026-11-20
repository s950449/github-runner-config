import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('check_secrets', ROOT / 'scripts/check-secrets.py')
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)


class RepositorySecurityTest(unittest.TestCase):
    def test_sensitive_filenames_are_rejected_even_if_force_added(self):
        for filename in ['.env.production', 'app.pem', 'identity.agekey', 'secrets/config.yml',
                         '.local/backup.yml', 'runtime/job.iso', '.ssh/id_ed25519']:
            with self.subTest(filename=filename):
                self.assertTrue(scanner.findings(filename, b'test'))

    def test_secret_patterns_cannot_hide_in_source_files(self):
        values = [b'-----BEGIN ' + b'RSA PRIVATE KEY-----',
                  b'AGE-SECRET-' + b'KEY-1' + b'A' * 58,
                  b'ghp_' + b'a' * 36, b'AKIA' + b'A' * 16,
                  b'eyJ' + b'a' * 20 + b'.eyJ' + b'b' * 20 + b'.' + b'c' * 32]
        for value in values:
            self.assertTrue(scanner.findings('config.py', value))

    def test_private_host_ip_is_rejected_but_policy_cidrs_are_allowed(self):
        host = '.'.join(['192', '168', '7', '25']).encode()
        self.assertIn('unencrypted private host IP', scanner.findings('README.md', host))
        self.assertEqual(scanner.findings('pf.conf', b'block from 192.168.0.0/16'), [])
        self.assertEqual(scanner.findings('README.md', b'ci-admin@192.0.2.10'), [])

    def test_renaming_plaintext_as_sops_does_not_bypass_check(self):
        path = 'ansible/inventory/host_vars/homelab.sops.yml'
        self.assertTrue(scanner.findings(path, b'ansible_host: example.org\n'))
        self.assertEqual(scanner.findings(path,
            b'ansible_host: ENC[AES256_GCM,data:ciphertext]\nsops:\n  version: test\n'), [])

    def test_plain_inventory_vars_are_rejected(self):
        self.assertTrue(scanner.findings('ansible/inventory/host_vars/homelab.yml', b'{}'))

    def test_public_sops_recipient_config_is_allowed(self):
        self.assertEqual(scanner.findings('.sops.yaml', b'creation_rules: []\n'), [])


if __name__ == '__main__':
    unittest.main()
