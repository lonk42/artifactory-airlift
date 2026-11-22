import signal
import sys
import time
from typing import NoReturn

from . import config, log, reload

# How often a parked process restates why it is idle. Long enough not to bury
# the surrounding logs, short enough that an operator tailing the sidecar sees
# the reason without waiting.
_PARK_INTERVAL_SECONDS = 300


class _Terminated(BaseException):
    """Raised by the SIGTERM handler to unwind to main().

    A BaseException so no ``except Exception`` on the way can swallow it,
    while ``finally`` blocks (the state lock, open clients) still run.
    """


def _on_sigterm(signum, frame) -> None:
    # Nothing but the raise: logging from inside a signal handler can re-enter
    # a stream write the main thread was in the middle of.
    raise _Terminated


def _park(
    logger, event: str, *, watcher: reload.ConfigWatcher | None = None, **fields
) -> NoReturn:
    """Stay alive, restating a fatal error, instead of exiting.

    Airlift runs as a sidecar next to Artifactory. A container that exits is
    restarted, and a container that keeps exiting holds the whole pod out of
    Ready and blocks the StatefulSet rollout, so a mistake in airlift's own
    configuration would stop Artifactory from being scheduled. Airlift is not
    important enough to do that: it parks, says why on a loop, and leaves the
    pod healthy. SIGTERM still ends it.

    This is for errors no retry can clear (invalid settings, an unknown mode).
    Anything that might fix itself is retried in the cycle loops instead.
    With a watcher, a valid edit to the config file starts over in place, so
    correcting a bad ConfigMap needs no restart either.
    """
    while True:
        logger.error(event, **fields)
        if watcher is None:
            time.sleep(_PARK_INTERVAL_SECONDS)
            continue
        try:
            watcher.wait(_PARK_INTERVAL_SECONDS, current=None)
        except reload.ReloadRequested:
            reload.reexec()


def main() -> int:
    # Python as PID 1 has no SIGTERM handler by default, and the kernel drops
    # signals to PID 1 that it does not handle. Without this, Kubernetes waits
    # out the pod's whole termination grace period and then SIGKILLs airlift,
    # holding Artifactory's pod in Terminating for that long.
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        return _run()
    except _Terminated:
        log.get("artifactory_airlift").info("shutdown", signal="SIGTERM")
        return 0


def _run() -> int:
    watcher = reload.ConfigWatcher()
    try:
        settings = config.load()
    except Exception as exc:
        # Logging is not configured yet, so configure it at the default level
        # purely to report this.
        log.configure(None)
        _park(
            log.get("artifactory_airlift"),
            "config_invalid",
            watcher=watcher,
            error=str(exc),
        )

    log.configure(settings.log_level)
    logger = log.get("artifactory_airlift")
    logger.info(
        "startup",
        mode=settings.mode,
        instance=settings.instance_name,
        artifactory_url=settings.artifactory_url,
        cycle_seconds=settings.cycle_seconds,
    )

    try:
        if settings.mode == "sender":
            from . import sender

            rc = sender.run(settings, watcher=watcher)
        elif settings.mode == "receiver":
            from . import receiver

            rc = receiver.run(settings, watcher=watcher)
        else:
            _park(logger, "unknown_mode", watcher=watcher, mode=settings.mode)
    except reload.ReloadRequested:
        # The loop has unwound: state lock released, clients closed.
        reload.reexec()
    except Exception as exc:
        # A bug in airlift is still no reason to stop Artifactory. The
        # traceback is logged in full first, so nothing is hidden.
        logger.exception("fatal")
        _park(
            logger,
            "parked_after_fatal",
            watcher=watcher,
            error=str(exc),
            mode=settings.mode,
        )

    # The run loops only return on a condition that will not clear by itself
    # (another process holds the state lock). Park on it rather than handing
    # the container runtime a non-zero exit to restart forever.
    if rc != 0:
        _park(logger, "parked_after_exit", watcher=watcher, code=rc, mode=settings.mode)
    return rc


if __name__ == "__main__":
    sys.exit(main())
