import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
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
             patch.object(build, 'kernel_running', return_value=False), \
             patch.object(build, 'stop_build') as stop:
            result = build.monitor(process, jobs, 8, 4 * build.GIB, 2 * build.GIB,
                                   120, Path('/unused/ninja_log'))
            self.assertEqual(stop.call_count, 1 if isinstance(result, str) else 0)
            return result

    def test_swap_limit_requires_consecutive_readings(self):
        self.assertEqual(self.monitor(8, [5, 5, 3, 5, 5, 5], [8] * 6), 'down')

    def test_low_ram_reduces_jobs(self):
        self.assertEqual(self.monitor(6, [1] * 3, [1] * 3), 'down')

    def test_ram_pressure_reduces_jobs_before_swap(self):
        self.assertEqual(self.monitor(16, [0] * 3, [3] * 3), 'down')

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

    def test_initial_build_checks_graph_and_resume_reuses_it(self):
        initial = build.build_command(Path('/root/soong_ui.bash'), 8, 'bacon')
        resumed = build.build_command(Path('/root/soong_ui.bash'), 6, 'bacon', True)
        self.assertNotIn('--skip-make', initial)
        self.assertNotIn('--skip-soong', initial)
        self.assertIn('--skip-make', resumed)
        self.assertIn('--skip-soong', resumed)
        self.assertIn('KERNEL_JOBS=8', initial)
        self.assertEqual(resumed[-3:], ['-j6', 'KERNEL_JOBS=8', 'bacon'])

    def test_old_ninja_environment_does_not_interrupt_soong(self):
        process = Mock()
        process.poll.side_effect = [None, None, None, 0]
        process.returncode = 0
        with patch.object(build, 'log_mtime', return_value=123), \
             patch.object(build, 'swap_used') as swap, \
             patch.object(build.time, 'sleep'), \
             patch.object(build, 'stop_build') as stop:
            result = build.monitor(process, 8, 16, 4 * build.GIB, 2 * build.GIB,
                                   300, Path('/log'), ready_file=Path('/env'),
                                   ready_before=123)
            self.assertEqual(result, 0)
            swap.assert_not_called()
            stop.assert_not_called()

    def test_monitor_starts_only_after_new_ninja_environment(self):
        process = Mock()
        process.poll.side_effect = [None] * 5
        with patch.object(build, 'log_mtime', side_effect=[0, 10, 10, 11, 0, 0, 0]), \
             patch.object(build, 'swap_used', return_value=5 * build.GIB) as swap, \
             patch.object(build, 'free_memory', return_value=8 * build.GIB), \
             patch.object(build, 'kernel_running', return_value=False), \
             patch.object(build.time, 'sleep'), \
             patch.object(build, 'stop_build') as stop:
            result = build.monitor(process, 8, 16, 4 * build.GIB, 2 * build.GIB,
                                   300, Path('/log'), ready_file=Path('/env'),
                                   ready_before=10)
            self.assertEqual(result, 'down')
            self.assertEqual(swap.call_count, 3)
            stop.assert_called_once_with(process)

    def test_kernel_recipe_finishes_before_job_change(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(build, 'log_mtime', return_value=0), \
             patch.object(build, 'swap_used', return_value=5 * build.GIB), \
             patch.object(build, 'free_memory', return_value=8 * build.GIB), \
             patch.object(build, 'kernel_running', side_effect=[True, True, False]) as kernel, \
             patch.object(build.time, 'sleep'), \
             patch.object(build, 'stop_build') as stop:
            self.assertEqual(build.monitor(process, 8, 16, 4 * build.GIB,
                                           2 * build.GIB, 300, Path('/log')), 'down')
            self.assertEqual(kernel.call_count, 3)
            stop.assert_called_once_with(process)

    def test_kernel_detection_ignores_unrelated_builds(self):
        process = Mock(pid=123)
        output = '999 1 /tools/make O=/out/KERNEL_OBJ Image\n123 1 soong_ui bacon\n'
        with patch.object(build.subprocess, 'check_output', return_value=output):
            self.assertFalse(build.kernel_running(process))

    def test_kernel_detection_follows_ninja_descendants(self):
        process = Mock(pid=123)
        # ps ordering must not matter, and a nested recipe may have its own group.
        output = ('456 234 /tools/make O=/out/KERNEL_OBJ Image\n'
                  '234 123 ninja bacon\n123 1 soong_ui bacon\n')
        with patch.object(build.subprocess, 'check_output', return_value=output):
            self.assertTrue(build.kernel_running(process))


class StopBuildTest(unittest.TestCase):
    def test_waits_for_child_in_separate_group_after_driver_exits(self):
        process = Mock(pid=100)
        process.poll.side_effect = [None, 0, 0]
        driver = (1, 100, 'S', 'driver start')
        child = (100, 200, 'S', 'child start')
        orphan = (1, 200, 'S', 'child start')
        with patch.object(build, 'process_table', side_effect=[
                {100: driver, 200: child}, {200: orphan}, {}]), \
             patch.object(build.os, 'kill') as kill, \
             patch.object(build.time, 'sleep') as sleep:
            build.stop_build(process)
        self.assertEqual({call.args[0] for call in kill.call_args_list}, {100, 200})
        sleep.assert_called_once_with(0.2)
        process.wait.assert_called_once()

    def test_does_not_signal_reused_child_pid(self):
        process = Mock(pid=100)
        process.poll.side_effect = [None, 0]
        with patch.object(build, 'process_table', side_effect=[
                {100: (1, 100, 'S', 'driver'), 200: (100, 200, 'S', 'old')},
                {200: (1, 200, 'S', 'new')}]), \
             patch.object(build.os, 'kill') as kill:
            build.stop_build(process)
        self.assertEqual(kill.call_count, 2)
        process.wait.assert_called_once()


class LauncherTest(unittest.TestCase):
    def run_launcher(self, results):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'out').mkdir()
            source = root / 'build/soong'
            (source / 'ui/build').mkdir(parents=True)
            (source / 'soong_ui.bash').touch()
            (source / 'ui/build/config.go').write_text('arg == "--skip-soong"')
            with patch.object(build.sys, 'argv', ['launcher', '--root', directory]), \
                 patch.object(build, 'swap_used', return_value=0), \
                 patch.object(build, 'monitor', side_effect=results) as monitor, \
                 patch.object(build.subprocess, 'Popen') as popen:
                result = build.main()
                return result, popen.call_args_list, monitor.call_args_list

    def test_only_in_session_job_changes_skip_graph_analysis(self):
        result, processes, monitors = self.run_launcher(['down', 'up', 0])
        self.assertEqual(result, 0)
        self.assertEqual(len(processes), 3)
        self.assertEqual([p.args[0][-3] for p in processes], ['-j16', '-j14', '-j16'])
        self.assertNotIn('--skip-soong', processes[0].args[0])
        for process in processes[1:]:
            self.assertIn('--skip-soong', process.args[0])
            self.assertIn('--skip-make', process.args[0])
        self.assertIsNotNone(monitors[0].kwargs['ready_file'])
        self.assertIsNone(monitors[1].kwargs['ready_file'])
        self.assertEqual(monitors[0].args[2], 16)

    def test_actual_failure_does_not_restart(self):
        result, processes, _ = self.run_launcher([1])
        self.assertEqual(result, 1)
        self.assertEqual(len(processes), 1)


if __name__ == '__main__':
    unittest.main()
