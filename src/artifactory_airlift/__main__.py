import sys

from . import config, log


def main() -> int:
    settings = config.load()
    log.configure(settings.log_level)
    logger = log.get("artifactory_airlift")
    logger.info(
        "startup",
        mode=settings.mode,
        instance=settings.instance_name,
        artifactory_url=settings.artifactory_url,
        cycle_seconds=settings.cycle_seconds,
    )

    if settings.mode == "sender":
        from . import sender
        return sender.run(settings)

    if settings.mode == "receiver":
        from . import receiver
        return receiver.run(settings)

    logger.error("unknown_mode", mode=settings.mode)
    return 2

if __name__ == "__main__":
    sys.exit(main())
