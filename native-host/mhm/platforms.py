"""Operating-system process containment primitives."""

from __future__ import annotations
import os
import signal
import ctypes


class _WinIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _WinBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _WinExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WinBasicLimitInformation),
        ("IoInfo", _WinIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsKillJob:
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("CreateJobObjectW failed")
        info = _WinExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            ctypes.c_void_p(handle),
            self.JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise OSError("SetInformationJobObject failed")
        self.handle = handle

    def assign(self, pid: int) -> None:
        if os.name != "nt" or not self.handle:
            return
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        process_handle = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )
        if not process_handle:
            raise OSError("OpenProcess failed for job assignment")
        try:
            if not kernel32.AssignProcessToJobObject(
                ctypes.c_void_p(self.handle), ctypes.c_void_p(process_handle)
            ):
                raise OSError("AssignProcessToJobObject failed")
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(process_handle))

    def close(self) -> None:
        if os.name == "nt" and self.handle:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


def _linux_child_setup(parent_pid: int) -> None:
    os.setsid()
    try:
        libc = ctypes.CDLL(None)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGKILL)
    except Exception:
        pass
