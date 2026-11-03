"""Live view of Meta model calls.

Every request to a Meta model (Muse Spark, Muse Wonder, Muse Image, Muse Voice Transcribe, SAM 3.1
and the open-weight Muse Spark route) goes through post() here. While a call is in flight the
dashboard shows it (GET /live, the pill in the page). Only the model name, a short task label, the
HTTP status and the timing are kept: never the prompt, the reply, the URL or any key.
"""
import itertools
import threading
import time
from collections import deque

import requests

_lock = threading.Lock()
_ids = itertools.count(1)
_active = {}                # call id -> {model, task, started}
_recent = deque(maxlen=30)  # finished calls, oldest first


def _begin(model, task):
    with _lock:
        cid = next(_ids)
        _active[cid] = {"model": model, "task": task, "started": time.time()}
    return cid


def _end(cid, status, error):
    with _lock:
        call = _active.pop(cid, None)
        if not call:
            return
        _recent.append({
            "model": call["model"],
            "task": call["task"],
            "ok": status is not None and 200 <= status < 300,
            "status": status,
            "error": error,
            "seconds": round(time.time() - call["started"], 1),
        })


def post(model, task, *args, **kwargs):
    """requests.post for a Meta model call, announced on the dashboard while it runs.

    A streamed response (stream=True) is timed until the headers arrive, not until the body ends.
    """
    cid = _begin(model, task)
    status, error = None, ""
    try:
        r = requests.post(*args, **kwargs)
        status = r.status_code
        return r
    except Exception as e:
        error = type(e).__name__
        raise
    finally:
        _end(cid, status, error)


def snapshot():
    now = time.time()
    with _lock:
        active = [{"model": c["model"], "task": c["task"], "seconds": round(now - c["started"], 1)}
                  for c in _active.values()]
        recent = [dict(c) for c in list(_recent)[-8:]]
    return {"active": active, "recent": recent}
