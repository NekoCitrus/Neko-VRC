import copy
import os
import re
import secrets
import uuid
from pathlib import Path

import yaml

from srv import WAVEFORM_NAMES_BY_DEVICE


# Keep the legacy data directory so existing users retain their settings and
# SteamVR manifest when upgrading to the renamed application.
APP_NAME = 'ShockingVRChat'
PRODUCT_NAME = 'Neko-VRC'
CONFIG_FILE_VERSION = 'v0.9'
CONFIG_FILENAME = f'settings-{CONFIG_FILE_VERSION}.yaml'

DEFAULT_EXTRA_PARAMETERS = (
    '/avatar/parameters/Shock/TouchAreaA',
    '/avatar/parameters/Shock/TouchAreaC',
    '/avatar/parameters/Shock/wildcard/*',
)

DEFAULT_BASIC_SETTINGS = {
    'dglab3': {
        device_kind: {
            'channel_a': {
                'socket_zone': '*',
                'plug_zone': '*',
                'extra_parameters': {
                    'enabled': True,
                    'paths': list(DEFAULT_EXTRA_PARAMETERS),
                    'bottom': 0.0,
                    'top': 1.0,
                },
                'strength_limit': 100,
            },
            'channel_b': {
                'socket_zone': '*',
                'plug_zone': '*',
                'extra_parameters': {
                    'enabled': True,
                    'paths': list(DEFAULT_EXTRA_PARAMETERS),
                    'bottom': 0.0,
                    'top': 1.0,
                },
                'strength_limit': 100,
            },
        }
        for device_kind in ('coyote', 'opossum')
    },
    'version': CONFIG_FILE_VERSION,
}

DEFAULT_SETTINGS = {
    'SERVER_IP': None,
    'dglab3': {
        device_kind: {
            channel: {
                'depth': {
                    'freq_ms': 10,
                    'waveform': WAVEFORM_NAMES_BY_DEVICE[device_kind][0],
                },
            }
            for channel in ('channel_a', 'channel_b')
        }
        for device_kind in ('coyote', 'opossum')
    },
    'ws': {
        'master_uuid': None,
        'listen_host': '0.0.0.0',
        'listen_port': 28846,
    },
    'osc': {
        'listen_host': '127.0.0.1',
        'listen_port': 9001,
    },
    'oscquery': {
        'listen_host': '127.0.0.1',
        'listen_port': 8801,
    },
    'relay': {
        'enabled': False,
        'listen_host': '127.0.0.1',
        'listen_port': 9001,
        'vrcft_host': '127.0.0.1',
        'vrcft_port': 9011,
        'internal_host': '127.0.0.1',
        'internal_port': 9021,
    },
    'web_server': {
        'listen_host': '127.0.0.1',
        'listen_port': 8800,
    },
    'log_level': 'INFO',
    'version': CONFIG_FILE_VERSION,
    'general': {
        'run_in_background': True,
        'steamvr_auto_start': False,
        'local_ip_detect': {'host': '223.5.5.5', 'port': 80},
    },
    'chatbox': {
        'enable': True,
        'osc_host': '127.0.0.1',
        'osc_port': 9000,
        'update_interval': 3.0,
        'set_avatar_parameter': True,
    },
    'api': {
        'control_enabled': False,
        'token': None,
    },
}


