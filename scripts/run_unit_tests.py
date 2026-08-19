#!/usr/bin/env python3
"""Run the unit suite while rejecting every non-loopback socket destination."""

from __future__ import annotations

import ipaddress
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REAL_GETADDRINFO = socket.getaddrinfo
REAL_CONNECT = socket.socket.connect
REAL_CONNECT_EX = socket.socket.connect_ex


def _loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="strict")
    if not isinstance(host, str):
        return False
    normalized = host.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized.split("%", 1)[0])
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return bool(address.version == 6 and address.ipv4_mapped and address.ipv4_mapped.is_loopback)


def guarded_getaddrinfo(host, *args, **kwargs):
    if not _loopback(host):
        raise AssertionError(f"unit test attempted external DNS/network access: {host!r}")
    return REAL_GETADDRINFO(host, *args, **kwargs)


def guarded_connect(instance, address):
    host = address[0] if isinstance(address, tuple) and address else address
    if not _loopback(host):
        raise AssertionError(f"unit test attempted external socket access: {host!r}")
    return REAL_CONNECT(instance, address)


def guarded_connect_ex(instance, address):
    host = address[0] if isinstance(address, tuple) and address else address
    if not _loopback(host):
        raise AssertionError(f"unit test attempted external socket access: {host!r}")
    return REAL_CONNECT_EX(instance, address)


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    with (
        mock.patch.object(socket, "getaddrinfo", guarded_getaddrinfo),
        mock.patch.object(socket.socket, "connect", guarded_connect),
        mock.patch.object(socket.socket, "connect_ex", guarded_connect_ex),
    ):
        result = unittest.TextTestRunner(verbosity=2, buffer=True).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
