"""The ``airlift`` operator CLI.

A second console script alongside the daemon entrypoint, run inside the
sidecar (``kubectl exec ... -c airlift -- airlift status``). It loads settings
through :func:`artifactory_airlift.config.load` exactly as the daemon does, so
what it reports is by construction what the daemon sees, including the env
vars that outrank the mounted ConfigMap.

The split between :mod:`.views` and :mod:`.render` is deliberate: every
command is a function returning plain dictionaries, and rendering is a thin
layer over that. A web UI or an HTTP API can call the same functions without
reimplementing any of it.
"""

from .app import main

__all__ = ["main"]