def merge_defaults(defaults, values):
    """Recursively add missing defaults without overwriting user values."""
    if not isinstance(values, dict):
        return copy.deepcopy(values)
    merged = copy.deepcopy(defaults)
    for key, value in values.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = merge_defaults(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def apply_basic_settings(settings, basic_settings):
    """Build the runtime settings without mutating persisted dictionaries."""
    runtime = copy.deepcopy(settings)
    for device_kind in ('coyote', 'opossum'):
        for channel in ('channel_a', 'channel_b'):
            basic_channel = basic_settings['dglab3'][device_kind][channel]
            runtime_channel = runtime['dglab3'][device_kind][channel]
            runtime_channel['socket_zone'] = basic_channel['socket_zone']
            runtime_channel['plug_zone'] = basic_channel['plug_zone']
            runtime_channel['extra_parameters'] = copy.deepcopy(basic_channel['extra_parameters'])
            runtime_channel['strength_limit'] = basic_channel['strength_limit']
    return runtime


def migrate_sps_schema(settings, basic_settings):
    """Migrate old single-trigger channels to the automatic max-source model."""
    old_runtime_root = copy.deepcopy(settings.get('dglab3', {}))
    settings = merge_defaults(DEFAULT_SETTINGS, settings)
    migrated_basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
    old_basic_root = basic_settings.get('dglab3', {})
    migrated_runtime = copy.deepcopy(DEFAULT_SETTINGS['dglab3'])
    for device_kind in ('coyote', 'opossum'):
        for channel_name in ('channel_a', 'channel_b'):
            shared_basic = old_basic_root.get(channel_name, {})
            device_basic = old_basic_root.get(device_kind, {}).get(channel_name, {})
            old_basic = device_basic if isinstance(device_basic, dict) and device_basic else shared_basic
            new_basic = migrated_basic['dglab3'][device_kind][channel_name]

            strength_limit = old_basic.get('strength_limit')
            if not isinstance(strength_limit, int) and isinstance(shared_basic, dict):
                strength_limit = shared_basic.get(f'{device_kind}_strength_limit')
                if not isinstance(strength_limit, int):
                    strength_limit = shared_basic.get('strength_limit')
            if isinstance(strength_limit, int):
                new_basic['strength_limit'] = strength_limit
            for trigger_type, field in (
                ('sps_socket', 'socket_zone'),
                ('sps_plug', 'plug_zone'),
            ):
                configured = old_basic.get(field)
                if isinstance(configured, str) and configured.strip():
                    new_basic[field] = configured.strip()
                elif (
                    old_basic.get('trigger_type') == trigger_type
                    and isinstance(old_basic.get('zone'), str)
                    and old_basic['zone'].strip()
                ):
                    new_basic[field] = old_basic['zone'].strip()

            old_extra = old_basic.get('extra_parameters')
            if isinstance(old_extra, dict):
                new_basic['extra_parameters'] = merge_defaults(
                    new_basic['extra_parameters'], old_extra,
                )
            elif isinstance(old_basic.get('avatar_params'), list):
                paths = [
                    str(item).strip() for item in old_basic['avatar_params']
                    if str(item).strip()
                ]
                if paths:
                    new_basic['extra_parameters']['paths'] = paths

            shared_runtime = old_runtime_root.get(channel_name, {})
            device_runtime = old_runtime_root.get(device_kind, {}).get(channel_name, {})
            old_runtime = device_runtime if isinstance(device_runtime, dict) and device_runtime else shared_runtime
            frequency = None
            waveform = None
            if isinstance(old_runtime.get('depth'), dict):
                frequency = old_runtime['depth'].get('freq_ms')
                waveform = old_runtime['depth'].get('waveform')
            legacy_mode = old_runtime.get('mode_config')
            if frequency is None and isinstance(legacy_mode, dict):
                distance = legacy_mode.get('distance')
                if isinstance(distance, dict):
                    frequency = distance.get('freq_ms')
            if waveform is None and isinstance(legacy_mode, dict):
                shock = legacy_mode.get('shock')
                if isinstance(shock, dict):
                    waveform = shock.get('waveform')
            default_depth = DEFAULT_SETTINGS['dglab3'][device_kind][channel_name]['depth']
            if not isinstance(frequency, int) or not 0 <= frequency <= 255:
                frequency = default_depth['freq_ms']
            if waveform not in WAVEFORM_NAMES_BY_DEVICE[device_kind]:
                waveform = default_depth['waveform']
            migrated_runtime[device_kind][channel_name] = {
                'depth': {'freq_ms': frequency, 'waveform': waveform},
            }
    settings['dglab3'] = migrated_runtime
    settings['dglab3'].pop('waveform_sync', None)
    # v0.6 briefly exposed a direct-BLE Opossum mode. DG-LAB 4 APP reports
    # Coyote and OVC as separate slots over the same Socket V4 connection.
    settings.pop('device', None)
    settings.pop('opossum', None)
    settings['version'] = CONFIG_FILE_VERSION
    migrated_basic['version'] = CONFIG_FILE_VERSION
    return settings, migrated_basic


def _validate_port(section, name):
    port = section.get(name)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f'{name} 必须是 1~65535 之间的整数。')


