"""Waveform helpers shared by Coyote and OVC Socket devices."""

from __future__ import annotations

import re

from srv import WAVEFORMS_BY_DEVICE


_FRAME_RE = re.compile(r'^[0-9A-Fa-f]{16}$')


def load_waveform_frames(name, device_kind='coyote'):
    """Return validated frames from the selected device's official library."""
    try:
        frames = WAVEFORMS_BY_DEVICE[device_kind][name]
    except (KeyError, TypeError) as exc:
        raise ValueError(f'未知波形：{name}') from exc
    if not frames or not all(isinstance(frame, str) and _FRAME_RE.fullmatch(frame) for frame in frames):
        raise ValueError(f'波形数据无效：{name}')
    return tuple(frame.upper() for frame in frames)


def scale_coyote_frame(frame, depth):
    """Scale the four amplitude bytes of one Coyote frame by normalized depth."""
    if not isinstance(frame, str) or not _FRAME_RE.fullmatch(frame):
        raise ValueError('郊狼波形帧必须是 8 字节十六进制字符串。')
    depth = min(max(float(depth), 0.0), 1.0)
    frequency = frame[:8].upper()
    amplitudes = bytes.fromhex(frame[8:])
    scaled = bytes(min(100, max(0, round(value * depth))) for value in amplitudes)
    return frequency + scaled.hex().upper()


def frame_amplitudes(frame):
    """Return the four decimal amplitude bytes carried by a waveform frame."""
    if not isinstance(frame, str) or not _FRAME_RE.fullmatch(frame):
        raise ValueError('波形帧必须是 8 字节十六进制字符串。')
    return tuple(bytes.fromhex(frame[8:]))


def normalize_socket_frame(frame, device_type):
    """Return a Socket V4 frame suitable for the selected DG-LAB device."""
    if not isinstance(frame, str) or not _FRAME_RE.fullmatch(frame):
        raise ValueError('波形帧必须是 8 字节十六进制字符串。')
    frame = frame.upper()
    if device_type == 'OVC_1':
        # Official OVC Socket waveforms use the same eight-byte envelope as
        # Coyote V3 frames, with a fixed 0x0A prefix and four amplitude bytes.
        return '0A0A0A0A' + frame[8:]
    return frame
