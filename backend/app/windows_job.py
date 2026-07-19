from __future__ import annotations

import ctypes
import os
from typing import Any, Protocol


CREATE_SUSPENDED = 0x00000004


class KillOnCloseJob(Protocol):
    def assign(self, process: Any) -> None: ...

    def resume(self, process: Any) -> None: ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobObjectBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class WindowsKillOnCloseJob:
    """A suspended-process Job Object that kills every assigned descendant on close."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll")
        self._configure_api()
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self.handle = int(handle)
        limits = _JobObjectExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            ctypes.c_void_p(self.handle),
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            try:
                self.close()
            except OSError:
                pass
            raise OSError(error, "SetInformationJobObject failed")

    def _configure_api(self) -> None:
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._kernel32.SetInformationJobObject.restype = ctypes.c_int
        self._kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        self._kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel32.TerminateJobObject.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int
        self._ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
        self._ntdll.NtResumeProcess.restype = ctypes.c_long

    def assign(self, process: Any) -> None:
        process_handle = ctypes.c_void_p(int(getattr(process, "_handle")))
        if not self._kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(self.handle), process_handle
        ):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def resume(self, process: Any) -> None:
        process_handle = ctypes.c_void_p(int(getattr(process, "_handle")))
        status = int(self._ntdll.NtResumeProcess(process_handle))
        if status != 0:
            raise OSError(status, "NtResumeProcess failed")

    def terminate(self) -> None:
        if self.handle and not self._kernel32.TerminateJobObject(
            ctypes.c_void_p(self.handle), 1
        ):
            raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def close(self) -> None:
        if not getattr(self, "handle", 0):
            return
        if not self._kernel32.CloseHandle(ctypes.c_void_p(self.handle)):
            raise OSError(ctypes.get_last_error(), "CloseHandle failed")
        self.handle = 0


__all__ = ["CREATE_SUSPENDED", "KillOnCloseJob", "WindowsKillOnCloseJob"]
