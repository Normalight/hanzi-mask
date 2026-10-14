import ctypes
from types import SimpleNamespace

import pytest

from hanzi_mask import concurrency


GIB = 1024 ** 3


@pytest.mark.parametrize("cpus,memory,maximum,recommended", [
    (10, 16 * GIB, 4, 2),
    (128, None, 4, 2),
    (1, 16 * GIB, 1, 1),
    (2, 16 * GIB, 2, 1),
    (3, 16 * GIB, 3, 1),
    (10, 4 * GIB, 1, 1),
    (10, 4 * GIB + 1, 2, 2),
    (10, 8 * GIB, 2, 2),
    (10, 8 * GIB + 1, 4, 2),
    (None, None, 1, 1),
    (0, None, 1, 1),
    (-1, None, 1, 1),
])
def test_worker_policy_distinguishes_cpu_count_application_cap_and_recommendation(
        monkeypatch, cpus, memory, maximum, recommended):
    monkeypatch.setattr(concurrency.os, "cpu_count", lambda: cpus)
    monkeypatch.setattr(concurrency, "_total_memory_bytes", lambda: memory)
    limits = concurrency.worker_limits()
    assert limits.logical_cpus == max(1, cpus or 1)
    assert limits.total_memory_bytes == memory
    assert limits.maximum == maximum
    assert limits.recommended == recommended
    assert 1 <= limits.recommended <= limits.maximum <= concurrency.MAX_WORKERS
    assert limits.maximum <= limits.logical_cpus


def test_unavailable_cpu_information_does_not_prevent_startup(monkeypatch):
    def unavailable():
        raise OSError("CPU information unavailable")

    monkeypatch.setattr(concurrency.os, "cpu_count", unavailable)
    monkeypatch.setattr(concurrency, "_total_memory_bytes", lambda: None)
    assert concurrency.worker_limits() == concurrency.WorkerLimits(1, 1, 1, None)


@pytest.mark.parametrize("pages,page_size,expected", [(1024, 4096, 4194304), (0, 4096, None), (1024, -1, None)])
def test_posix_memory_detection_handles_values_and_invalid_information(monkeypatch, pages, page_size, expected):
    monkeypatch.setattr(concurrency.sys, "platform", "linux")
    # Windows ships os as a frozen module without sysconf; the simulation
    # installs the POSIX attribute explicitly.
    monkeypatch.setattr(concurrency.os, "sysconf",
                        lambda name: {"SC_PHYS_PAGES": pages, "SC_PAGE_SIZE": page_size}[name],
                        raising=False)
    assert concurrency._total_memory_bytes() == expected


def test_posix_memory_detection_failure_falls_back_to_cpu_limits(monkeypatch):
    monkeypatch.setattr(concurrency.sys, "platform", "linux")

    def unavailable(_):
        raise OSError("memory information unavailable")

    monkeypatch.setattr(concurrency.os, "sysconf", unavailable, raising=False)
    monkeypatch.setattr(concurrency.os, "cpu_count", lambda: 10)
    assert concurrency.worker_limits() == concurrency.WorkerLimits(10, 4, 2, None)


@pytest.mark.parametrize("success", [True, False])
def test_macos_memory_detection_uses_standard_library_sysctl(monkeypatch, success):
    monkeypatch.setattr(concurrency.sys, "platform", "darwin")

    def get_value(name, value, length, new_value, new_length):
        assert name == b"hw.memsize" and new_value is None and new_length == 0
        ctypes.cast(value, ctypes.POINTER(ctypes.c_uint64)).contents.value = 16 * GIB
        ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t)).contents.value = ctypes.sizeof(ctypes.c_uint64)
        return 0 if success else -1

    monkeypatch.setattr(concurrency.ctypes, "CDLL", lambda _: SimpleNamespace(sysctlbyname=get_value))
    assert concurrency._total_memory_bytes() == (16 * GIB if success else None)


@pytest.mark.parametrize("success", [True, False])
def test_windows_memory_detection_uses_standard_library_status(monkeypatch, success):
    monkeypatch.setattr(concurrency.sys, "platform", "win32")

    def get_status(pointer):
        status = pointer._obj
        assert status.length == ctypes.sizeof(status)
        status.total_physical = 8 * GIB
        return int(success)

    monkeypatch.setattr(concurrency.ctypes, "windll",
                        SimpleNamespace(kernel32=SimpleNamespace(GlobalMemoryStatusEx=get_status)), raising=False)
    assert concurrency._total_memory_bytes() == (8 * GIB if success else None)


def test_missing_memory_interface_is_optional(monkeypatch):
    monkeypatch.setattr(concurrency.sys, "platform", "darwin")
    monkeypatch.setattr(concurrency.ctypes, "CDLL", lambda _: SimpleNamespace())
    assert concurrency._total_memory_bytes() is None