def _validate_host(section, name):
    host = section.get(name)
    if not isinstance(host, str) or not host.strip() or any(char.isspace() for char in host):
        raise ValueError(f'{name} 不是有效的监听地址。')


def validate_config(settings, basic_settings):
    general = settings['general']
    for option in ('run_in_background', 'steamvr_auto_start'):
        if not isinstance(general.get(option), bool):
            raise ValueError(f'general.{option} 必须是布尔值。')

    for section_name in ('osc', 'web_server', 'ws'):
        section = settings[section_name]
        _validate_host(section, 'listen_host')
        _validate_port(section, 'listen_port')

    relay = settings['relay']
    for host_name in ('listen_host', 'vrcft_host', 'internal_host'):
        _validate_host(relay, host_name)
    for port_name in ('listen_port', 'vrcft_port', 'internal_port'):
        _validate_port(relay, port_name)
    if relay['enabled']:
        listen = (relay['listen_host'], relay['listen_port'])
        vrcft = (relay['vrcft_host'], relay['vrcft_port'])
        internal = (relay['internal_host'], relay['internal_port'])
        wildcard_hosts = {'0.0.0.0', '::', '[::]'}

        def overlaps_listener(target):
            return target == listen or (
                target[1] == listen[1]
                and (
                    listen[0] in wildcard_hosts
                    or target[0] in ('127.0.0.1', 'localhost', '::1')
                )
            )

        if overlaps_listener(vrcft) or overlaps_listener(internal):
            raise ValueError('分流目标不能与分流入口相同，否则会形成 UDP 循环。')
        if vrcft == internal:
            raise ValueError('VRCFT 目标与本程序内部目标不能相同。')

    chatbox = settings['chatbox']
    _validate_host(chatbox, 'osc_host')
    _validate_port(chatbox, 'osc_port')
    if float(chatbox['update_interval']) < 1.0:
        raise ValueError('chatbox.update_interval 不能小于 1 秒。')

    for device_kind in ('coyote', 'opossum'):
        for channel_name in ('channel_a', 'channel_b'):
            basic_channel = basic_settings['dglab3'][device_kind][channel_name]
            path = f'{device_kind}.{channel_name}'
            limit = basic_channel['strength_limit']
            if not isinstance(limit, int) or not 0 <= limit <= 200:
                raise ValueError(f'{path}.strength_limit 必须是 0~200 之间的整数。')
            for field in ('socket_zone', 'plug_zone'):
                zone = basic_channel[field]
                if (
                    not isinstance(zone, str)
                    or not zone.strip()
                    or (zone != '*' and ('/' in zone or any(char.isspace() for char in zone)))
                ):
                    raise ValueError(f'{path}.{field} 必须是有效的 SPS 部位 ID。')
            extra = basic_channel['extra_parameters']
            if not isinstance(extra.get('enabled'), bool):
                raise ValueError(f'{path}.extra_parameters.enabled 必须是布尔值。')
            paths = extra.get('paths')
            if not isinstance(paths, list) or any(
                not isinstance(item, str) or not item.startswith('/avatar/parameters/')
                for item in paths
            ):
                raise ValueError(f'{path}.extra_parameters.paths 包含无效 OSC 参数。')
            bottom, top = extra.get('bottom'), extra.get('top')
            if (
                not isinstance(bottom, (int, float))
                or not isinstance(top, (int, float))
                or float(bottom) >= float(top)
            ):
                raise ValueError(f'{path}.extra_parameters 范围无效。')
            frequency = settings['dglab3'][device_kind][channel_name]['depth']['freq_ms']
            if not isinstance(frequency, int) or not 0 <= frequency <= 255:
                raise ValueError(f'{path}.depth.freq_ms 必须是 0~255 之间的整数。')
            waveform = settings['dglab3'][device_kind][channel_name]['depth']['waveform']
            if waveform not in WAVEFORM_NAMES_BY_DEVICE[device_kind]:
                raise ValueError(f'{path}.depth.waveform 不是有效波形。')

    try:
        uuid.UUID(str(settings['ws']['master_uuid']))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('ws.master_uuid 必须是有效 UUID。') from exc
    if settings['api']['control_enabled'] and not settings['api'].get('token'):
        raise ValueError('启用 API 控制时必须设置 api.token。')


