"""Pure DG-LAB BLE encoders. Do not reuse Socket envelopes on the wire.

Reference: dungeonlab-open/dglab-bluetooth-protocol (Coyote V2/V3, OVC V1).
"""

import math


def uuid16(value):
    return f'0000{value:04x}-0000-1000-8000-00805f9b34fb'


def v2_uuid(value):
    return f'955a{value:04x}-0fe2-f5aa-a094-84b8d4f3e8ad'


WRITE_UUID = uuid16(0x150A)
NOTIFY_UUID = uuid16(0x150B)
BATTERY_UUID = uuid16(0x1500)
V2_STRENGTH_UUID = v2_uuid(0x1504)
# The official V2 table assigns PWM_A34 to B, and PWM_B34 to A.
V2_WAVE_UUID = {'A': v2_uuid(0x1506), 'B': v2_uuid(0x1505)}
ZERO_FRAME = '0A0A0A0A00000000'
MODELS = {
    '47L121000': 'coyote3',
    '47L127000': 'opossum',
    'D-LAB ESTIM01': 'coyote2',
}


def strength(value):
    if type(value) is not int or not 0 <= value <= 200:
        raise ValueError('通道强度必须是 0~200 的整数。')
    return value


def frame_bytes(frame):
    if not isinstance(frame, str) or len(frame) != 16:
        raise ValueError('波形必须是 8 字节。')
    raw = bytes.fromhex(frame)
    if len(raw) != 8 or any(value > 100 for value in raw[4:]):
        raise ValueError('波形幅值必须为 0~100。')
    return raw


def coyote_packet(a, b, strength_a, strength_b, sequence=0, change_strength=True):
    a, b = frame_bytes(a), frame_bytes(b)
    if any(not 10 <= value <= 240 for value in a[:4] + b[:4]):
        raise ValueError('郊狼 3.0 编码频率必须为 10~240。')
    if type(sequence) is not int or not 0 <= sequence <= 15:
        raise ValueError('郊狼序列号必须为 0~15。')
    return bytes((0xB0, sequence << 4 | (0x0F if change_strength else 0), strength(strength_a), strength(strength_b))) + a + b


def coyote_limits(a, b, frequency=(0, 0), amplitude=(0, 0)):
    values = tuple(frequency) + tuple(amplitude)
    if len(values) != 4 or any(type(v) is not int or not 0 <= v <= 255 for v in values):
        raise ValueError('平衡参数必须为 0~255。')
    return bytes((0xBF, strength(a), strength(b), *values))


def opossum_packet(a, b):
    return bytes((0xB0,)) + bytes(7) + frame_bytes(a)[4:] + bytes(4) + frame_bytes(b)[4:]


def opossum_strength(a, b):
    return bytes((0xB3, strength(a), strength(b)))


def opossum_display(a, b):
    # The official literal exceeds its documented 20-byte WRITE size. Send as
    # ONE packet only when negotiated MTU permits it, never split the command.
    return bytes((0xB2,)) + bytes.fromhex('FFFF00FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF0809') + bytes((strength(a), strength(b)))


def v2_strength(a, b):
    return ((strength(a) * 7 << 11) | strength(b) * 7).to_bytes(3, 'little')


def v2_wave(frame):
    raw = frame_bytes(frame)
    if any(not 10 <= value <= 240 for value in raw[:4]):
        raise ValueError('波形频率无效。')
    periods = [v if v <= 100 else (v - 100) * 5 + 100 if v <= 200 else (v - 200) * 10 + 600
               for v in raw[:4]]
    # V2 holds ONE XYZ group per 100ms, unlike V3's four 25ms groups.
    # Approximate the existing library by averaging within the 100ms frame.
    period = round(sum(periods) / 4)
    x = min(31, max(1, round(math.sqrt(period / 1000) * 15)))
    y = min(1023, max(0, period - x))
    z = min(20, round(sum(raw[4:]) / 4 * 20 / 100))
    return (x | y << 5 | z << 15).to_bytes(3, 'little')
