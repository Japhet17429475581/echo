"""Linux kernel isolation for untrusted local parsers: Landlock + libseccomp.

No network, parent memory access, or reads outside runtime libraries and the
random task directory. Failure is fatal; there is no unsandboxed fallback.
"""

import ctypes
import errno
import os
import signal
import sys


class SandboxUnavailable(RuntimeError):
    pass


def isolate(work):
    if sys.platform != "linux":
        raise SandboxUnavailable()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise SandboxUnavailable()
    parent = os.getppid()
    libc.prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG
    if os.getppid() != parent:
        raise SandboxUnavailable()
    landlock(libc, work)
    seccomp()


def landlock(libc, work):
    # Linux UAPI syscall numbers shared by x86_64 and aarch64.
    if os.uname().machine not in {"x86_64", "aarch64"}:
        raise SandboxUnavailable()
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise SandboxUnavailable()

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]

    all_access = (1 << 15) - 1
    read_access = 1 | 4 | 8  # EXECUTE, READ_FILE, READ_DIR
    ruleset = Ruleset(all_access)
    fd = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if fd < 0:
        raise SandboxUnavailable()
    try:
        entries = [
            ("/usr", read_access),
            ("/lib", read_access),
            ("/lib64", read_access),
            ("/opt/tools", read_access),
            ("/opt/echo", read_access),
            ("/app/.venv", read_access),
            ("/app/src", read_access),
            (os.path.dirname(__file__), read_access),
            (work, all_access),
            ("/etc/ld.so.cache", 4),
            ("/etc/magic", 4),
            ("/etc/mime.types", 4),
            ("/etc/localtime", 4),
            ("/etc/fonts", read_access),
            ("/dev/null", 2 | 4),
            ("/dev/urandom", 4),
            ("/dev/random", 4),
            ("/sys/devices/system/cpu", read_access),
        ]
        for path, access in entries:
            if not os.path.exists(path):
                continue
            path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, path_fd)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) < 0:
                    raise SandboxUnavailable()
            finally:
                os.close(path_fd)
        if libc.syscall(446, fd, 0) < 0:
            raise SandboxUnavailable()
    finally:
        os.close(fd)


def seccomp():
    try:
        lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as e:
        raise SandboxUnavailable() from e
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise SandboxUnavailable()
    try:
        for name in (
            "socket",
            "connect",
            "socketpair",
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "pidfd_getfd",
            "open_by_handle_at",
            "bpf",
            "mount",
            "umount2",
            "setns",
            "unshare",
        ):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise SandboxUnavailable()
        if lib.seccomp_load(context) != 0:
            raise SandboxUnavailable()
    finally:
        lib.seccomp_release(context)
