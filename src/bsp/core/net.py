"""Best-effort local addresses of this machine (stdlib only)."""

from __future__ import annotations

import socket


def local_addresses() -> list[str]:
    """Global addresses of this machine: the default route's source first, then hostname lookups."""
    out: list[str] = []
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None):
            if fam in (socket.AF_INET, socket.AF_INET6):
                out.append(sa[0])
    except OSError:
        pass
    try:                                   # the source address of the default route
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            out.insert(0, s.getsockname()[0])
    except OSError:
        pass
    return [a for a in dict.fromkeys(out) if not a.startswith(("127.", "::1", "fe80"))]
