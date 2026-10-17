"""Measured local stages, not estimates of physical-device latency."""
import threading
import time
import math
from loguru import logger

_lock = threading.Lock()
_stages = {}


def brand_log_record(record):
    if record['name'] == 'shocking_vrchat':
        record['name'] = 'neko-vrc'


def websocket_latency(connection, request_ack=None):
    """App-only RTT, never a claim about the phone's Bluetooth device link."""
    if request_ack is not None:
        milliseconds, observed_at = request_ack
        if 0 <= time.perf_counter() - observed_at < 30:
            return {'kind': 'app_ack', 'milliseconds': round(milliseconds, 2)}
    seconds = getattr(connection, 'latency', None)
    # websockets starts at zero before its first Ping/Pong. Mocks, NaN and
    # absent samples must not turn into a fabricated zero-ms measurement.
    if type(seconds) in (float, int) and math.isfinite(seconds) and seconds > 0:
        return {'kind': 'app_ping', 'milliseconds': round(seconds * 1000, 2)}
    return None


def record_latency(stage, started):
    now = time.perf_counter()
    milliseconds = max(0.0, (now - started) * 1000)
    with _lock:
        count, maximum, last_log, last_value = _stages.get(stage, (0, 0, 0, None))
        count += 1
        maximum = max(maximum, milliseconds)
        value = round(milliseconds, 1)
        publish = now - last_log >= 5 and value != last_value
        _stages[stage] = (count, maximum, now if publish else last_log, value if publish else last_value)
    if publish:
        logger.info('延迟 stage={} last_ms={:.3f} max_ms={:.3f} samples={}', stage, milliseconds, maximum, count)
    return milliseconds
