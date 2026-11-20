"""Exercise host lifecycle failures without requiring a hypervisor or root."""
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "openbsd/run-vm.sh"


class LifecycleTest(unittest.TestCase):
    def execute(self, scenario, automatic=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            state = root / "state"
            state.mkdir()
            (root / "base").mkdir()
            (root / "runtime").mkdir()
            (root / "base/github.qcow2").touch()
            (root / "job.vm.conf").write_text('runtime config')
            (root / "job-seed").mkdir()
            (root / "job-seed/user-data").write_text('#cloud-config\n')
            (root / "job-seed/network-config").write_text('version: 2\n')
            if automatic:
                (root / 'runtime/lock').mkdir()
                (root / 'runtime/lock/session').write_text('a' * 32)
            if scenario == "existing":
                (state / "active").touch()
            commands = {
                "id": "echo 0",
                "uname": "echo Linux" if scenario == "unsupported" else "echo OpenBSD",
                "sleep": "exit 0",
                "date": 'n=$(cat "$STATE/clock" 2>/dev/null || echo 0); '
                        'n=$((n+2000)); echo "$n" > "$STATE/clock"; echo "$n"',
                "vmd": 'test -f "$STATE/../runtime/job.qcow2"; test -f "$STATE/../runtime/job-seed.iso"',
                "xorriso": 'if [ "$AUTOMATIC" = true ]; then '
                           'grep -q jit-private "$9/user-data"; fi; touch "$4"',
                "vmctl": '''
echo "$*" >> "$STATE/calls"
case "$1" in
  status)
    if [ "$SCENARIO" = connection-error ]; then
      echo 'vmctl: connect: Connection refused' >&2; exit 1
    fi
    if [ "$SCENARIO" = malformed-status ]; then
      echo 'unexpected server response'; exit 1
    fi
    echo 'ID PID VCPUS MAXMEM CURMEM TTY OWNER STATE NAME'
    if [ -f "$STATE/active" ]; then
      if [ "$SCENARIO" = normal ]; then
        n=$(cat "$STATE/queries" 2>/dev/null || echo 0)
        n=$((n+1)); echo "$n" > "$STATE/queries"
        if [ "$n" -ge 2 ]; then rm "$STATE/active"; exit 1; fi
      fi
      echo '1 2 2 8G 1G tty root running ci-runner'
    else
      exit 1
    fi ;;
  create) touch "$4" ;;
  reload) exit 0 ;;
  load) test -f "$STATE/../runtime/job.qcow2" ;;
  start)
    [ "$2" = ci-runner ]
    touch "$STATE/active" ;;
  stop)
    [ "$SCENARIO" != stop-failure ] || exit 1
    rm -f "$STATE/active" ;;
  *) exit 2 ;;
esac
''',
            }
            for name, body in commands.items():
                command = binaries / name
                command.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
                command.chmod(0o700)
            # Only substitute platform boundaries; run actual production control flow.
            script = root / "run.sh"
            script.write_text(SOURCE.read_text().replace(
                "PATH=/bin:/sbin:/usr/bin:/usr/sbin",
                f"PATH={binaries}:/bin:/usr/bin"
            ).replace("root=/home/vmm-ci", f"root={root}"))
            result = subprocess.run(
                ["sh", str(script), "github"] + (["--auto", 'a' * 32] if automatic else []),
                capture_output=True, text=True,
                input='#cloud-config\n{"jit":"jit-private"}' if automatic else None,
                env={**os.environ, "STATE": str(state), "SCENARIO": scenario,
                     'AUTOMATIC': 'true' if automatic else 'false'},
                timeout=5,
            )
            return (result, (root / "runtime/job.qcow2").exists(),
                    (root / "runtime/lock").exists(),
                    (state / "calls").read_text() if (state / "calls").exists() else "")

    def test_unsupported_host_never_calls_vmctl(self):
        result, disk, lock, calls = self.execute("unsupported")
        self.assertEqual(result.returncode, 1)
        self.assertIn("OpenBSD required", result.stderr)
        self.assertEqual(calls, "")
        self.assertFalse(disk)
        self.assertFalse(lock)

    def test_normal_shutdown_removes_disk_and_lock(self):
        result, disk, lock, calls = self.execute("normal")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(disk)
        self.assertFalse(lock)
        operations = calls.splitlines()
        create = next(i for i, line in enumerate(operations) if line.startswith('create '))
        load = next(i for i, line in enumerate(operations) if line.startswith('load '))
        start = operations.index('start ci-runner')
        self.assertLess(create, load)
        self.assertLess(load, start)
        self.assertEqual(operations[-1], 'reload')

    def test_automatic_seed_is_private_and_session_is_cleaned(self):
        result, disk, lock, _ = self.execute('normal', automatic=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('jit-private', result.stdout + result.stderr)
        self.assertFalse(disk)
        self.assertFalse(lock)

    def test_persistent_config_needs_neither_job_disk_nor_diskless_vm(self):
        config = SOURCE.with_name('vmd-host.conf').read_text()
        self.assertEqual(re.findall(r'^\s*vm\s+"', config, re.M), [])
        self.assertEqual(re.findall(r'^\s*disk\s+', config, re.M), [])

    def test_deadline_stops_vm_and_removes_disk(self):
        result, disk, lock, calls = self.execute("timeout")
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("stop -fw ci-runner", calls)
        self.assertFalse(disk)
        self.assertFalse(lock)

    def test_existing_vm_is_never_stopped(self):
        result, disk, lock, calls = self.execute("existing")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("stop", calls)
        self.assertNotIn("create", calls)
        self.assertFalse(disk)
        self.assertFalse(lock)

    def test_stop_failure_retains_disk_and_lock(self):
        result, disk, lock, _ = self.execute("stop-failure")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(disk)
        self.assertTrue(lock)

    def test_real_connection_error_is_not_an_empty_list(self):
        result, disk, lock, calls = self.execute('connection-error')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('create', calls)
        self.assertNotIn('start ', calls)
        self.assertFalse(disk)
        self.assertTrue(lock)

    def test_unknown_status_output_is_not_an_empty_list(self):
        result, disk, lock, calls = self.execute('malformed-status')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('create', calls)
        self.assertFalse(disk)
        self.assertTrue(lock)


if __name__ == "__main__":
    unittest.main()