def parse_endpoint(value):
    """Parse host:port or [IPv6]:port into a validated pair."""
    value = str(value).strip()
    if value.startswith('['):
        match = re.fullmatch(r'\[([^]]+)]:(\d+)', value)
        if not match:
            raise ValueError(f'地址格式无效：{value}')
        host, port_text = match.groups()
    else:
        try:
            host, port_text = value.rsplit(':', 1)
        except ValueError as exc:
            raise ValueError(f'地址格式无效：{value}') from exc
    port = int(port_text)
    section = {'host': host, 'port': port}
    _validate_host(section, 'host')
    _validate_port(section, 'port')
    return host, port


def format_endpoint(host, port):
    host = str(host)
    return f'[{host}]:{port}' if ':' in host and not host.startswith('[') else f'{host}:{port}'


class ConfigManager:
    def __init__(self, app_dir=None, config_dir=None):
        self.app_dir = Path(app_dir) if app_dir else Path.cwd()
        if config_dir is None:
            appdata = os.environ.get('APPDATA')
            config_dir = Path(appdata) / APP_NAME if appdata else Path.home() / 'AppData' / 'Roaming' / APP_NAME
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / CONFIG_FILENAME
        self.migrated_from = None

    def _new_config(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['ws']['master_uuid'] = str(uuid.uuid4())
        settings['api']['token'] = secrets.token_urlsafe(32)
        return settings, basic

    def _read_yaml(self, path):
        with Path(path).open('r', encoding='utf-8') as stream:
            value = yaml.safe_load(stream) or {}
        if not isinstance(value, dict):
            raise ValueError(f'配置文件格式无效：{path}')
        return value

    def _find_legacy_files(self):
        for version in ('v0.8', 'v0.7', 'v0.6', 'v0.5', 'v0.4', 'v0.3'):
            for base in (self.config_dir, self.app_dir):
                unified = base / f'settings-{version}.yaml'
                if unified.exists():
                    return unified
        candidates = (self.config_dir, self.app_dir)
        for base in candidates:
            advanced = base / 'settings-advanced-v0.2.yaml'
            basic = base / 'settings-v0.2.yaml'
            if advanced.exists() and basic.exists():
                return advanced, basic
        return None

    def load(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            document = self._read_yaml(self.path)
            settings = document.get('settings', {})
            basic = document.get('channels', {})
        else:
            legacy = self._find_legacy_files()
            if legacy:
                if isinstance(legacy, Path):
                    document = self._read_yaml(legacy)
                    settings = document.get('settings', {})
                    basic = document.get('channels', {})
                    self.migrated_from = str(legacy)
                else:
                    advanced_path, basic_path = legacy
                    settings = self._read_yaml(advanced_path)
                    basic = self._read_yaml(basic_path)
                    self.migrated_from = str(advanced_path.parent)
            else:
                settings, basic = self._new_config()

        settings, basic = migrate_sps_schema(settings, basic)
        if settings['ws'].get('master_uuid') is None:
            settings['ws']['master_uuid'] = str(uuid.uuid4())
        if settings['api'].get('token') is None:
            settings['api']['token'] = secrets.token_urlsafe(32)
        validate_config(settings, basic)
        self.save(settings, basic)
        return settings, basic

    def save(self, settings, basic_settings):
        settings = copy.deepcopy(settings)
        basic_settings = copy.deepcopy(basic_settings)
        settings['version'] = CONFIG_FILE_VERSION
        basic_settings['version'] = CONFIG_FILE_VERSION
        validate_config(settings, basic_settings)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        document = {
            'version': CONFIG_FILE_VERSION,
            'settings': settings,
            'channels': basic_settings,
        }
        temporary = self.path.with_suffix(self.path.suffix + '.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            yaml.safe_dump(document, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

