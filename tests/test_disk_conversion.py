"""Check offline conversion ordering and failure containment without real VM tools."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'openbsd/prepare-build-disk.sh'


class DiskConversionTest(unittest.TestCase):
    def execute(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'base').mkdir()
            binaries = root / 'bin'
            binaries.mkdir()
            source = root / 'ubuntu-cloud.qcow2'
            source.write_text('COMPRESSED:original-data')
            target = root / 'base/build.qcow2'
            if scenario == 'existing':
                target.write_text('existing-build-data')
            commands = {
                'qemu-img': '''
echo "qemu-img $*" >> "$ROOT/calls"
case "$1" in
  convert)
    [ "$5" = raw ]
    sed 's/^COMPRESSED://' "$6" > "$7" ;;
  check) [ "$3" = qcow2 ] ;;
  compare) [ "$SCENARIO" != compare-failure ] ;;
  *) exit 2 ;;
esac
''',
                'vmctl': '''
echo "vmctl $*" >> "$ROOT/calls"
case "$3" in raw:*) input=${3#raw:} ;; *) echo 'vmctl: unsupported refcount size' >&2; exit 1 ;; esac
case "$(cat "$input")" in COMPRESSED:*) exit 1 ;; esac
cp "$input" "$6"
[ "$SCENARIO" != native-failure ]
''',
            }
            for name, body in commands.items():
                command = binaries / name
                command.write_text('#!/bin/sh\nset -eu\n' + body)
                command.chmod(0o700)
            script = root / 'prepare.sh'
            script.write_text(SOURCE.read_text().replace(
                'PATH=/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin',
                f'PATH={binaries}:/bin:/usr/bin'
            ).replace('root=/home/vmm-ci', f'root={root}'))
            result = subprocess.run(['sh', str(script)], text=True, capture_output=True,
                                    timeout=5, env={**os.environ, 'ROOT': str(root),
                                                    'SCENARIO': scenario})
            return (result, target.read_text() if target.exists() else None,
                    source.read_text(), list((root / 'base').glob('convert.*')),
                    (root / 'calls').read_text() if (root / 'calls').exists() else '')

    def test_native_tool_only_reads_raw_and_writes_its_own_qcow2(self):
        result, target, source, temporary, calls = self.execute('normal')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target, 'original-data')
        self.assertEqual(source, 'COMPRESSED:original-data')
        self.assertEqual(temporary, [])
        self.assertLess(calls.index('qemu-img compare'), calls.index('vmctl create'))
        self.assertIn('vmctl create -i raw:', calls)
        self.assertNotIn('compat=0.10', calls)

    def test_comparison_failure_does_not_publish_disk(self):
        result, target, source, temporary, calls = self.execute('compare-failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(target)
        self.assertNotIn('vmctl create', calls)
        self.assertEqual(source, 'COMPRESSED:original-data')
        self.assertEqual(temporary, [])

    def test_native_failure_does_not_publish_partial_disk(self):
        result, target, _, temporary, _ = self.execute('native-failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(target)
        self.assertEqual(temporary, [])

    def test_existing_build_is_preserved(self):
        result, target, _, _, calls = self.execute('existing')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, 'existing-build-data')
        self.assertEqual(calls, '')
