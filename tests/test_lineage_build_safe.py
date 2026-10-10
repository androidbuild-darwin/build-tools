import importlib.machinery
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / 'lineage-build-safe'
loader = importlib.machinery.SourceFileLoader('safe_build', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
build = importlib.util.module_from_spec(spec)
loader.exec_module(build)


class MonitorTest(unittest.TestCase):
    def monitor(self, jobs, swaps, memory, times=None, logs=None, finishes=False):
        process = Mock()
        process.poll.side_effect = [None] * len(swaps) + ([0] if finishes else [])
        process.returncode = 0
        with patch.object(build, 'swap_used', side_effect=[x * build.GIB for x in swaps]), \
             patch.object(build, 'free_memory', side_effect=[x * build.GIB for x in memory]), \
             patch.object(build, 'log_mtime', side_effect=logs or [0] * (len(swaps) + 1)), \
             patch.object(build.time, 'monotonic', side_effect=times), \
             patch.object(build.time, 'sleep'), \
             patch.object(build, 'stop_build') as stop:
            result = build.monitor(process, jobs, 8, 4 * build.GIB, 2 * build.GIB,
                                   120, Path('/unused/ninja_log'))
            self.assertEqual(stop.call_count, 1 if isinstance(result, str) else 0)
            return result

    def test_swap_limit_requires_consecutive_readings(self):
        self.assertEqual(self.monitor(8, [5, 5, 3, 5, 5, 5], [8] * 6), 'down')

    def test_low_ram_reduces_jobs(self):
        self.assertEqual(self.monitor(6, [1] * 3, [1] * 3), 'down')

    def test_recovery_timer_resets(self):
        self.assertEqual(self.monitor(4, [1, 1, 3, 1, 1, 1], [8] * 6,
                                      times=[0, 0, 60, 100, 100, 160, 220],
                                      logs=list(range(7))), 'up')

    def test_no_increase_without_completed_jobs(self):
        self.assertEqual(self.monitor(4, [1] * 3, [8] * 3,
                                      times=[0, 0, 60, 120], finishes=True), 0)

    def test_two_jobs_continue_with_high_swap(self):
        self.assertEqual(self.monitor(2, [20] * 3, [1] * 3, finishes=True), 0)

    def test_maximum_jobs_do_not_increase(self):
        self.assertEqual(self.monitor(8, [1] * 3, [8] * 3, finishes=True), 0)

    def test_swap_units(self):
        with patch.object(build.subprocess, 'check_output',
                          return_value='total = 3072.00M used = 1630.00M free = 1442.00M'):
            self.assertEqual(build.swap_used(), 1630 * 1024 ** 2)

    def test_vm_page_size(self):
        with patch.object(build.subprocess, 'check_output', return_value='''
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free: 100.
Pages speculative: 20.
Pages purgeable: 5.
Pages inactive: 999.
'''):
            self.assertEqual(build.free_memory(), 125 * 16384)


if __name__ == '__main__':
    unittest.main()
