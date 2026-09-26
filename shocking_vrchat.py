import asyncio
import copy
import hmac
import ipaddress
import json
import math
import socket
import sys
import traceback
from concurrent.futures import TimeoutError as FutureTimeoutError
from functools import wraps
from pathlib import Path
from threading import Event, RLock, Thread
from urllib.parse import parse_qs, quote, urlsplit

from flask import Flask, jsonify, redirect, render_template, request
from loguru import logger
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import AsyncIOOSCUDPServer
from websockets.asyncio.server import serve as wsserve
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

import srv
from srv.advanced_chatbox_manager import AdvancedChatboxManager
from srv.config_manager import (
    CONFIG_FILE_VERSION,
    DEFAULT_BASIC_SETTINGS,
    DEFAULT_SETTINGS,
    ConfigManager,
    apply_basic_settings,
    merge_defaults,
    validate_config,
)
from srv.connector.coyotev3ws import DGConnection
from srv.connector.coyotev4ws import DGV4Connection
from srv.handler.machine_handler import TuYaConnection, TuyaHandler
from srv.handler.shock_handler import ShockHandler
from srv.oscquery import OSCQueryAvatarSnapshot, OSCQueryService
from srv.sps_depth import SPS_PLUG, SPS_SOCKET, SPSZone, find_avatar_sps_config


BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BUNDLE_DIR / 'templates'))

SETTINGS = copy.deepcopy(DEFAULT_SETTINGS)
SETTINGS_BASIC = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
CONFIG_MANAGER = None
CONFIG_FILENAME = None
CONFIG_FILENAME_BASIC = None
SERVER_IP = '127.0.0.1'
ACTIVE_CONTROLLER = None
RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
)
CGNAT_NETWORK = ipaddress.ip_network('100.64.0.0/10')


def configure_console_encoding():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            try:
                reconfigure(encoding='utf-8')
            except (OSError, ValueError):
                pass


configure_console_encoding()


def detect_current_ip(settings=None):
    current = settings or SETTINGS
    candidates = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2.0)
            target = current['general']['local_ip_detect']
            sock.connect((target['host'], target['port']))
            candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        candidates.extend(
            item[4][0]
            for item in socket.getaddrinfo(
                socket.gethostname(),
                None,
                family=socket.AF_INET,
                type=socket.SOCK_DGRAM,
            )
        )
    except OSError:
        pass
    usable = []
    for order, candidate in enumerate(dict.fromkeys(candidates)):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        if any(address in network for network in RFC1918_NETWORKS):
            priority = 0
        elif address.is_global:
            priority = 1
        elif address in CGNAT_NETWORK:
            priority = 2
        else:
            # Excludes benchmark and documentation ranges commonly used by
            # VPN/proxy virtual adapters (for example 198.18.0.0/15).
            continue
        usable.append((priority, order, candidate))
    if usable:
        return min(usable)[2]
    logger.warning('无法自动检测局域网 IP，二维码暂时使用 127.0.0.1。')
    return '127.0.0.1'


def build_websocket_url(settings=None, server_ip=None):
    current = settings or SETTINGS
    ip = server_ip or current.get('SERVER_IP') or SERVER_IP or detect_current_ip(current)
    if ':' in ip and not ip.startswith('['):
        ip = f'[{ip}]'
    return (
        f'ws://{ip}:{current["ws"]["listen_port"]}/'
        f'?tid={current["ws"]["master_uuid"]}'
    )


def build_qr_content(settings=None, server_ip=None):
    websocket_url = build_websocket_url(settings, server_ip)
    return (
        'https://dungeon-lab.cn/s/?v=1&action=socket&url='
        f'{quote(websocket_url, safe="")}'
    )


@app.route('/get_ip')
def get_current_ip():
    return detect_current_ip()


@app.route('/')
def web_index():
    return redirect('/qr', code=302)


@app.route('/qr')
def web_qr():
    return render_template('tiny-qr.html', content=build_qr_content())


@app.after_request
def after_request_hook(response):
    if request.args.get('ret') == 'status' and response.status_code == 200:
        response = jsonify(build_status_response())
    return response


class ClientNotAllowed(Exception):
    pass


@app.errorhandler(ClientNotAllowed)
def handle_client_not_allowed(_error):
    return {'error': 'Client not allowed.'}, 401


@app.errorhandler(Exception)
def handle_exception(error):
    if isinstance(error, HTTPException):
        return error
    logger.error(traceback.format_exc())
    return {'error': 'Internal server error.'}, 500


def require_control_access(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        api_settings = SETTINGS.get('api', {})
        if not api_settings.get('control_enabled', False):
            raise ClientNotAllowed
        expected_token = str(api_settings.get('token') or '')
        supplied_token = request.headers.get('X-Control-Token') or request.args.get('token') or ''
        if not expected_token or not hmac.compare_digest(expected_token, supplied_token):
            raise ClientNotAllowed
        user_agent = request.headers.get('User-Agent', '')
        if 'UnityPlayer' not in user_agent or 'NSPlayer' in user_agent or 'WMFSDK' in user_agent:
            raise ClientNotAllowed
        return func(*args, **kwargs)
    return wrapper


@app.route('/api/v1/status')
def api_v1_status():
    return build_status_response()


def build_status_response():
    connections = tuple(
        connection
        for connection in srv.get_ws_connections()
        if getattr(connection, 'is_device_ready', lambda: True)()
    )
    device_entries = []
    for connection in connections:
        for state in connection_device_states(connection):
            device_entries.append({
                'type': 'shock',
                'device': {
                    'COYOTE_020': 'coyotev2',
                    'COYOTE_030': 'coyotev3',
                    'OVC_1': 'opossum',
                }.get(state['device_type'], 'coyotev3'),
                'attr': {
                    'strength': dict(state['strength']),
                    'uuid': connection.uuid,
                    'slot_id': state['slot_id'],
                },
            })
    return {
        'healthy': 'ok',
        'service': ACTIVE_CONTROLLER.state if ACTIVE_CONTROLLER else 'stopped',
        'devices': device_entries,
    }


def connection_device_states(connection):
    getter = getattr(connection, 'get_device_states', None)
    if getter is not None:
        return getter()
    device_type = getattr(connection, 'device_type', None) or 'COYOTE_030'
    kind = 'opossum' if device_type == 'OVC_1' else 'coyote'
    strength_max = dict(getattr(connection, 'strength_max', {'A': 200, 'B': 200}))
    get_upper = getattr(connection, 'get_upper_strength', None)
    return ({
        'slot_id': getattr(connection, 'slot_id', None) or connection.uuid,
        'device_type': device_type,
        'device_kind': kind,
        'device_name': getattr(connection, 'device_name', None),
        'strength': dict(connection.strength),
        'strength_max': strength_max,
        'upper_strength': {
            channel: get_upper(channel) if get_upper is not None else strength_max[channel]
            for channel in ('A', 'B')
        },
    },)


def submit_to_async_loop(coroutine, timeout=10):
    controller = ACTIVE_CONTROLLER
    runtime = controller.runtime if controller else None
    loop = runtime.loop if runtime else None
    if loop is None or not loop.is_running():
        coroutine.close()
        raise RuntimeError('Device service is not running.')
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        future.cancel()
        raise TimeoutError('Device operation timed out.')


def normalize_wave_request(channel, repeat, wavedata):
    import re

    channel = str(channel).upper()
    if channel not in ('A', 'B'):
        raise ValueError('channel must be A or B')
    repeat = int(repeat)
    if repeat < 1 or repeat > 100:
        raise ValueError('repeat must be between 1 and 100')
    if not re.fullmatch(r'[0-9A-F]{16}', str(wavedata)):
        raise ValueError('wavedata must be 16 uppercase hexadecimal characters')
    return channel, repeat, str(wavedata)


async def broadcast_repeated_wave(channels, repeat, wavedata):
    wavestr = json.dumps([wavedata] * repeat, separators=(',', ':'))
    for channel in channels:
        await DGConnection.broadcast_wave(channel=channel, wavestr=wavestr)


@app.route('/api/v1/shock/<channel>/<second>', methods=['GET', 'POST'])
@require_control_access
def api_v1_shock(channel, second):
    channel = str(channel).upper()
    if channel == 'ALL':
        channels = ['A', 'B']
    elif channel in ('A', 'B'):
        channels = [channel]
    else:
        return {'error': 'channel must be A, B, or all'}, 400
    try:
        second = float(second)
    except ValueError:
        return {'error': 'second must be a number'}, 400
    if not 0.1 <= second <= 10.0:
        return {'error': 'second must be between 0.1 and 10'}, 400
    submit_to_async_loop(broadcast_repeated_wave(channels, math.ceil(second / 0.1), '0A0A0A0A64646464'))
    return {'result': 'OK'}


@app.route('/api/v1/sendwave/<channel>/<repeat>/<wavedata>', methods=['GET', 'POST'])
@require_control_access
def api_v1_sendwave(channel, repeat, wavedata):
    try:
        channel, repeat, wavedata = normalize_wave_request(channel, repeat, wavedata)
    except (TypeError, ValueError) as exc:
        return {'error': str(exc)}, 400
    submit_to_async_loop(broadcast_repeated_wave([channel], repeat, wavedata))
    return {'result': 'OK'}


class DeviceRuntime:
    """Own the asynchronous OSC, relay, WebSocket and Chatbox services."""

    def __init__(self, settings, event_callback=None):
        self.settings = settings
        self.event_callback = event_callback
        self.loop = None
        self.stop_event = None
        self.ready = Event()
        self.error = None
        self.thread = None
        self.handlers = []
        self.chatbox_manager = None
        self.relay_packets = 0
        self.oscquery = None
        self.current_avatar_id = ''

    def _emit(self, event):
        if self.event_callback is not None:
            try:
                self.event_callback(event)
            except Exception:
                logger.debug('UI event callback failed.')

    def _build_dispatcher(self):
        dispatcher = Dispatcher()
        self.handlers = []
        self.chatbox_manager = AdvancedChatboxManager(self.settings)
        for device_kind in ('coyote', 'opossum'):
            for channel in ('A', 'B'):
                handler = ShockHandler(
                    SETTINGS=self.settings,
                    DG_CONN=DGConnection,
                    channel_name=channel,
                    device_kind=device_kind,
                    event_callback=self._emit,
                )
                handler.set_chatbox_manager(self.chatbox_manager)
                self.handlers.append(handler)
                for address, signal in handler.osc_bindings():
                    dispatcher.map(address, handler.osc_handler, signal)
                    logger.info('{} 通道 {} 监听 SPS：{}', device_kind, channel, address)

        dispatcher.map('/avatar/change', self._avatar_change_handler)

        if 'machine' in self.settings and 'tuya' in self.settings['machine']:
            tuya = self.settings['machine']['tuya']
            connection = TuYaConnection(
                access_id=tuya['access_id'],
                access_key=tuya['access_key'],
                device_ids=tuya['device_ids'],
            )
            handler = TuyaHandler(SETTINGS=self.settings, DEV_CONN=connection)
            self.handlers.append(handler)
            for param in tuya['avatar_params']:
                dispatcher.map(param, handler.osc_handler)
        return dispatcher

    def _avatar_change_handler(self, _address, *args):
        if len(args) != 1 or not isinstance(args[0], str):
            logger.warning('Ignored invalid /avatar/change OSC message: {}', args)
            return
        avatar_id = args[0].strip()
        if self.oscquery is not None:
            self.oscquery.request_avatar_refresh(avatar_id)
        else:
            self._apply_avatar_snapshot(OSCQueryAvatarSnapshot(avatar_id, ()))

    def _oscquery_avatar_callback(self, snapshot):
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._apply_avatar_snapshot, snapshot)

    @staticmethod
    def _query_zones(parameter_paths):
        zones = {SPS_SOCKET: set(), SPS_PLUG: set()}
        for path in parameter_paths:
            parts = str(path).strip('/').split('/')
            if len(parts) < 6 or parts[:3] != ['avatar', 'parameters', 'OGB']:
                continue
            if parts[3] == 'Orf':
                zones[SPS_SOCKET].add(parts[4])
            elif parts[3] == 'Pen':
                zones[SPS_PLUG].add(parts[4])
        return zones

    def _apply_avatar_snapshot(self, snapshot):
        avatar_id = snapshot.avatar_id
        if avatar_id != self.current_avatar_id:
            for handler in self.handlers:
                if isinstance(handler, ShockHandler):
                    handler.reset_for_avatar_change()
            self.current_avatar_id = avatar_id
        avatar = find_avatar_sps_config(avatar_id, APP_DIR)
        local_zones = {}
        if avatar is not None:
            local_zones.update({
                (SPS_SOCKET, item.zone_id): item
                for item in avatar.sockets
            })
            local_zones.update({
                (SPS_PLUG, item.zone_id): item
                for item in avatar.plugs
            })
        if snapshot.endpoint:
            query_zones = self._query_zones(snapshot.parameter_paths)
            sockets = tuple(
                local_zones.get(
                    (SPS_SOCKET, zone_id),
                    SPSZone(SPS_SOCKET, zone_id, zone_id),
                )
                for zone_id in sorted(query_zones[SPS_SOCKET], key=str.casefold)
            )
            plugs = tuple(
                local_zones.get(
                    (SPS_PLUG, zone_id),
                    SPSZone(SPS_PLUG, zone_id, zone_id),
                )
                for zone_id in sorted(query_zones[SPS_PLUG], key=str.casefold)
            )
            source = 'OSCQuery'
        else:
            sockets = avatar.sockets if avatar else ()
            plugs = avatar.plugs if avatar else ()
            source = '/avatar/change 兜底'
        event = {
            'type': 'avatar',
            'avatar_id': avatar_id,
            'avatar_name': avatar.avatar_name if avatar else avatar_id,
            'found': bool(snapshot.endpoint or avatar is not None),
            'source': source,
            'sockets': [
                {'zone_id': item.zone_id, 'label': item.label}
                for item in sockets
            ],
            'plugs': [
                {'zone_id': item.zone_id, 'label': item.label}
                for item in plugs
            ],
        }
        self._emit(event)
        logger.info(
            'Current Avatar from {}: {} ({})，Socket {} / Plug {}',
            source,
            event['avatar_name'],
            avatar_id,
            len(sockets),
            len(plugs),
        )

    def _relay_packet(self, _size, _address):
        self.relay_packets += 1

    async def _websocket_handler(self, connection):
        if srv.get_ws_connections():
            await connection.close(code=1008, reason='Only one device is supported')
            logger.warning('已拒绝第二个 DG-LAB APP 连接。')
            return
        request_path = getattr(getattr(connection, 'request', None), 'path', '/')
        parsed = urlsplit(request_path)
        query = parse_qs(parsed.query)
        target_ids = query.get('tid') or query.get('targetId')
        if target_ids:
            if target_ids[0] != str(self.settings['ws']['master_uuid']):
                await connection.close(code=1008, reason='Unknown controller id')
                logger.warning('已拒绝目标 ID 不匹配的 V4 连接。')
                return
            client = DGV4Connection(connection, settings=self.settings)
        else:
            expected_path = f'/{self.settings["ws"]["master_uuid"]}'
            if parsed.path.rstrip('/') != expected_path:
                await connection.close(code=1008, reason='Invalid pairing path')
                logger.warning('已拒绝配对路径无效的 WebSocket 连接。')
                return
            client = DGConnection(connection, SETTINGS=self.settings)
        self._emit({
            'type': 'device',
            'connected': False,
            'app_connected': True,
            'protocol': client.protocol_version,
        })
        try:
            await client.serve()
        finally:
            self._emit({'type': 'device', 'connected': False, 'app_connected': False})

    async def _chatbox_task(self):
        while True:
            connections = srv.get_ws_connections()[:1]
            await self.chatbox_manager.update_chatbox(connections, self.handlers)
            await asyncio.sleep(0.5)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        osc_transport = None
        chatbox_task = None
        try:
            dispatcher = self._build_dispatcher()
            for handler in self.handlers:
                start = getattr(handler, 'start_background_jobs', None)
                if start:
                    start()
            if self.chatbox_manager.enabled:
                chatbox_task = asyncio.create_task(self._chatbox_task())

            osc_address = (self.settings['osc']['listen_host'], self.settings['osc']['listen_port'])
            self.oscquery = OSCQueryService(
                self.settings,
                avatar_callback=self._oscquery_avatar_callback,
            )
            self.oscquery.start()

            osc_server = AsyncIOOSCUDPServer(osc_address, dispatcher, self.loop)
            osc_transport, _ = await osc_server.create_serve_endpoint()
            async with wsserve(
                self._websocket_handler,
                self.settings['ws']['listen_host'],
                self.settings['ws']['listen_port'],
                ping_interval=10,
                ping_timeout=30,
                max_queue=32,
            ):
                self.ready.set()
                self._emit({'type': 'service', 'state': 'running'})
                await self.stop_event.wait()
        except Exception as exc:
            self.error = exc
            self.ready.set()
            self._emit({'type': 'service', 'state': 'error', 'message': str(exc)})
            logger.error(traceback.format_exc())
        finally:
            if chatbox_task is not None:
                chatbox_task.cancel()
                await asyncio.gather(chatbox_task, return_exceptions=True)
            for handler in self.handlers:
                stop = getattr(handler, 'stop_background_jobs', None)
                if stop:
                    await stop()
            if self.chatbox_manager is not None:
                self.chatbox_manager.cleanup()
            if osc_transport is not None:
                osc_transport.close()
            if self.oscquery is not None:
                self.oscquery.stop()
                self.oscquery = None
            self._emit({'type': 'service', 'state': 'stopped'})

    def start(self, timeout=10):
        self.ready.clear()
        self.error = None
        self.thread = Thread(target=lambda: asyncio.run(self._main()), daemon=True, name='device-services')
        self.thread.start()
        if not self.ready.wait(timeout=timeout):
            self.stop()
            raise RuntimeError('后台设备服务启动超时。')
        if self.error is not None:
            self.stop()
            raise RuntimeError(f'后台设备服务启动失败：{self.error}') from self.error

    def stop(self):
        if self.loop is not None and self.stop_event is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.stop_event.set)
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning('后台设备服务未能在 5 秒内完全退出。')
        self.thread = None

    def snapshot(self):
        connection = next(iter(srv.get_ws_connections()), None)
        device_ready = bool(
            connection
            and getattr(connection, 'is_device_ready', lambda: True)()
        )
        device_states = connection_device_states(connection) if device_ready else ()
        device_channels = {'coyote': {}, 'opossum': {}}
        for handler in self.handlers:
            if not isinstance(handler, ShockHandler):
                continue
            info = handler.get_mode_info()
            device_strengths = []
            for state in device_states:
                if state['device_kind'] != handler.device_kind:
                    continue
                device_strengths.append({
                    'slot_id': state['slot_id'],
                    'device_name': state['device_name'],
                    'actual_strength': int(state['strength'].get(handler.channel, 0)),
                    'upper_strength': int(state['upper_strength'].get(handler.channel, 0)),
                })
            primary = next(iter(device_strengths), {'actual_strength': 0, 'upper_strength': 0})
            device_channels[handler.device_kind][handler.channel] = {
                **info,
                'actual_strength': primary['actual_strength'],
                'upper_strength': primary['upper_strength'],
                'device_strengths': device_strengths,
            }
        # Keep the original key as a compatibility view for API consumers.
        channels = device_channels['coyote']
        return {
            'connected': device_ready,
            'app_connected': connection is not None,
            'protocol': getattr(connection, 'protocol_version', '') if connection else '',
            'device_type': getattr(connection, 'device_type', '') if connection else '',
            'device_name': getattr(connection, 'device_name', '') if connection else '',
            'devices': list(device_states),
            'device_id': (
                getattr(connection, 'slot_id', None) or connection.uuid
                if device_ready else ''
            ),
            'relay_packets': self.relay_packets,
            'channels': channels,
            'device_channels': device_channels,
        }


