# bunnylogs

Python logging handler for [BunnyLogs](https://bunnylogs.com) — ship your logs to a live stream with three lines of code.

## Install

```bash
pip install bunnylogs
```

## Usage

```python
import logging
from bunnylogs import BunnyLogsHandler

logging.getLogger().addHandler(BunnyLogsHandler("your-uuid-here"))
```

That's it. Every log record at `WARNING` and above (or whatever level your root logger is set to) will appear in your BunnyLogs stream in real time.

### Capture a specific logger

```python
import logging
from bunnylogs import BunnyLogsHandler

handler = BunnyLogsHandler("your-uuid-here")
handler.setLevel(logging.DEBUG)

logger = logging.getLogger("myapp")
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)
```

### Django — `settings.py`

```python
LOGGING = {
    "version": 1,
    "handlers": {
        "bunnylogs": {
            "class": "bunnylogs.BunnyLogsHandler",
            "uuid": "your-uuid-here",
        },
    },
    "root": {
        "handlers": ["bunnylogs"],
        "level": "WARNING",
    },
}
```

### Self-hosted deployments

```python
BunnyLogsHandler("your-uuid-here", endpoint="https://your-own-host.com")
```

## How it works

Log records are placed on an in-process queue and flushed by a daemon thread, so `emit()` is non-blocking (~microseconds on the calling thread). The background thread sends one HTTPS POST per record using only the Python standard library — no external dependencies.

On process exit the daemon thread is killed. Call `handler.close()` explicitly if you need to guarantee all queued records are flushed before shutdown.

## Parameters

| Parameter  | Default                    | Description                              |
|------------|----------------------------|------------------------------------------|
| `uuid`     | —                          | Your logspace UUID                       |
| `level`    | `logging.NOTSET`           | Minimum level to forward                 |
| `endpoint` | `https://bunnylogs.com`    | Base URL (override for self-hosted)      |
| `timeout`  | `5`                        | HTTP request timeout in seconds          |

## License

MIT
