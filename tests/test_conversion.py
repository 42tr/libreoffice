import concurrent.futures
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import libreoffice_client as client


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profiles = self.root / 'profiles'
        for name, value in [('CLI_PROFILE_DIR', self.profiles),
                            ('conversion_slots', client.make_slots(2))]:
            p = patch.object(client, name, value)
            p.start()
            self.addCleanup(p.stop)

    def paths(self, i=0):
        directory = self.root / str(i)
        directory.mkdir(exist_ok=True)
        source = directory / 'source.txt'
        source.write_text(str(i))
        return source, directory / 'result.pdf'

    def test_parallel_jobs_have_isolated_profiles_and_outputs(self):
        barrier = threading.Barrier(2)
        guard = threading.Lock()
        in_use, outdirs, profiles = set(), set(), set()
        active = peak = 0

        def run(command, deadline=None, env=None):
            nonlocal active, peak
            profile = next(arg for arg in command if arg.startswith('-env:'))
            outdir = Path(command[command.index('--outdir') + 1])
            with guard:
                self.assertNotIn(profile, in_use)
                self.assertNotIn(outdir, outdirs)
                in_use.add(profile)
                outdirs.add(outdir)
                profiles.add(profile)
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=5)
            source = Path(command[-1])
            (outdir / 'source.pdf').write_text(source.read_text())
            with guard:
                in_use.remove(profile)
                active -= 1
            return subprocess.CompletedProcess(command, 0, '', '')

        paths = [self.paths(i) for i in range(6)]
        with patch.object(client, '_run_command', side_effect=run):
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda pair: client.convert(*pair), paths))
        self.assertEqual(peak, 2)
        # Profiles are reused per slot instead of being created for every job.
        self.assertEqual(len(profiles), 2)
        for i, (_, target) in enumerate(paths):
            self.assertEqual(target.read_text(), str(i))
        self.assertEqual(list(self.profiles.iterdir()), [])

    def test_failed_conversion_discards_profile(self):
        profile_dirs = []

        def run(command, deadline=None, env=None):
            profile = Path(next(arg for arg in command if arg.startswith('-env:'))
                           .split('=', 1)[1].removeprefix('file://'))
            profile.mkdir(parents=True)
            profile_dirs.append(profile)
            return subprocess.CompletedProcess(command, 1, '', 'failed')

        with patch.object(client, '_run_command', side_effect=run):
            with self.assertRaises(RuntimeError):
                client.convert(*self.paths())
        self.assertFalse(profile_dirs[0].exists())

    def test_pdf_to_pdf_is_copied_without_libreoffice(self):
        source = self.root / 'input.pdf'
        source.write_bytes(b'%PDF-1.4 test')
        with patch.object(client, '_run_command') as run:
            result = client.convert(source, self.root / 'result.pdf', 'pdf')
            run.assert_not_called()
        self.assertEqual(result.read_bytes(), b'%PDF-1.4 test')

    def test_task_deadline_limits_command_timeout(self):
        with patch.object(client, 'CLI_TIMEOUT_SECONDS', 30):
            with self.assertRaises(subprocess.TimeoutExpired):
                client._run_command([sys.executable, '-c', 'import time; time.sleep(30)'],
                                    deadline=time.monotonic() + 0.2)
            with patch.object(subprocess, 'Popen') as popen:
                with self.assertRaises(subprocess.TimeoutExpired):
                    client._run_command(['soffice'], deadline=time.monotonic() - 1)
                popen.assert_not_called()

    def test_undecodable_output_is_replaced(self):
        result = client._run_command(
            [sys.executable, '-c', 'import sys; sys.stderr.buffer.write(bytes([0xff, 0xfe]))'])
        self.assertEqual(result.returncode, 0)
        self.assertIn('\ufffd', result.stderr)

    def test_queue_timeout(self):
        slots = client.make_slots(1)
        slots.get()
        with patch.object(client, 'conversion_slots', slots), \
             patch.object(client, 'QUEUE_TIMEOUT_SECONDS', 0.01), \
             patch.object(client, '_run_command') as run:
            with self.assertRaises(client.ConversionBusyError):
                client.convert(*self.paths())
            run.assert_not_called()

    def test_failure_cleans_profile_and_releases_slot(self):
        for result in [subprocess.CompletedProcess([], 1, '', 'failed'),
                       subprocess.CompletedProcess([], 0, '', '')]:
            with patch.object(client, '_run_command', return_value=result):
                with self.assertRaises(RuntimeError):
                    client.convert(*self.paths())
            self.assertEqual(list(self.profiles.iterdir()), [])
            self.assertEqual(client.conversion_slots.qsize(), 2)

    def test_invalid_format_does_not_launch_process(self):
        with patch.object(client, '_run_command') as run:
            with self.assertRaises(ValueError):
                client.convert(*self.paths(), fmt='../pdf')
            run.assert_not_called()

    def test_timeout_kills_child_process(self):
        marker = self.root / 'orphan'
        child = 'import time; from pathlib import Path; time.sleep(1); Path(%r).touch()' % str(marker)
        parent = ('import subprocess, sys, time; '
                  'subprocess.Popen([sys.executable, "-c", %r]); time.sleep(30)') % child
        with patch.object(client, 'CLI_TIMEOUT_SECONDS', 0.2):
            with self.assertRaises(subprocess.TimeoutExpired):
                client._run_command([sys.executable, '-c', parent])
        time.sleep(1.1)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
