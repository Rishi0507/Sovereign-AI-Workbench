"""Network probe loaded inside the sandbox before agent code runs.

Every attempt to open an IP connection or resolve a name is written to ``.net_attempts.jsonl``
in the job directory and then refused, before any system call is made. Standard library only.
"""

import errno
import json
import os
import socket
import time

_LOG = os.path.join(os.getcwd(), ".net_attempts.jsonl")
_INSTALLED = False


def _log(kind, addr):
    try:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "kind": kind, "addr": str(addr)}) + "\n")
    except OSError:
        pass


def _format(address):
    if isinstance(address, tuple) and len(address) >= 2:
        host, port = address[0], address[1]
        return f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"
    return str(address)


def _refuse(kind, address):
    _log(kind, _format(address))
    raise OSError(errno.ENETUNREACH, "Network is unreachable (sandbox has no network)")


def install(log_path=None):
    global _INSTALLED, _LOG
    if log_path:
        _LOG = log_path
    if _INSTALLED:
        return
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(self, address):
        if self.family == getattr(socket, "AF_UNIX", object()):
            return original_connect(self, address)
        _refuse("connect", address)

    def connect_ex(self, address):
        if self.family == getattr(socket, "AF_UNIX", object()):
            return original_connect_ex(self, address)
        _log("connect", _format(address))
        return errno.ENETUNREACH

    def create_connection(address, *args, **kwargs):
        _refuse("connect", address)

    def getaddrinfo(host, port, *args, **kwargs):
        _log("dns", f"{host}:{port}")
        raise socket.gaierror(socket.EAI_NONAME, "Name resolution disabled in the sandbox")

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    socket.getaddrinfo = getaddrinfo
    _INSTALLED = True


def installed():
    return _INSTALLED
