"""Bound subprocess observation while leaving provider accounting untouched."""

import os
import selectors
import subprocess
import time

STDOUT_LIMIT = 262144
STDERR_LIMIT = 65536


def collect(process, *, timeout, tick=None):
    """Drain both pipes incrementally; cap memory even for an endless producer.

    Stopping this CLI observer is not a provider cancellation or cleanup proof.
    Callers must retain any admitted/uncertain reservation after failure.
    The deadline bounds acceptance. A callback must bound its own latency;
    it cannot be interrupted here, so caller return may also include that
    callback's maximum duration and up to five seconds of observer teardown.
    """
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    caps = {"stdout": STDOUT_LIMIT, "stderr": STDERR_LIMIT}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("Broker observation deadline exceeded; retained work requires reconciliation.")
                if tick is not None:
                    tick()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("Broker observation deadline exceeded; retained work requires reconciliation.")
                if not selector.get_map():
                    try:
                        process.wait(timeout=min(remaining, 0.25))
                    except subprocess.TimeoutExpired:
                        pass
                    continue
                for key, _ in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    room = caps[name] - len(buffers[name])
                    if len(chunk) > room:
                        raise ValueError("The broker returned an oversized response; retained work requires reconciliation.")
                    buffers[name].extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Broker observation deadline exceeded; retained work requires reconciliation.")
            process.wait(timeout=remaining)
            if time.monotonic() >= deadline:
                raise ValueError("Broker observation deadline exceeded; retained work requires reconciliation.")
        return bytes(buffers["stdout"]), bytes(buffers["stderr"])
    finally:
        try:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        finally:
            process.stdout.close()
            process.stderr.close()
