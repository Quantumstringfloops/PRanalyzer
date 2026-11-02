import logging
import sys

import structlog


def setup_logging(level: str = "WARNING") -> None:
    # logs go to stderr so stdout carries only the report (keeps --output json parseable)
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        numeric_level = logging.WARNING

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )
