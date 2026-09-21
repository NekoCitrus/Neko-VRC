import copy
import json
import os
import re
import secrets
import uuid
from pathlib import Path

import yaml


APP_NAME = 'ShockingVRChat'
CONFIG_FILE_VERSION = 'v0.3'
CONFIG_FILENAME = f'settings-{CONFIG_FILE_VERSION}.yaml'

DEFAULT_BASIC_SETTINGS = {
    'dglab3': {
        'channel_a': {
            'avatar_params': [
                '/avatar/parameters/pcs/contact/enterPass',
                '/avatar/parameters/Shock/TouchAreaA',
                '/avatar/parameters/Shock/TouchAreaC',
                '/avatar/parameters/Shock/wildcard/*',
            ],
            'mode': 'distance',
            'strength_limit': 100,
        },
        'channel_b': {
            'avatar_params': [
                '/avatar/parameters/pcs/contact/enterPass',
                '/avatar/parameters/lms-penis-proximityA*',
                '/avatar/parameters/Shock/TouchAreaB',
                '/avatar/parameters/Shock/TouchAreaC',
            ],
            'mode': 'distance',
            'strength_limit': 100,
        },
    },
    'version': CONFIG_FILE_VERSION,
}

_DEFAULT_WAVE = json.dumps(['0A0A0A0A64646464'] * 10, separators=(',', ':'))
_DEFAULT_WAVEFORM = '呼吸'

DEFAULT_SETTINGS = {
    'SERVER_IP': None,
    'dglab3': {
        channel: {
            'mode_config': {
                'shock': {'duration': 2, 'wave': _DEFAULT_WAVE, 'waveform': _DEFAULT_WAVEFORM},
                'distance': {'freq_ms': 10},
                'trigger_range': {'bottom': 0.0, 'top': 1.0},
            },
        }
        for channel in ('channel_a', 'channel_b')
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
    for channel in ('channel_a', 'channel_b'):
        basic_channel = basic_settings['dglab3'][channel]
        runtime_channel = runtime['dglab3'][channel]
        runtime_channel['avatar_params'] = copy.deepcopy(basic_channel['avatar_params'])
        runtime_channel['mode'] = basic_channel['mode']
        runtime_channel['strength_limit'] = basic_channel['strength_limit']
    return runtime


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

    for channel_name in ('channel_a', 'channel_b'):
        basic_channel = basic_settings['dglab3'][channel_name]
        if basic_channel['mode'] not in ('distance', 'shock'):
            raise ValueError(f'{channel_name}.mode 只支持 distance 或 shock。')
        if not isinstance(basic_channel['strength_limit'], int) or not 0 <= basic_channel['strength_limit'] <= 200:
            raise ValueError(f'{channel_name}.strength_limit 必须是 0~200 之间的整数。')
        avatar_params = basic_channel['avatar_params']
        if not isinstance(avatar_params, list) or not avatar_params:
            raise ValueError(f'{channel_name}.avatar_params 至少需要一个参数。')
        if not all(
            isinstance(item, str)
            and item.startswith('/avatar/parameters/')
            and not any(char.isspace() for char in item)
            for item in avatar_params
        ):
            raise ValueError(f'{channel_name}.avatar_params 包含无效参数。')

        mode_config = settings['dglab3'][channel_name]['mode_config']
        trigger_range = mode_config['trigger_range']
        bottom = float(trigger_range['bottom'])
        top = float(trigger_range['top'])
        if not 0 <= bottom < top <= 1:
            raise ValueError(f'{channel_name}.trigger_range 必须满足 0 <= bottom < top <= 1。')
        frequency = mode_config['distance']['freq_ms']
        if not isinstance(frequency, int) or not 0 <= frequency <= 255:
            raise ValueError(f'{channel_name}.distance.freq_ms 必须是 0~255 之间的整数。')
        duration = float(mode_config['shock']['duration'])
        if not 0.1 <= duration <= 10:
            raise ValueError(f'{channel_name}.shock.duration 必须在 0.1~10 秒之间。')
        try:
            wave = json.loads(mode_config['shock']['wave'])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f'{channel_name}.shock.wave 必须是合法 JSON 数组。') from exc
        if not wave or not all(isinstance(item, str) and re.fullmatch(r'[0-9A-F]{16}', item) for item in wave):
            raise ValueError(f'{channel_name}.shock.wave 包含无效波形。')

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


def parse_parameter_lines(value):
    params = []
    seen = set()
    for raw_line in str(value).splitlines():
        param = raw_line.strip()
        if not param or param.startswith('#'):
            continue
        if not param.startswith('/avatar/parameters/') or any(char.isspace() for char in param):
            raise ValueError(f'无效的 Avatar 参数：{param}')
        if param not in seen:
            seen.add(param)
            params.append(param)
    if not params:
        raise ValueError('每个通道至少需要一个 Avatar 参数。')
    return params


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
            settings = merge_defaults(DEFAULT_SETTINGS, document.get('settings', {}))
            basic = merge_defaults(DEFAULT_BASIC_SETTINGS, document.get('channels', {}))
        else:
            legacy = self._find_legacy_files()
            if legacy:
                advanced_path, basic_path = legacy
                settings = merge_defaults(DEFAULT_SETTINGS, self._read_yaml(advanced_path))
                basic = merge_defaults(DEFAULT_BASIC_SETTINGS, self._read_yaml(basic_path))
                self.migrated_from = str(advanced_path.parent)
            else:
                settings, basic = self._new_config()

        settings['version'] = CONFIG_FILE_VERSION
        basic['version'] = CONFIG_FILE_VERSION
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

