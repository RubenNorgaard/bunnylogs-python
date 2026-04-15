"""
BunnyLogs logging handler.

Sends log records to a BunnyLogs stream endpoint in a background thread so
that logging calls never block the caller.

A persistent HTTPSConnection is kept alive inside the worker thread and
reused across records, avoiding a TCP + TLS handshake on every emit().
The connection is re-established automatically after any network error.
"""

import http.client
import logging
import queue
import ssl
import threading
import urllib.parse
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
        parsed = urllib.parse.urlparse(endpoint)
        self._scheme = parsed.scheme or "https"
        self._host = parsed.netloc or parsed.path
        self._path = f"/live/{uuid}"
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
        self._thread.join(timeout=5)
        super().close()

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _make_conn(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(
                self._host, context=_SSL_CTX, timeout=self._timeout
            )
        return http.client.HTTPConnection(self._host, timeout=self._timeout)

    def _worker(self) -> None:
        conn: http.client.HTTPConnection | None = None
        while True:
            item = self._queue.get()
            if item is _STOP:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                return
            conn = self._send(item, conn)

    def _send(
        self,
        record: logging.LogRecord,
        conn: "http.client.HTTPConnection | None",
    ) -> "http.client.HTTPConnection | None":
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

            if conn is None:
                conn = self._make_conn()

            conn.request(
                "POST",
                self._path,
                body=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(data)),
                },
            )
            resp = conn.getresponse()
            resp.read()  # drain so the connection can be reused
            return conn

        except http.client.HTTPException:
            # Protocol error or server closed the connection — reconnect next send
            try:
                conn.close()
            except Exception:
                pass
            return None

        except Exception:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            self.handleError(record)
            return None
