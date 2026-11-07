"""Hand memory back to the system once a download has finished with it.

Network threads allocate the download buffers and another thread frees them. With glibc's defaults
every thread gets its own arena, and buffers of several MB come from those arenas, so a download
leaves hundreds of MB free inside the process but still counted against it. Other systems' allocators
do not need this, and without glibc these do nothing.
"""
import ctypes
import sys

_M_MMAP_THRESHOLD = -3
_M_ARENA_MAX = -8
_MMAP_THRESHOLD = 1024 * 1024
_ARENAS = 2


def _glibc():
    if not sys.platform.startswith('linux'):
        return None
    library = ctypes.CDLL(None)
    return library if hasattr(library, 'mallopt') and hasattr(library, 'malloc_trim') else None


def tune() -> None:
    """Keep large buffers out of the arenas and the arenas few. Call before any thread starts."""
    glibc = _glibc()
    if glibc:
        glibc.mallopt(_M_MMAP_THRESHOLD, _MMAP_THRESHOLD)
        glibc.mallopt(_M_ARENA_MAX, _ARENAS)


def release() -> None:
    """Return what the arenas hold free. Worth its few milliseconds after a job, not during one."""
    glibc = _glibc()
    if glibc:
        glibc.malloc_trim(0)
