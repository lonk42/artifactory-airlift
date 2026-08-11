import sys
import time
from typing import NoReturn

from . import config, log

# How often a parked process restates why it is idle. Long enough not to bury
# the surrounding logs, short enough that an operator tailing the sidecar sees
# the reason without waiting.
_PARK_INTERVAL_SECONDS = 300


def _park(logger, event: str, **fields) -> NoReturn:
    """Stay alive, restating a fatal error, instead of exiting.

    Airlift runs as a sidecar next to Artifactory. A container that exits is
    restarted, and a container that keeps exiting holds the whole pod out of
    Ready and blocks the StatefulSet rollout, so a mistake in airlift's own
    configuration would stop Artifactory from being scheduled. Airlift is not
    important enough to do that: it parks, says why on a loop, and leaves the
    pod healthy. Kubernetes still terminates the process on SIGTERM.

    This is for errors no retry can clear (invalid settings, an unknown mode).
    Anything that might fix itself is retried in the cycle loops instead.
    """
    while True:
        logger.error(event, **fields)
        time.sleep(_PARK_INTERVAL_SECONDS)


def main() -> int:
    try:
        settings = config.load()
    except Exception as exc:
        # Logging is not configured yet, so configure it at the default level
        # purely to report this.
        log.configure(None)
        _park(log.get("artifactory_airlift"), "config_invalid", error=str(exc))

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

            rc = sender.run(settings)
        elif settings.mode == "receiver":
            from . import receiver

            rc = receiver.run(settings)
        else:
            _park(logger, "unknown_mode", mode=settings.mode)
    except Exception as exc:
        # A bug in airlift is still no reason to stop Artifactory. The
        # traceback is logged in full first, so nothing is hidden.
        logger.exception("fatal")
        _park(logger, "parked_after_fatal", error=str(exc), mode=settings.mode)

    # The run loops only return on a condition that will not clear by itself
    # (another process holds the state lock). Park on it rather than handing
    # the container runtime a non-zero exit to restart forever.
    if rc != 0:
        _park(logger, "parked_after_exit", code=rc, mode=settings.mode)
    return rc


if __name__ == "__main__":
    sys.exit(main())
