"""`bsp dut pipe`: a raw byte pipe between stdin/stdout and a board's serial-mux socket.

The bytes are serial-mux's own protocol (length-prefixed JSON), so any serial-mux client can drive
the console through it. On a remote host the same bridge runs there under ssh as `python3 -c`
(hosts always have Python >= 3.10); this file is the single copy of that code. Stdlib only.
"""

from __future__ import annotations

import os
import shlex

BRIDGE = r'''
import os, select, socket

def bridge(path, fin, fout):
    """Copy fin -> socket and socket -> fout until either side closes."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    try:
        while True:
            ready, _, _ = select.select([fin, s], [], [])
            if s in ready:
                data = s.recv(65536)
                if not data:
                    return 0
                while data:
                    data = data[os.write(fout, data):]
            if fin in ready:
                data = os.read(fin, 65536)
                if not data:
                    return 0
                s.sendall(data)
    finally:
        s.close()
'''

_ns: dict = {}
exec(compile(BRIDGE, "<bsp-pipe>", "exec"), _ns)      # noqa: S102 - our own constant source
bridge = _ns["bridge"]


def remote_command(sock_path: str) -> str:
    """Shell command that runs the bridge on a host, connected to the ssh session's stdin/stdout."""
    code = BRIDGE + "\nimport sys\nsys.exit(bridge(sys.argv[1], 0, 1))\n"
    return f"python3 -c {shlex.quote(code)} {shlex.quote(sock_path)}"


def local(sock_path: str) -> int:
    return bridge(sock_path, 0, 1) if os.path.exists(sock_path) else 1
