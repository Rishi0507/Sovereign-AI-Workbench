"""Sandbox entry point: ``python -I run.py [--rlimits k=v,...] script.py``.

Applies resource limits (dev backend only), installs the network probe, then runs the script as
``__main__``. ``python -I`` ignores PYTHON* environment variables, so the probe cannot be skipped
through PYTHONPATH or sitecustomize. Standard library only.
"""

import os
import runpy
import sys


def _apply_rlimits(spec):
    try:
        import resource
    except ImportError:
        sys.stderr.write("[sandbox] resource limits are not available on this platform\n")
        return
    names = {"cpu": resource.RLIMIT_CPU, "as": resource.RLIMIT_AS, "fsize": resource.RLIMIT_FSIZE,
             "nproc": getattr(resource, "RLIMIT_NPROC", None)}
    for item in spec.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        limit = names.get(key.strip())
        if limit is None:
            continue
        n = int(value)
        resource.setrlimit(limit, (n, n))


def main(argv):
    args = list(argv)
    if len(args) >= 2 and args[0] == "--rlimits":
        _apply_rlimits(args[1])
        args = args[2:]
    if not args:
        sys.stderr.write("usage: run.py [--rlimits spec] script.py [args]\n")
        return 2
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import sandbox_net_probe

    sandbox_net_probe.install(os.path.join(os.getcwd(), ".net_attempts.jsonl"))
    sys.path.remove(here)
    script = os.path.abspath(args[0])
    sys.argv = [script, *args[1:]]
    sys.path.insert(0, os.path.dirname(script))
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
