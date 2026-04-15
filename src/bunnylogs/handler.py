"""
BunnyLogs logging handler.

Sends log records to a BunnyLogs stream endpoint in a background thread so
that logging calls never block the caller.
"""

import logging
import queue
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import certifi

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


_STOP = object()  # sentinel that tells the worker thread to exit


class BunnyLogsHandler(logging.Handler):
    """
    A logging.Handler that ships records to a BunnyLogs live stream.

    Records are placed on an in-process queue and flushed by a daemon thread,
    so ``emit()`` is non-blocking (~microseconds).

    Usage::

        import logging
        from bunnylogs import BunnyLogsHandler

        logging.getLogger().addHandler(BunnyLogsHandler("your-uuid-here"))

    Parameters
    ----------
    uuid:
        The logspace UUID shown in your BunnyLogs stream URL.
    level:
        Minimum log level to forward (default: ``logging.NOTSET``, i.e. all).
    endpoint:
        Override the base URL (default: ``https://bunnylogs.com``).
        Useful for self-hosted deployments.
    timeout:
        HTTP request timeout in seconds (default: 5).
    """

    def __init__(
        self,
        uuid: str,
        level: int = logging.NOTSET,
        endpoint: str = "https://bunnylogs.com",
        timeout: float = 5,
    ) -> None:
        super().__init__(level)
        self._url = f"{endpoint.rstrip('/')}/live/{uuid}/"
        self._timeout = timeout
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._worker,
            name="bunnylogs-worker",
            daemon=True,  # won't block process exit
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # logging.Handler interface
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        """Put the record on the queue; returns immediately."""
        try:
            self._queue.put_nowait(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        """Flush remaining records then shut down the worker thread."""
        self._queue.put_nowait(_STOP)
        # Give the worker up to 5 s to drain before the handler is torn down.
        self._thread.join(timeout=5)
        super().close()

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            self._send(item)

    def _send(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
            data = urllib.parse.urlencode(
                {
                    "message": self.format(record),
                    "level": record.levelname,
                    "program": record.name,
                    "timestamp": ts,
                }
            ).encode()
            req = urllib.request.Request(
                self._url,
                data=data,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            urllib.request.urlopen(req, timeout=self._timeout, context=_SSL_CTX)
        except Exception:
            self.handleError(record)
