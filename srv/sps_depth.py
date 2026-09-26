"""SPS/OGB avatar discovery and normalized penetration depth calculation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


AVATAR_PARAMETER_PREFIX = '/avatar/parameters/'
OGB_PARAMETER_PREFIX = f'{AVATAR_PARAMETER_PREFIX}OGB/'
SPS_SOCKET = 'sps_socket'
SPS_PLUG = 'sps_plug'
SPS_TRIGGER_TYPES = (SPS_SOCKET, SPS_PLUG)


@dataclass(frozen=True)
class SPSZone:
    kind: str
    zone_id: str
    label: str


@dataclass(frozen=True)
class AvatarSPSConfig:
    avatar_id: str
    avatar_name: str
    path: Path
    sockets: tuple[SPSZone, ...]
    plugs: tuple[SPSZone, ...]


def _zone_from_parameter(parameter):
    name = parameter.get('name')
    output = parameter.get('output') or {}
    address = output.get('address')
    if not isinstance(name, str) or not isinstance(address, str):
        return None
    name_parts = name.split('/')
    address_parts = address.split('/')
    if len(name_parts) < 4 or len(address_parts) < 7:
        return None
    if name_parts[0] != 'OGB' or name_parts[1] not in ('Orf', 'Pen'):
        return None
    if not address.startswith(OGB_PARAMETER_PREFIX):
        return None
    kind = SPS_SOCKET if name_parts[1] == 'Orf' else SPS_PLUG
    return SPSZone(kind=kind, zone_id=address_parts[5], label=name_parts[2])


def load_avatar_sps_config(path):
    """Read one VRChat avatar OSC JSON file and return its OGB zones."""
    path = Path(path)
    with path.open('r', encoding='utf-8-sig') as stream:
        document = json.load(stream)
    zones = {}
    for parameter in document.get('parameters', []):
        zone = _zone_from_parameter(parameter)
        if zone is not None:
            zones[(zone.kind, zone.zone_id)] = zone
    sockets = tuple(sorted(
        (zone for zone in zones.values() if zone.kind == SPS_SOCKET),
        key=lambda item: item.label.casefold(),
    ))
    plugs = tuple(sorted(
        (zone for zone in zones.values() if zone.kind == SPS_PLUG),
        key=lambda item: item.label.casefold(),
    ))
    return AvatarSPSConfig(
        avatar_id=str(document.get('id') or path.stem),
        avatar_name=str(document.get('name') or path.stem),
        path=path,
        sockets=sockets,
        plugs=plugs,
    )


def avatar_osc_search_roots(app_dir=None):
    roots = []
    local_app_data = os.environ.get('USERPROFILE')
    if local_app_data:
        roots.append(
            Path(local_app_data)
            / 'AppData' / 'LocalLow' / 'VRChat' / 'VRChat' / 'OSC'
        )
    if app_dir is not None:
        roots.append(Path(app_dir))
    unique = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def find_avatar_sps_config(avatar_id, app_dir=None):
    """Load the OSC JSON matching an avatar ID reported by ``/avatar/change``."""
    avatar_id = str(avatar_id).strip()
    if not avatar_id.startswith('avtr_') or any(char in avatar_id for char in ('/', '\\')):
        return None
    filename = f'{avatar_id}.json'
    candidates = []
    app_root = Path(app_dir).resolve() if app_dir is not None else None
    for root in avatar_osc_search_roots(app_dir):
        try:
            if not root.exists():
                continue
            paths = root.glob(filename) if root == app_root else root.rglob(filename)
            for path in paths:
                candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _mtime, path in sorted(candidates, reverse=True):
        try:
            return load_avatar_sps_config(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return None


def sps_osc_bindings(trigger_type, zone_id):
    """Return ``(OSC address, signal name)`` pairs for one selected SPS zone."""
    if trigger_type not in SPS_TRIGGER_TYPES:
        raise ValueError(f'不支持的 SPS 触发类型：{trigger_type}')
    zone_id = str(zone_id).strip()
    if not zone_id or '/' in zone_id:
        raise ValueError('SPS 部位 ID 无效。')
    ogb_kind = 'Orf' if trigger_type == SPS_SOCKET else 'Pen'
    base = f'{OGB_PARAMETER_PREFIX}{ogb_kind}/{zone_id}'
    if trigger_type == SPS_SOCKET:
        signals = (
            'PenSelfNewRoot',
            'PenSelfNewTip',
            'PenOthersNewRoot',
            'PenOthersNewTip',
            'PenSelf',
            'PenOthers',
        )
    else:
        signals = ('PenSelf', 'PenOthers')
    return tuple((f'{base}/{signal}', signal) for signal in signals)


class _PenetratorLengthDetector:
    """Port of OGB's stable root/tip length estimator."""

    def __init__(self):
        self.length = None
        self.recent_samples = []
        self.penetrating_sample = None

    def _recalculate(self):
        if len(self.recent_samples) < 4:
            self.length = self.penetrating_sample
            return
        samples = sorted(self.recent_samples)
        closest_index = min(
            range(1, len(samples)),
            key=lambda index: abs(samples[index] - samples[index - 1]),
        )
        self.length = samples[closest_index]

    def update(self, root_proximity, tip_proximity):
        if root_proximity is None or tip_proximity is None:
            return
        if root_proximity < 0.01 or tip_proximity < 0.01:
            self.recent_samples.clear()
            self.penetrating_sample = None
            self.length = None
            return
        if root_proximity > 0.95:
            return
        length = tip_proximity - root_proximity
        if length < 0.02:
            return
        if tip_proximity > 0.99:
            if self.penetrating_sample is None or length > self.penetrating_sample:
                self.penetrating_sample = length
                self._recalculate()
            return
        self.recent_samples.insert(0, length)
        del self.recent_samples[8:]
        self._recalculate()


