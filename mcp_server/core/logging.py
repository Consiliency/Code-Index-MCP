"""Logging configuration for MCP Server."""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from mcp_server.config.environment import Environment, get_environment


class _PrivateTransportFilter(logging.Filter):
    """Keep framework diagnostics from repeating client-controlled payloads."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name.endswith(".access")
            and isinstance(record.args, tuple)
            and len(record.args) == 5
        ):
            args = list(record.args)
            try:
                args[2] = urlsplit(str(args[2])).path
            except ValueError:
                args[2] = "<invalid-target>"
            record.args = tuple(args)
        else:
            # SDK errors can embed whole invalid JSON messages before our handler runs.
            record.msg = "Transport diagnostic in %s (%s)"
            record.args = (record.funcName, record.levelname)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_private_diagnostics() -> None:
    """Install content-free SDK and query-free HTTP access diagnostics."""
    for name in ("uvicorn.access", "gunicorn.access", "mcp.server.lowlevel.server"):
        target = logging.getLogger(name)
        if not any(isinstance(item, _PrivateTransportFilter) for item in target.filters):
            target.addFilter(_PrivateTransportFilter())


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        # Forward any extra fields set on the record
        skip = {
            "name",
            "msg",
            "args",
            "created",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in skip:
                payload[key] = value
        return json.dumps(payload)


def _use_json_logging() -> bool:
    return get_environment() == Environment.PRODUCTION or os.getenv("MCP_LOG_FORMAT") == "json"


def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None) -> None:
    """Configure logging for the MCP Server."""
    configure_private_diagnostics()
    if log_file is None:
        log_file = "mcp_server.log"

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    if _use_json_logging():
        formatter: logging.Formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(log_path, mode="a", encoding="utf-8"),
    ]
    for h in handlers:
        h.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    # Replace existing handlers so repeated calls in tests don't stack
    root.handlers = handlers

    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("fastapi").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized - Level: {log_level}, File: {log_path}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a configured logger instance.

    Args:
        name: Name for the logger (typically __name__)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)
