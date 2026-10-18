"""Real finite and hostile subprocess streams, without Docker or provider effects."""
import subprocess
import sys
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from research_workspace.bounded_process import collect, STDOUT_LIMIT, STDERR_LIMIT
from research_workspace.runtime import Broker


class BoundedProcessTests(unittest.TestCase):
    def process(self, code):
        return subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def assert_closed(self, process):
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdout.closed); self.assertTrue(process.stderr.closed)

    def test_exact_limits_are_accepted_and_both_pipes_drain(self):
        p=self.process(f'import os; os.write(1,b"a"*{STDOUT_LIMIT}); os.write(2,b"b"*{STDERR_LIMIT})')
        stdout,stderr=collect(p,timeout=3)
        self.assertEqual(len(stdout),STDOUT_LIMIT); self.assertEqual(len(stderr),STDERR_LIMIT)
        self.assert_closed(p)

    def test_endless_stdout_or_stderr_is_stopped_with_bounded_memory(self):
        for fd in (1,2):
            with self.subTest(fd=fd):
                p=self.process(f'import os\nwhile True: os.write({fd},b"x"*65536)')
                tracemalloc.start()
                try:
                    with self.assertRaisesRegex(ValueError,'oversized'):collect(p,timeout=3)
                    _,peak=tracemalloc.get_traced_memory()
                    self.assertLess(peak,2*1024*1024)
                finally:tracemalloc.stop()
                self.assert_closed(p)

    def test_closed_pipes_do_not_allow_a_live_process_to_escape_deadline(self):
        p=self.process('import os,time; os.close(1); os.close(2); time.sleep(30)')
        start=time.monotonic()
        with self.assertRaisesRegex(ValueError,'deadline'):collect(p,timeout=0.1)
        self.assertLess(time.monotonic()-start,2);self.assert_closed(p)

    def test_controls_are_polled_while_output_is_quiet(self):
        p=self.process('import time; time.sleep(.6); print("done")');ticks=[]
        stdout,_=collect(p,timeout=3,tick=lambda:ticks.append(True))
        self.assertEqual(stdout,b'done\n');self.assertGreaterEqual(len(ticks),2);self.assert_closed(p)

    def test_failed_control_callback_closes_observer(self):
        p=self.process('import time; time.sleep(30)')
        def fail():raise RuntimeError('control failed')
        with self.assertRaisesRegex(RuntimeError,'control failed'):collect(p,timeout=3,tick=fail)
        self.assert_closed(p)

    def test_slow_final_control_callback_cannot_accept_after_deadline(self):
        p=self.process('print("{}")')
        with self.assertRaisesRegex(ValueError,'deadline'):
            collect(p,timeout=0.08,tick=lambda:time.sleep(0.12))
        self.assert_closed(p)

    def test_broker_call_streams_and_rejects_overflow_without_file_effects(self):
        broker=Broker(sys.executable,Path.cwd())
        broker.command=lambda *args:[sys.executable,'-c','print("{}")']
        self.assertEqual(broker.call('synthetic'),{})
        broker.command=lambda *args:[sys.executable,'-c','import os\nwhile True: os.write(2,b"x"*65536)']
        with self.assertRaisesRegex(ValueError,'oversized'):broker.call('synthetic')

    def test_nonzero_broker_receipt_is_not_accepted(self):
        broker=Broker(sys.executable,Path.cwd())
        broker.command=lambda *args:[sys.executable,'-c','import sys; print("{}"); sys.exit(1)']
        with self.assertRaisesRegex(ValueError,'refused'):broker.call('synthetic')
