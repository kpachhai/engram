"""Peer credential check for daemon UDS connections.

Linux: ``SO_PEERCRED`` returns ``struct ucred = (pid, uid, gid)``.
macOS: ``getpeereid(fd)`` returns ``(uid, gid)``; no pid (we sentinel to 0).

Spec: ``2026-05-12-engram-daemon-mode-design.md`` Section 7.2.

The peer-credential check is belt-and-suspenders on top of UDS filesystem
permissions (mode 0600). Filesystem perms already prevent non-owner
access; ``SO_PEERCRED`` / ``getpeereid`` guard the weird-mount scenarios
(NFS / chroot / shared-uid container) where filesystem perms might be
misleading. Combined, the guard is strictly stronger than either alone.
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
import sys
from dataclasses import dataclass

from engram.errors import PeerCredRejectError

# Linux's ``SO_PEERCRED`` socket option is 17.
_SO_PEERCRED_LINUX = 17
# ``struct ucred`` is three ``int32`` fields (pid, uid, gid) packed in
# little-endian on every Linux ABI engram targets.
_UCRED_FMT = "iii"
_UCRED_SIZE = struct.calcsize(_UCRED_FMT)


@dataclass(frozen=True)
class PeerCred:
    """Peer credentials of a UDS connection.

    ``pid`` is 0 on macOS (``getpeereid`` does not expose it). Callers
    that need a real PID for diagnostics should only rely on it when
    ``pid != 0``.
    """

    pid: int
    uid: int
    gid: int


def peer_credentials(fd: int) -> PeerCred:
    """Return the (pid, uid, gid) of the peer connected on ``fd``.

    Caller is expected to compare ``cred.uid`` against :func:`os.getuid`
    and refuse connections from foreign UIDs. Use :func:`check_peer_or_reject`
    for the combined fetch + check.
    """
    platform = sys.platform
    if platform.startswith("linux"):  # pragma: no cover - Linux-only path
        data = _getsockopt(fd, socket.SOL_SOCKET, _SO_PEERCRED_LINUX, _UCRED_SIZE)
        pid, uid, gid = struct.unpack(_UCRED_FMT, data)
        return PeerCred(pid=pid, uid=uid, gid=gid)

    if platform == "darwin":  # pragma: no cover - macOS-only path
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        c_uid = ctypes.c_uint32()
        c_gid = ctypes.c_uint32()
        rc = libc.getpeereid(fd, ctypes.byref(c_uid), ctypes.byref(c_gid))
        if rc != 0:
            errno = ctypes.get_errno()
            msg = f"getpeereid failed: errno={errno}"
            raise OSError(errno, msg)
        return PeerCred(pid=0, uid=c_uid.value, gid=c_gid.value)

    msg = f"peer_credentials not supported on platform {platform}"  # pragma: no cover
    raise NotImplementedError(msg)  # pragma: no cover


def check_peer_or_reject(fd: int) -> PeerCred:
    """Return the peer cred if it matches the daemon's UID; raise otherwise.

    Raises :class:`engram.errors.PeerCredRejectError` when the peer's UID
    differs from the daemon's. The caller's accept loop logs the rejection
    and closes the connection.
    """
    cred = peer_credentials(fd)
    if cred.uid != os.getuid():
        msg = f"peer uid={cred.uid} does not match daemon uid={os.getuid()}"
        raise PeerCredRejectError(msg)
    return cred


def _getsockopt(fd: int, level: int, opt: int, size: int) -> bytes:
    """``getsockopt`` against a duped FD so the caller's FD is never touched.

    :func:`socket.fromfd` duplicates ``fd``; we own + close the dup; the
    original fd (typically the daemon's accept-loop socket) is left
    untouched. The dup-and-close pattern guards against accidentally
    closing the daemon's own listening socket from inside an asyncio
    accept callback.
    """
    dup_sock = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        return dup_sock.getsockopt(level, opt, size)
    finally:
        dup_sock.close()


__all__ = ["PeerCred", "check_peer_or_reject", "peer_credentials"]
