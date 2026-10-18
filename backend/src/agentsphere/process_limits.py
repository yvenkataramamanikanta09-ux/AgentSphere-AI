"""OS resource containment for parser children; this is not a malware sandbox."""

import os
import signal


class ResourceLimitsUnavailable(RuntimeError):
    pass


class WindowsJob:
    """Kill-on-close job with hard process-memory and child-count limits."""

    def __init__(self, process, memory_mb):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD),
                ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority_class", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "read_operations",
                    "write_operations",
                    "other_operations",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", IoCounters),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ResourceLimitsUnavailable("Parser containment could not be established.")
        try:
            limits = Extended()
            limits.basic.flags = 0x2000 | 0x100 | 0x200 | 0x8
            limits.basic.active_process_limit = 1
            limits.process_memory = limits.job_memory = memory_mb * 1024 * 1024
            if not kernel.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ResourceLimitsUnavailable("Parser containment could not be established.")
            child = kernel.OpenProcess(0x0101, False, process.pid)
            if not child:
                raise ResourceLimitsUnavailable("Parser containment could not be established.")
            try:
                if not kernel.AssignProcessToJobObject(handle, child):
                    raise ResourceLimitsUnavailable("Parser containment could not be established.")
            finally:
                kernel.CloseHandle(child)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        self.handle = handle

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def apply_child_limits(memory_mb, timeout_seconds, parent_pid):
    """POSIX limits are installed before importing any document parser."""
    if os.name == "nt":
        # Parent assigns a Job Object before sending any document input.
        return
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    maximum_fds = min(64, resource.getrlimit(resource.RLIMIT_NOFILE)[1])
    resource.setrlimit(resource.RLIMIT_NOFILE, (maximum_fds, maximum_fds))
    if os.sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
            raise ResourceLimitsUnavailable("Parser parent containment could not be established.")