class ServiceController:
    def __init__(self, event_callback=None):
        self.event_callback = event_callback
        self.runtime = None
        self.web_server = None
        self.web_thread = None
        self.state = 'stopped'
        self._lock = RLock()

    def _emit(self, event):
        if self.event_callback:
            self.event_callback(event)

    def start(self, settings=None, basic_settings=None):
        global SETTINGS, SETTINGS_BASIC, SERVER_IP, ACTIVE_CONTROLLER
        with self._lock:
            if self.state in ('starting', 'running'):
                return
            self.state = 'starting'
            self._emit({'type': 'service', 'state': self.state})
            settings = copy.deepcopy(settings if settings is not None else SETTINGS)
            basic_settings = copy.deepcopy(basic_settings if basic_settings is not None else SETTINGS_BASIC)
            validate_config(settings, basic_settings)
            runtime_settings = apply_basic_settings(settings, basic_settings)
            SETTINGS = settings
            SETTINGS_BASIC = basic_settings
            SERVER_IP = settings.get('SERVER_IP') or detect_current_ip(settings)
            ACTIVE_CONTROLLER = self
            try:
                self.runtime = DeviceRuntime(runtime_settings, event_callback=self._emit)
                self.runtime.start()
                self.web_server = make_server(
                    settings['web_server']['listen_host'],
                    settings['web_server']['listen_port'],
                    app,
                    threaded=True,
                )
                self.web_thread = Thread(
                    target=self.web_server.serve_forever,
                    daemon=True,
                    name='status-web-server',
                )
                self.web_thread.start()
                self.state = 'running'
                self._emit({'type': 'service', 'state': self.state})
            except Exception:
                if self.web_server is not None:
                    self.web_server.server_close()
                    self.web_server = None
                self.web_thread = None
                if self.runtime is not None:
                    self.runtime.stop()
                self.runtime = None
                self.state = 'error'
                self._emit({'type': 'service', 'state': self.state})
                raise

    def stop(self):
        with self._lock:
            if self.web_server is not None:
                self.web_server.shutdown()
                self.web_server.server_close()
                self.web_server = None
            if self.web_thread is not None:
                self.web_thread.join(timeout=3)
                self.web_thread = None
            if self.runtime is not None:
                self.runtime.stop()
                self.runtime = None
            self.state = 'stopped'
            self._emit({'type': 'service', 'state': self.state})

    def restart(self, settings=None, basic_settings=None):
        self.stop()
        self.start(settings, basic_settings)

    def snapshot(self):
        if self.runtime is None:
            return {'connected': False, 'device_id': '', 'relay_packets': 0, 'channels': {}}
        return self.runtime.snapshot()


