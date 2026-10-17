"""Profile-local waveform catalog. Import never connects or drives a device.

Dungeonlab+pulse is a Coyote format, not an OVC format. Conversion below is
implemented from the public file-format description and V3 wire protocol;
editor anchor flags do not change the exported amplitude samples.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import zipfile
from pathlib import Path

from srv import WAVEFORMS_BY_DEVICE

KINDS = ('coyote', 'opossum')
MAX_FILE = 1024 * 1024
MAX_FILES = 2000
MAX_FRAMES = 35000
MAX_TOTAL_FRAMES = 500000
MAX_MANIFEST = 16 * 1024 * 1024
FREQUENCIES = (*range(10, 50), *range(50, 80, 2), *range(80, 100, 5),
               *range(100, 200, 10), 200, 233, 266, 300, 333, 366,
               *range(400, 600, 50), *range(600, 1001, 100))
# V3 section-duration slider, expressed in 100 ms units.
DURATIONS = (*range(1, 50), *range(50, 80, 2), *range(80, 100, 5),
             *range(100, 200, 10), 200, 233, 266, 300, 333, 366,
             *range(400, 600, 50), *range(600, 1000, 100),
             *range(1000, 2000, 200), 2000, 2500, 3000)


def validate_frames(frames, kind):
    if kind not in KINDS or not isinstance(frames, (list, tuple)) or not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError('波形类型或长度无效。')
    result = []
    for frame in frames:
        if not isinstance(frame, str) or not re.fullmatch(r'[0-9A-Fa-f]{16}', frame):
            raise ValueError('波形帧必须为 8 字节十六进制。')
        raw = bytes.fromhex(frame)
        if any(x > 100 for x in raw[4:]) or any(not 10 <= x <= 240 for x in raw[:4]):
            raise ValueError('波形频率或幅值超出协议范围。')
        if kind == 'opossum' and raw[:4] != b'\x0a' * 4:
            raise ValueError('负鼠波形不支持郊狼频率数据。')
        result.append(frame.upper())
    return tuple(result)


def fingerprint(kind, frames):
    return hashlib.sha256((kind + ':' + ''.join(frames)).encode('ascii')).hexdigest()


def parse_pulse(text, kind):
    text = text.strip()
    if not text.startswith('Dungeonlab+pulse:'):
        # Explicit device-tagged frame exchange format; no silent cross-device conversion.
        data = json.loads(text)
        if not isinstance(data, dict) or data.get('device_kind') != kind:
            raise ValueError('波形的设备类型不匹配。')
        return validate_frames(data.get('frames'), kind)
    if kind != 'coyote':
        raise ValueError('这是郊狼 .pulse，不能作为负鼠波形导入。')
    raw = text[len('Dungeonlab+pulse:'):]
    if '=' in raw:
        header, body = raw.split('=', 1)
        rest, speed, balance = map(int, header.split(','))
    else:
        # Earlier exports omitted the global header (also present in the sample pack).
        body, rest, speed, balance = raw, 0, 1, 16
    if not 0 <= rest <= 100 or speed not in (1, 2, 4) or not 0 <= balance <= 16:
        raise ValueError('波形休息时长、速度或平衡值无效。')
    sections = body.split('+section+')
    if not 1 <= len(sections) <= 10:
        raise ValueError('波形小节数超限。')
    samples = []
    for section in sections:
        settings, shape = section.split('/', 1)
        start, end, duration, mode, enabled = map(int, settings.split(','))
        if not (0 <= start < 84 and 0 <= end < 84 and 0 <= duration < 100
                and mode in (1, 2, 3, 4) and enabled in (0, 1)):
            raise ValueError('波形小节参数无效。')
        points = []
        for point in shape.split(','):
            value, anchor = point.split('-')
            strength = float(value)
            if not math.isfinite(strength) or not 0 <= strength <= 100 or anchor not in ('0', '1'):
                raise ValueError('波形幅值无效。')
            points.append(math.floor(strength + .5))
        if not 2 <= len(points) <= 10000:
            raise ValueError('波形形状长度无效。')
        if not enabled:
            continue
        repeats = math.ceil(DURATIONS[duration] / len(points))
        count = repeats * len(points)
        if len(samples) + count * (4 // speed) > MAX_FRAMES * 4:
            raise ValueError('波形时长超限。')
        for index in range(count):
            progress = (0 if mode == 1 else index / max(1, count - 1) if mode == 2
                        else (index % len(points)) / (len(points) - 1) if mode == 3
                        else (index // len(points)) / max(1, repeats - 1))
            period = FREQUENCIES[start] + (FREQUENCIES[end] - FREQUENCIES[start]) * progress
            encoded = period if period <= 100 else 100 + (period - 100) / 5 if period <= 600 else 200 + (period - 600) / 10
            samples.extend([(math.floor(encoded + .5), points[index % len(points)])] * (4 // speed))
    if not samples:
        raise ValueError('波形没有启用的小节。')
    # Rest is appended once at the end of the full waveform, not per section.
    samples.extend([(10, 0)] * (rest * 4))
    samples.extend([(10, 0)] * (-len(samples) % 4))
    frames = [bytes([p[0] for p in samples[i:i+4]] + [p[1] for p in samples[i:i+4]]).hex().upper()
              for i in range(0, len(samples), 4)]
    return validate_frames(frames, kind)


class WaveformLibrary:
    def __init__(self, config_dir):
        self.path = Path(config_dir) / 'waveforms' / 'library.json'
        self.data = {kind: {'custom': {}, 'enabled': list(WAVEFORMS_BY_DEVICE[kind])} for kind in KINDS}
        if self.path.exists():
            if self.path.stat().st_size > MAX_MANIFEST:
                raise ValueError('波形库文件过大。')
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self._validate(data)
            self.data = data

    @staticmethod
    def _validate(data):
        total = 0
        for kind in KINDS:
            section = data[kind]
            if not isinstance(section['custom'], dict) or len(section['custom']) > MAX_FILES:
                raise ValueError('波形库数量超限。')
            for key, entry in section['custom'].items():
                frames = validate_frames(entry['frames'], kind)
                if key != 'custom:' + fingerprint(kind, frames) or not isinstance(entry['name'], str) or not 1 <= len(entry['name']) <= 160:
                    raise ValueError('波形库记录无效。')
                total += len(frames)
            enabled = section['enabled']
            known = set(WAVEFORMS_BY_DEVICE[kind]) | set(section['custom'])
            if not isinstance(enabled, list) or not enabled or len(set(enabled)) != len(enabled) or any(x not in known for x in enabled):
                raise ValueError('可选波形列表无效。')
        if total > MAX_TOTAL_FRAMES:
            raise ValueError('波形库总时长超限。')

    def _commit(self, data):
        self._validate(data)
        payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        if len(payload.encode('utf-8')) > MAX_MANIFEST:
            raise ValueError('波形库容量超限。')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        self.data = data

    def attach(self, settings):
        result = copy.deepcopy(settings)
        result.pop('_waveform_frames', None)
        custom = {kind: {key: entry['frames'] for key, entry in self.data[kind]['custom'].items()} for kind in KINDS}
        if any(custom.values()):
            result['_waveform_frames'] = custom
        return result

    def frames(self, kind, key):
        return WAVEFORMS_BY_DEVICE[kind][key] if key in WAVEFORMS_BY_DEVICE[kind] else self.data[kind]['custom'][key]['frames']

    def catalog(self):
        return {kind: {'enabled': list(self.data[kind]['enabled']), 'items': [
            {'id': key, 'name': key if key in WAVEFORMS_BY_DEVICE[kind] else self.data[kind]['custom'][key]['name'],
             'builtin': key in WAVEFORMS_BY_DEVICE[kind], 'duration': len(self.frames(kind, key)) / 10}
            for key in (*WAVEFORMS_BY_DEVICE[kind], *self.data[kind]['custom'])]} for kind in KINDS}

    def select(self, kind, enabled, active=()):
        if kind not in KINDS or not isinstance(enabled, list):
            raise ValueError('无效的波形列表。')
        if any(key not in enabled for key in active):
            raise ValueError('请先更换通道正在使用的波形，再将它移出列表。')
        data = copy.deepcopy(self.data)
        data[kind]['enabled'] = enabled
        self._commit(data)

    def preview(self, kind, key):
        frames = self.frames(kind, key)
        samples = [value for frame in frames for value in bytes.fromhex(frame)[4:]]
        # Peak-preserving buckets: bounded IPC and preview even for long waves.
        step = max(1, math.ceil(len(samples) / 400))
        points = []
        for index in range(0, len(samples), step):
            bucket = samples[index:index+step]
            low, high = min(bucket), max(bucket)
            points.extend([low, high])
        # Exact 25 ms samples, compact enough even for the maximum-length wave.
        # Keep the bounded overview for older clients; animation must not play
        # the artificial min/max buckets as if they were real samples.
        return {'duration': len(frames) / 10, 'amplitudes': points,
                'sample_hex': bytes(samples).hex(), 'sample_interval_ms': 25,
                'frequency_hex': ''.join(frame[:8] for frame in frames) if kind == 'coyote' else None}

    def import_paths(self, kind, paths):
        if kind not in KINDS or not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES:
            raise ValueError('无效的导入路径。')
        data = copy.deepcopy(self.data)
        known = {fingerprint(kind, self.frames(kind, key)) for key in (*WAVEFORMS_BY_DEVICE[kind], *data[kind]['custom'])}
        report = {'imported': 0, 'duplicates': 0, 'failed': 0, 'errors': []}
        count, consumed = 0, 0

        def consume(name, read, size):
            nonlocal count, consumed
            count += 1
            if count > MAX_FILES or consumed + size > 32 * MAX_FILE:
                raise ValueError('本次导入数量或总大小超限。')
            consumed += size
            try:
                if size > MAX_FILE:
                    raise ValueError('单个波形文件超过 1 MB。')
                frames = parse_pulse(read().decode('utf-8-sig'), kind)
                digest = fingerprint(kind, frames)
                if digest in known:
                    report['duplicates'] += 1
                    return
                display = re.sub(r'^pulse-', '', Path(name.replace('\\', '/')).stem)
                display = re.sub(r'-\d{5,}$', '', display)[:150] or '导入波形'
                names = {key for key in WAVEFORMS_BY_DEVICE[kind]} | {item['name'] for item in data[kind]['custom'].values()}
                candidate, suffix = display, 2
                while candidate in names:
                    candidate = f'{display} ({suffix})'
                    suffix += 1
                data[kind]['custom']['custom:' + digest] = {'name': candidate, 'frames': list(frames)}
                known.add(digest)
                report['imported'] += 1
            except (ValueError, TypeError, KeyError, OSError, UnicodeError, zipfile.BadZipFile, RuntimeError) as exc:
                report['failed'] += 1
                if len(report['errors']) < 8:
                    report['errors'].append(f'{Path(name).name}: {exc}')

        for raw in paths:
            path = Path(raw)
            if path.is_symlink():
                raise ValueError('不导入符号链接。')
            candidates = path.rglob('*.pulse') if path.is_dir() else [path]
            for candidate in candidates:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                if candidate.suffix.lower() == '.zip':
                    if candidate.stat().st_size > 32 * MAX_FILE:
                        raise ValueError('ZIP 文件超过 32 MB。')
                    with zipfile.ZipFile(candidate) as archive:
                        if len(archive.infolist()) > MAX_FILES * 2:
                            raise ValueError('ZIP 文件数过多。')
                        for item in archive.infolist():
                            if not item.is_dir() and item.filename.lower().endswith('.pulse'):
                                consume(item.filename, lambda item=item: archive.read(item), item.file_size)
                elif candidate.suffix.lower() == '.pulse':
                    consume(candidate.name, candidate.read_bytes, candidate.stat().st_size)
        if count == 0:
            raise ValueError('未找到 .pulse 文件。')
        if report['imported']:
            self._commit(data)
        return report
