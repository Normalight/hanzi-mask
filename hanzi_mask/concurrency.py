"""Conservative image-worker choices for the standalone desktop client.

Logical CPUs describe the machine, while MAX_WORKERS is the application's
supported cap. The recommended choice is deliberately smaller than that cap:
native sparse solves can consume substantial memory for each active image.
Memory detection is optional and never prevents the client from starting.
"""

import ctypes
from dataclasses import dataclass
import os
import sys


MAX_WORKERS = 4
_GIB = 1024 ** 3


@dataclass(frozen=True)
class WorkerLimits:
    logical_cpus: int
    maximum: int
    recommended: int
    total_memory_bytes: int | None


def _total_memory_bytes() -> int | None:
    """Read total physical memory using only standard-library OS interfaces."""
    try:
        if sys.platform == "win32":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_uint32),
                    ("memory_load", ctypes.c_uint32),
                    ("total_physical", ctypes.c_uint64),
                    ("available_physical", ctypes.c_uint64),
                    ("total_page_file", ctypes.c_uint64),
                    ("available_page_file", ctypes.c_uint64),
                    ("total_virtual", ctypes.c_uint64),
                    ("available_virtual", ctypes.c_uint64),
                    ("available_extended_virtual", ctypes.c_uint64),
                ]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            get_status = ctypes.windll.kernel32.GlobalMemoryStatusEx
            get_status.argtypes = [ctypes.POINTER(MemoryStatus)]
            get_status.restype = ctypes.c_int
            if get_status(ctypes.byref(status)) and status.total_physical > 0:
                return int(status.total_physical)
            return None
        if sys.platform == "darwin":
            get_value = ctypes.CDLL(None).sysctlbyname
            get_value.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                                  ctypes.c_size_t]
            get_value.restype = ctypes.c_int
            total = ctypes.c_uint64()
            size = ctypes.c_size_t(ctypes.sizeof(total))
            if (get_value(b"hw.memsize", ctypes.byref(total), ctypes.byref(size), None, 0) == 0
                    and size.value == ctypes.sizeof(total) and total.value > 0):
                return int(total.value)
            return None
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            return pages * page_size
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        # Missing system interfaces or an unavailable memory value are normal
        # on unsupported platforms; CPU-only limits remain safe to display.
        return None
    return None


def worker_limits() -> WorkerLimits:
    """Return hardware information, application maximum and recommended workers.

    Recommendation also keeps the previous half-CPU worker budget (at least
    one worker). Unknown memory only applies the CPU/application cap.
    Up to 4 GiB permits
    one worker; up to 8 GiB permits two. These are total-memory limits, not a
    claim about current free memory or a promise that each solve has fixed cost.
    """
    try:
        logical_cpus = max(1, int(os.cpu_count() or 1))
    except (OSError, TypeError, ValueError, OverflowError):
        logical_cpus = 1
    memory = _total_memory_bytes()
    maximum = min(logical_cpus, MAX_WORKERS)
    if memory is not None:
        if memory <= 4 * _GIB:
            maximum = min(maximum, 1)
        elif memory <= 8 * _GIB:
            maximum = min(maximum, 2)
    recommended = min(maximum, 2, max(1, logical_cpus // 2))
    return WorkerLimits(logical_cpus, maximum, recommended, memory)