class SPSDepthCalculator:
    """Collect a selected OGB zone's signals and expose a 0..1 depth."""

    def __init__(self, trigger_type):
        if trigger_type not in SPS_TRIGGER_TYPES:
            raise ValueError(f'不支持的 SPS 触发类型：{trigger_type}')
        self.trigger_type = trigger_type
        self.values = {}
        self.detectors = {
            'Self': _PenetratorLengthDetector(),
            'Others': _PenetratorLengthDetector(),
        }

    @staticmethod
    def _clamp(value):
        return min(max(float(value), 0.0), 1.0)

    def update(self, signal, value):
        self.values[signal] = self._clamp(value)
        if self.trigger_type == SPS_SOCKET:
            for owner in ('Self', 'Others'):
                if signal in (f'Pen{owner}NewRoot', f'Pen{owner}NewTip'):
                    self.detectors[owner].update(
                        self.values.get(f'Pen{owner}NewRoot'),
                        self.values.get(f'Pen{owner}NewTip'),
                    )
        return self.depth

    def _new_socket_depth(self, owner):
        root = self.values.get(f'Pen{owner}NewRoot')
        tip = self.values.get(f'Pen{owner}NewTip')
        if root is None or tip is None or (root <= 0 and tip <= 0):
            return None
        length = self.detectors[owner].length
        if length and tip > 0.99:
            exposed_ratio = (1 - root) / length
            return self._clamp(1 - exposed_ratio)
        return 0.0

    def _socket_depth(self, owner):
        new_depth = self._new_socket_depth(owner)
        if new_depth is not None:
            return new_depth
        return self.values.get(f'Pen{owner}', 0.0)

    @property
    def depth(self):
        if self.trigger_type == SPS_SOCKET:
            return max(self._socket_depth('Self'), self._socket_depth('Others'))
        return max(self.values.get('PenSelf', 0.0), self.values.get('PenOthers', 0.0))