class ConfigFileInited(Exception):
    """Kept for source compatibility with v0.2 integrations."""


def config_init(config_dir=None):
    global CONFIG_MANAGER, CONFIG_FILENAME, CONFIG_FILENAME_BASIC, SETTINGS, SETTINGS_BASIC, SERVER_IP
    CONFIG_MANAGER = ConfigManager(APP_DIR, config_dir=config_dir)
    SETTINGS, SETTINGS_BASIC = CONFIG_MANAGER.load()
    CONFIG_FILENAME = CONFIG_MANAGER.path
    CONFIG_FILENAME_BASIC = CONFIG_MANAGER.path
    SERVER_IP = SETTINGS.get('SERVER_IP') or detect_current_ip(SETTINGS)
    logger.remove()
    log_path = CONFIG_MANAGER.config_dir / 'neko-vrc.log'
    logger.add(log_path, level=SETTINGS.get('log_level', 'INFO'), rotation='2 MB', retention=3, encoding='utf-8')
    if sys.stderr is not None:
        logger.add(sys.stderr, level=SETTINGS.get('log_level', 'INFO'))
    return SETTINGS, SETTINGS_BASIC


def config_save():
    if CONFIG_MANAGER is None:
        raise RuntimeError('配置尚未初始化。')
    CONFIG_MANAGER.save(SETTINGS, SETTINGS_BASIC)


def main():
    import ctypes

    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\NekoVRC.SingleInstance')
    if not mutex:
        raise ctypes.WinError()
    if ctypes.windll.kernel32.GetLastError() == 183:
        existing = ctypes.windll.user32.FindWindowW('NekoVRCDesktopWindow', None)
        if existing:
            ctypes.windll.user32.PostMessageW(existing, 0x8004, 0, 0)
        ctypes.windll.kernel32.CloseHandle(mutex)
        return
    config_init()
    from srv.win32_ui import DesktopApplication

    controller = ServiceController()
    application = DesktopApplication(CONFIG_MANAGER, SETTINGS, SETTINGS_BASIC, controller)
    controller.event_callback = application.post_event
    application.run()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logger.error(traceback.format_exc())
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, traceback.format_exc(), 'Neko-VRC 启动失败', 0x10)
        else:
            raise
