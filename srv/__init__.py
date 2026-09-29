import json
from threading import RLock

from .official_waveforms import (
    COYOTE_WAVEFORMS,
    OPOSSUM_WAVEFORMS,
    WAVEFORM_NAMES_BY_DEVICE,
    WAVEFORMS_BY_DEVICE,
)


WS_CONNECTIONS = set()
WS_CONNECTIONS_LOCK = RLock()


def add_ws_connection(connection):
    with WS_CONNECTIONS_LOCK:
        WS_CONNECTIONS.add(connection)


def remove_ws_connection(connection):
    with WS_CONNECTIONS_LOCK:
        WS_CONNECTIONS.discard(connection)


def get_ws_connections():
    """Return a stable snapshot that is safe to iterate from other threads."""
    with WS_CONNECTIONS_LOCK:
        return tuple(WS_CONNECTIONS)


# Compatibility aliases for integrations written before per-device libraries.
WAVEFORM_NAMES = WAVEFORM_NAMES_BY_DEVICE['coyote']
WAVEFORMS = {
    name: json.dumps(frames, separators=(',', ':'))
    for name, frames in COYOTE_WAVEFORMS.items()
}
waveData = [WAVEFORMS[name] for name in WAVEFORM_NAMES]
DEFAULT_WAVE = waveData[0]
