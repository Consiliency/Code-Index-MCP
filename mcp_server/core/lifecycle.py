"""Bound retirement when an in-process worker cannot be safely abandoned."""

import os
import threading
from contextlib import contextmanager
from typing import Iterator

_RETIREMENT_GRACE_SECONDS = 5.0


@contextmanager
def retirement_watchdog() -> Iterator[None]:
    """Exit the service if retirement hangs or fails; cancel only after success."""
    watchdog = threading.Timer(_RETIREMENT_GRACE_SECONDS, os._exit, args=(1,))
    watchdog.daemon = True
    watchdog.start()
    try:
        yield
    except BaseException:
        # Startup failure does not guarantee that an ASGI shutdown hook will run.
        raise
    else:
        watchdog.cancel()
        watchdog.join()
