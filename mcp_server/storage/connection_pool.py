"""
Thread-safe bounded SQLite connection pool.

The factory callable must return connections opened with
``check_same_thread=False``; failure to do so will raise
``sqlite3.ProgrammingError`` when connections are used across threads.
"""

import sqlite3
import threading
from collections import deque
from contextlib import contextmanager
from typing import Callable, Iterator


class ConnectionPool:
    """Bounded pool of ``sqlite3.Connection`` objects.

    Connections are pre-created at construction time using *factory*.
    ``acquire()`` blocks until a connection is available, then returns it
    to the pool on exit.  After ``close_all()`` any call to ``acquire()``
    raises ``RuntimeError`` immediately rather than blocking forever.

    The factory must return connections with ``check_same_thread=False``.
    """

    def __init__(self, factory: Callable[[], sqlite3.Connection], size: int = 4):
        if size < 1:
            raise ValueError("ConnectionPool size must be positive")
        self._factory = factory
        self._size = size
        self._pool = deque()
        self._condition = threading.Condition()
        self._closed = False
        try:
            for _ in range(size):
                self._pool.append(factory())
        except BaseException:
            self.close_all()
            raise

    @contextmanager
    def acquire(self) -> Iterator[sqlite3.Connection]:
        with self._condition:
            while not self._pool and not self._closed:
                self._condition.wait()
            if self._closed:
                raise RuntimeError("ConnectionPool is closed; cannot acquire connection")
            conn = self._pool.popleft()
        try:
            yield conn
        finally:
            with self._condition:
                if not self._closed:
                    self._pool.append(conn)
                    self._condition.notify()
                else:
                    conn.close()

    def close_all(self) -> None:
        """Drain the pool and close every connection.  Idempotent."""
        with self._condition:
            self._closed = True
            idle = list(self._pool)
            self._pool.clear()
            self._condition.notify_all()
        for conn in idle:
            try:
                conn.close()
            except sqlite3.Error:
                pass
