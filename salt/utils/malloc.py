"""
Direct access to glibc's ``malloc_trim(3)``.

Used by the master's ``EventPublisher`` (see
:conf_master:`event_publisher_memory_max`) to return arena top-of-heap
free space to the kernel at a natural cool-off point — when the
in-flight-bytes gate drops back to zero.  Python's own memory manager
(pymalloc) frees objects promptly, but glibc's default policy is to hold
freed arena chunks against future demand.  Long-running Python daemons
under bursty allocation therefore see RSS ratchet upward permanently
even though the live Python-object footprint is much smaller.

Calling ``malloc_trim(0)`` from Salt code (rather than an operator
env-var / LD_PRELOAD swap) lets the code that owns the burst also
release its arena at burst-end, without asking the operator to configure
anything at the host level.

Best-effort: no-op on non-glibc platforms (musl, macOS, Windows) or if
libc lookup fails.  Never raises.
"""

import ctypes
import ctypes.util
import logging

log = logging.getLogger(__name__)


# glibc mallopt(3) param IDs (see <malloc.h>).  Not exposed by Python's
# stdlib; we hard-code the values glibc has used since forever.
M_MXFAST = 1
M_TRIM_THRESHOLD = -1
M_TOP_PAD = -2
M_MMAP_THRESHOLD = -3
M_MMAP_MAX = -4


_libc = None
_LOOKED_UP = False


def _get_libc():
    """Load libc.so and cache the handle. Returns None if unavailable."""
    global _libc, _LOOKED_UP  # pylint: disable=global-statement
    if _LOOKED_UP:
        return _libc
    _LOOKED_UP = True
    try:
        libname = ctypes.util.find_library("c")
        if libname is None:
            log.debug("ctypes.util.find_library('c') returned None; malloc_trim disabled")
            return None
        _libc = ctypes.CDLL(libname, use_errno=True)
        if not hasattr(_libc, "malloc_trim"):
            log.debug(
                "%s has no malloc_trim symbol (musl / non-glibc?); disabled",
                libname,
            )
            _libc = None
            return None
        _libc.malloc_trim.restype = ctypes.c_int
        _libc.malloc_trim.argtypes = [ctypes.c_size_t]
    except Exception:  # pylint: disable=broad-exception-caught
        log.debug("libc lookup failed; malloc_trim disabled", exc_info=True)
        _libc = None
    return _libc


def malloc_trim(pad=0):
    """
    Call glibc's ``malloc_trim(pad)`` to release free arena space above
    ``pad`` bytes back to the kernel.

    Returns 1 if memory was released, 0 if not, or -1 if the call
    couldn't be made (non-glibc platform, symbol missing).
    """
    libc = _get_libc()
    if libc is None:
        return -1
    try:
        return libc.malloc_trim(pad)
    except Exception:  # pylint: disable=broad-exception-caught
        log.debug("malloc_trim call failed", exc_info=True)
        return -1


def mallopt(param, value):
    """
    Call glibc's ``mallopt(param, value)`` to tune allocator behavior.

    Common params (see this module's ``M_*`` constants):

    * ``M_MMAP_THRESHOLD`` -- allocations >= value bytes go through
      ``mmap`` (immediately unmap'd on ``free``) instead of the arena.
      Default is dynamic (starts at 128 KB, can grow to 32 MB).  Pinning
      it low (e.g. 32 KB) forces mid-size transient allocations off the
      arena so ``free`` actually returns pages to the kernel, avoiding
      the arena-fragmentation pattern that pins RSS for daemons with
      bursty mid-size allocations.  Trade-off: extra ~40 us per mmap.

    Returns 1 on success, 0 on failure, or -1 if the call couldn't be
    made (non-glibc platform, symbol missing).
    """
    libc = _get_libc()
    if libc is None:
        return -1
    try:
        if not hasattr(libc, "mallopt"):
            return -1
        libc.mallopt.restype = ctypes.c_int
        libc.mallopt.argtypes = [ctypes.c_int, ctypes.c_int]
        return libc.mallopt(param, value)
    except Exception:  # pylint: disable=broad-exception-caught
        log.debug("mallopt call failed", exc_info=True)
        return -1


def available():
    """Return True if ``malloc_trim`` can be called on this platform."""
    return _get_libc() is not None
