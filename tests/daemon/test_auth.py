"""Peer credential check abstraction.

Spec: ``2026-05-12-engram-daemon-mode-design.md`` Section 7.2.
"""

from __future__ import annotations

import os
import socket
import sys

import pytest

from engram.daemon.auth import PeerCred, check_peer_or_reject, peer_credentials
from engram.errors import PeerCredRejectError


def test_peer_credentials_same_uid_accepts() -> None:
    a, b = socket.socketpair()
    try:
        cred = peer_credentials(a.fileno())
        assert isinstance(cred, PeerCred)
        assert cred.uid == os.getuid()
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SO_PEERCRED path")
def test_linux_so_peercred_returns_struct() -> None:
    a, b = socket.socketpair()
    try:
        cred = peer_credentials(a.fileno())
        assert cred.pid > 0
        assert cred.uid == os.getuid()
        assert cred.gid == os.getgid()
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS getpeereid path")
def test_macos_getpeereid_returns_struct() -> None:
    a, b = socket.socketpair()
    try:
        cred = peer_credentials(a.fileno())
        # macOS ``getpeereid`` does not expose the peer pid; we sentinel
        # it to 0 in the abstraction.
        assert cred.pid == 0
        assert cred.uid == os.getuid()
        assert cred.gid == os.getgid()
    finally:
        a.close()
        b.close()


def test_check_peer_or_reject_passes_for_same_uid() -> None:
    a, b = socket.socketpair()
    try:
        cred = check_peer_or_reject(a.fileno())
        assert cred.uid == os.getuid()
    finally:
        a.close()
        b.close()


def test_check_peer_or_reject_raises_for_foreign_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock peer_credentials to return a foreign uid; check_peer_or_reject rejects."""
    foreign_uid = os.getuid() + 1
    monkeypatch.setattr(
        "engram.daemon.auth.peer_credentials",
        lambda fd: PeerCred(pid=42, uid=foreign_uid, gid=os.getgid()),
    )
    with pytest.raises(PeerCredRejectError) as exc_info:
        check_peer_or_reject(7)
    assert str(foreign_uid) in str(exc_info.value)
