from pathlib import Path
import importlib.util
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native-host"))
from mhm.session import BrowserSession
from mhm.errors import HostError

ENGINE = ROOT / ".build/validation" / ("mihomo.exe" if os.name == "nt" else "mihomo")


class SessionTests(unittest.TestCase):
    def test_closed_session_rejects_late_spawn(self):
        session = BrowserSession()
        calls = []

        @session.guard_spawn
        def spawn():
            calls.append(True)

        session.close()
        with self.assertRaises(HostError):
            spawn()
        self.assertEqual(calls, [])
        session.close()

    def test_linux_refuses_to_start_without_parent_death_protection(self):
        from mhm.platforms import _linux_child_setup

        libc = mock.Mock()
        libc.prctl.return_value = -1
        with mock.patch("mhm.platforms.os.setsid"), mock.patch(
            "mhm.platforms.ctypes.CDLL", return_value=libc
        ):
            with self.assertRaises(OSError):
                _linux_child_setup(1234)

    @unittest.skipUnless(ENGINE.exists(), "mihomo required")
    def test_native_eof_kills_main_and_temporary_core_before_worker_finishes(self):
        self.check_shutdown("eof")

    @unittest.skipUnless(ENGINE.exists(), "mihomo required")
    def test_native_crash_kills_main_and_temporary_core(self):
        self.check_shutdown("crash")

    def check_shutdown(self, mode):
        wrapper = r"""
import sys,importlib.util,json,time
from pathlib import Path
root,base,engine=map(Path,sys.argv[1:])
spec=importlib.util.spec_from_file_location('host',root/'native-host/native_host.py')
nh=importlib.util.module_from_spec(spec);spec.loader.exec_module(nh)
nh.BASE_DIR=base;nh.DIAGNOSTIC_LOG=base/'log';nh.SETTINGS_PATH=base/'settings.json';nh.TRAFFIC=nh.TrafficStore(base/'traffic.sqlite3')
nh.SETTINGS.save({'mihomoPath':str(engine),'localPort':10808})
temporary=nh.MihomoProcess()
def handle(message):
 if message['command']=='start':
  first=nh.CLIENT.start_document({'rules':['MATCH,DIRECT']})
  second=temporary.start_document({'rules':['MATCH,DIRECT']})
  return {'ports':[first['localPort'],second['localPort']]}
 if message['command']=='slow':
  (base/'working').write_text('yes');time.sleep(3)
  try:
   temporary.start_document({'rules':['MATCH,DIRECT']})
  except nh.HostError:
   (base/'late-result').write_text('closed')
  else:
   (base/'late-result').write_text('spawned')
  return {}
 return {}
nh.handle=handle
try:sys.exit(nh.main())
finally:temporary.stop()
"""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    wrapper,
                    str(ROOT),
                    str(base),
                    str(ENGINE.resolve()),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            def send(command):
                data = json.dumps({"command": command, "requestId": command}).encode()
                process.stdin.write(struct.pack("<I", len(data)) + data)
                process.stdin.flush()

            try:
                send("start")
                replies = queue.Queue()

                def read():
                    length = process.stdout.read(4)
                    replies.put(
                        json.loads(process.stdout.read(struct.unpack("<I", length)[0]))
                    )

                threading.Thread(target=read, daemon=True).start()
                result = replies.get(timeout=15)
                self.assertTrue(result["ok"], result)
                ports = result["ports"]
                self.assertEqual(len(set(ports)), 2)
                send("slow")
                deadline = time.monotonic() + 5
                while not (base / "working").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((base / "working").exists())
                process.stdin.close() if mode == "eof" else process.kill()
                if mode == "crash":
                    process.wait(timeout=5)
                deadline = time.monotonic() + (1.5 if mode == "eof" else 5)
                remaining = set(ports)
                while remaining and time.monotonic() < deadline:
                    for port in list(remaining):
                        try:
                            with socket.create_connection(
                                ("127.0.0.1", port), timeout=0.05
                            ):
                                pass
                        except OSError:
                            remaining.remove(port)
                    time.sleep(0.02)
                self.assertFalse(
                    remaining,
                    "EOF must stop every core without waiting for a slow request",
                )
                self.assertFalse(
                    (base / "late-result").exists(),
                    "the worker must still be blocked when the ports close",
                )
                process.wait(timeout=10)
                if mode == "eof":
                    self.assertEqual((base / "late-result").read_text(), "closed")
                    self.assertEqual(
                        process.returncode, 0, process.stderr.read().decode()
                    )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if not process.stdin.closed:
                    process.stdin.close()
                process.stdout.close()
                process.stderr.close()
