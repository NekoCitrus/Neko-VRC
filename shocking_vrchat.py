import asyncio
import copy
import errno
import hmac
import ipaddress
import json
import os
import socket
import sys
import time
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
from srv.device_router import DeviceRouter
from srv.handler.machine_handler import TuYaConnection, TuyaHandler
from srv.handler.shock_handler import ShockHandler
from srv.oscquery import OSCQueryAvatarSnapshot, OSCQueryService
from srv.sps_depth import (
    SPS_PLUG, SPS_SOCKET, SPSZone, find_avatar_sps_config,
    has_socket_depth_parameters, has_plug_depth_parameters,
    PCS_PROXIMITY_ADDRESS, LMS_PROXIMITY_ADDRESS,
)


BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BUNDLE_DIR / 'templates'))

SETTINGS = copy.deepcopy(DEFAULT_SETTINGS)
SETTINGS_BASIC = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
CONFIG_MANAGER = None
CONFIG_FILENAME = None
CONFIG_FILENAME_BASIC = None
RUN_LOG_PATH = None
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
        response = jsonify(build_legacy_status_response())
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


def _is_vrchat_client():
    user_agent = request.headers.get('User-Agent', '')
    return (
        'UnityPlayer' in user_agent
        and 'NSPlayer' not in user_agent
        and 'WMFSDK' not in user_agent
    )


def _is_local_network_client():
    try:
        address = ipaddress.ip_address(request.remote_addr or '')
    except ValueError:
        return False
    mapped_address = getattr(address, 'ipv4_mapped', None)
    if mapped_address is not None:
        address = mapped_address
    return address.is_loopback or address.is_link_local or address.is_private


def _has_control_access():
    api_settings = SETTINGS.get('api', {})
    if not api_settings.get('control_enabled', False):
        return False
    expected_token = str(api_settings.get('token') or '')
    supplied_token = request.headers.get('X-Control-Token') or request.args.get('token') or ''
    return bool(
        expected_token
        and hmac.compare_digest(expected_token, supplied_token)
        and _is_vrchat_client()
    )


def require_control_access(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not _has_control_access():
            raise ClientNotAllowed
        return func(*args, **kwargs)
    return wrapper


def allow_vrchat_map_trigger(func):
    """Restore the original local VRChat map-trigger contract safely."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if _is_vrchat_client() and _is_local_network_client():
            return func(*args, **kwargs)
        if _has_control_access():
            return func(*args, **kwargs)
        raise ClientNotAllowed
    return wrapper


@app.route('/api/v1/status')
def api_v1_status():
    return build_legacy_status_response()


def build_status_response():
    connections = tuple(
        connection
        for connection in srv.get_ws_connections()
        if getattr(connection, 'is_device_ready', lambda: True)()
    )
    device_entries = []
    runtime = getattr(ACTIVE_CONTROLLER, 'runtime', None)
    router = getattr(runtime, 'router', None)
    state_connections = (
        [(state, state.get('connection_uuid', state['slot_id'])) for state in router.device_states()]
        if router is not None else
        [(state, connection.uuid) for connection in connections for state in connection_device_states(connection)]
    )
    for state, connection_uuid in state_connections:
        device_entries.append({
            'type': 'shock',
            'device': {
                'COYOTE_020': 'coyotev2',
                'COYOTE_030': 'coyotev3',
                'OVC_1': 'opossum',
            }.get(state['device_type'], 'coyotev3'),
            'attr': {
                'strength': dict(state['strength']),
                'upper_strength': dict(state.get('upper_strength', state['strength'])),
                'uuid': connection_uuid,
                'slot_id': state['slot_id'],
            },
        })
    return {
        'healthy': 'ok',
        'service': ACTIVE_CONTROLLER.state if ACTIVE_CONTROLLER else 'stopped',
        'devices': device_entries,
    }


def build_legacy_status_response():
    """Expose effective caps, with Coyote first for original map integrations."""
    status = build_status_response()
    devices = []
    ordered_devices = sorted(
        status['devices'],
        key=lambda device: not str(device.get('device', '')).startswith('coyote'),
    )
    for device in ordered_devices:
        attr = dict(device['attr'])
        actual_strength = dict(attr.get('strength', {}))
        upper_strength = dict(attr.get('upper_strength', actual_strength))
        attr['actual_strength'] = actual_strength
        attr['strength'] = upper_strength
        devices.append({**device, 'attr': attr})
    return {**status, 'devices': devices}


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
    runtime = getattr(ACTIVE_CONTROLLER, 'runtime', None)
    if runtime is not None:
        if runtime.router.config('coyote')['mode'] == 'bluetooth':
            # BLE consumes one 100 ms frame at a time, not an App batch.
            for _ in range(repeat):
                for channel in channels:
                    await runtime.router.broadcast_wave(channel, json.dumps([wavedata]), 'coyote')
                await asyncio.sleep(0.1)
        else:
            for channel in channels:
                await runtime.router.broadcast_wave(channel, json.dumps([wavedata] * repeat), 'coyote')
        return
    wavestr = json.dumps([wavedata] * repeat, separators=(',', ':'))
    for channel in channels:
        await DGConnection.broadcast_wave(
            channel=channel, wavestr=wavestr, device_kind='coyote',
        )


@app.route('/api/v1/shock/<channel>/<second>', methods=['GET', 'POST'])
@allow_vrchat_map_trigger
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
    submit_to_async_loop(trigger_map_event(channels, second))
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


async def trigger_map_event(channels, seconds):
    controller = ACTIVE_CONTROLLER
    runtime = controller.runtime if controller else None
    if runtime is None:
        raise RuntimeError('Device service is not running.')
    for handler in runtime.handlers:
        if isinstance(handler, ShockHandler) and handler.channel in channels:
            await handler.trigger_map_event(seconds)


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
        self.dispatcher = None
        self.chatbox_manager = None
        self.relay_packets = 0
        self.oscquery = None
        self.current_avatar_id = ''
        self.current_avatar_endpoint = ''
        self.last_avatar_metadata = None
        self.last_avatar_change_time = 0.0
        self.router = DeviceRouter(settings)

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
                    DG_CONN=self.router,
                    channel_name=channel,
                    device_kind=device_kind,
                    event_callback=self._emit,
                )
                handler.set_chatbox_manager(self.chatbox_manager)
                self.handlers.append(handler)
                for address, binding in handler.osc_bindings():
                    dispatcher.map(address, handler.osc_handler, binding)
                    logger.debug('{} 通道 {} 监听：{}', device_kind, channel, address)

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

    async def _apply_settings(self, settings):
        if settings == self.settings:
            return
        old_chatbox = self.chatbox_manager
        # Stop the OLD route before changing its selection. Keep App and OSC
        # sockets intact while clearing pending samples in both transports.
        for kind in ('coyote', 'opossum'):
            if self.router.config(kind) != settings['connections'][kind]:
                for channel in ('A', 'B'):
                    await self.router.broadcast_clear_wave(channel, kind)
        for handler in self.handlers:
            if not isinstance(handler, ShockHandler):
                continue
            next_config = settings['dglab3'][handler.device_kind][f'channel_{handler.channel.lower()}']
            if handler.shock_settings != next_config:
                for address, binding in handler.osc_bindings():
                    self.dispatcher.unmap(address, handler.osc_handler, binding)

        self.settings = settings
        if old_chatbox.settings != settings['chatbox']:
            self.chatbox_manager = AdvancedChatboxManager(settings)
        for handler in self.handlers:
            if not isinstance(handler, ShockHandler):
                continue
            channel_key = f'channel_{handler.channel.lower()}'
            next_config = settings['dglab3'][handler.device_kind][channel_key]
            if handler.shock_settings != next_config:
                await handler.hot_update(next_config)
                for address, binding in handler.osc_bindings():
                    self.dispatcher.map(address, handler.osc_handler, binding)
            handler.SETTINGS = settings
            handler.set_chatbox_manager(self.chatbox_manager)

        for connection in srv.get_ws_connections():
            apply_connection_settings = getattr(connection, 'apply_settings', None)
            if apply_connection_settings is not None:
                await apply_connection_settings(settings)
        await self.router.apply_settings(settings)
        if self.oscquery is not None:
            self.oscquery.settings = settings
        if old_chatbox is not None and old_chatbox is not self.chatbox_manager:
            same_endpoint = all(
                old_chatbox.settings.get(key, default) == self.chatbox_manager.settings.get(key, default)
                for key, default in (('osc_host', '127.0.0.1'), ('osc_port', 9000))
            )
            if self.chatbox_manager.enabled and same_endpoint:
                self.chatbox_manager.last_status_text = old_chatbox.last_status_text
                self.chatbox_manager.last_device_keys = old_chatbox.last_device_keys
            old_chatbox.cleanup(notify=not self.chatbox_manager.enabled or not same_endpoint)

    def apply_settings(self, settings, timeout=10):
        if self.loop is None or not self.loop.is_running():
            raise RuntimeError('后台服务未运行。')
        future = asyncio.run_coroutine_threadsafe(self._apply_settings(settings), self.loop)
        future.result(timeout=timeout)

    async def _device_action(self, action, kind=None):
        if action in ('connect', 'resume'):
            for handler in self.handlers:
                if isinstance(handler, ShockHandler) and (kind is None or handler.device_kind == kind):
                    handler.reset_for_avatar_change()
        if action == 'connect':
            await self.router.connect(kind)
        elif action == 'disconnect':
            await self.router.disconnect(kind)
        elif action == 'scan':
            return await self.router.scan()
        elif action == 'stop':
            await self.router.emergency_stop()
        elif action == 'resume':
            self.router.paused = False
        else:
            raise ValueError('未知设备操作。')

    def device_action(self, action, kind=None, timeout=25):
        if kind is not None and kind not in ('coyote', 'opossum'):
            raise ValueError('未知设备。')
        if not self.loop or not self.loop.is_running():
            raise RuntimeError('后台服务未运行。')
        future = asyncio.run_coroutine_threadsafe(self._device_action(action, kind), self.loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise RuntimeError('设备操作超时，已取消。') from None

    def _avatar_change_handler(self, _address, *args):
        if len(args) != 1 or not isinstance(args[0], str):
            logger.warning('Ignored invalid /avatar/change OSC message: {}', args)
            return
        avatar_id = args[0].strip()
        self.last_avatar_change_time = time.perf_counter()
        # A hidden/unknown ID can repeat across avatar changes. Clear the old
        # inputs immediately rather than waiting for identity-based snapshots.
        for handler in self.handlers:
            if isinstance(handler, ShockHandler):
                handler.reset_for_avatar_change()
        if self.oscquery is not None:
            self.oscquery.request_avatar_refresh(avatar_id)
        else:
            self._apply_avatar_snapshot(OSCQueryAvatarSnapshot(avatar_id, ()))

    def _oscquery_avatar_callback(self, snapshot):
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._apply_avatar_snapshot, snapshot)

    @staticmethod
    def _query_zones(parameter_paths):
        parameter_paths = tuple(parameter_paths)
        zones = {SPS_SOCKET: set(), SPS_PLUG: set()}
        for path in parameter_paths:
            parts = str(path).strip('/').split('/')
            if len(parts) != 6 or parts[:3] != ['avatar', 'parameters', 'OGB']:
                continue
            if parts[3] == 'Orf':
                zones[SPS_SOCKET].add(parts[4])
            elif parts[3] == 'Pen':
                zones[SPS_PLUG].add(parts[4])
        return {
            kind: {zone for zone in candidates if check(parameter_paths, zone)}
            for kind, candidates, check in (
                (SPS_SOCKET, zones[SPS_SOCKET], has_socket_depth_parameters),
                (SPS_PLUG, zones[SPS_PLUG], has_plug_depth_parameters),
            )
        }

    def _apply_avatar_snapshot(self, snapshot):
        if snapshot.observed_at and snapshot.observed_at < self.last_avatar_change_time:
            return
        metadata = (snapshot.avatar_id, snapshot.endpoint, snapshot.parameter_paths,
                    snapshot.numeric_parameter_paths)
        if metadata == self.last_avatar_metadata:
            # Live values may refresh without an avatar/catalog change. Avoid
            # rescanning local files, rebuilding lists, or logging every poll.
            if snapshot.endpoint:
                paths = (snapshot.numeric_parameter_paths if snapshot.numeric_parameter_paths is not None
                         else snapshot.parameter_paths)
                for handler in self.handlers:
                    if isinstance(handler, ShockHandler):
                        handler.set_avatar_parameters(paths)
            self._apply_query_values(snapshot)
            return
        avatar_id = snapshot.avatar_id
        if (avatar_id, snapshot.endpoint) != (self.current_avatar_id, self.current_avatar_endpoint):
            for handler in self.handlers:
                if isinstance(handler, ShockHandler):
                    handler.reset_for_avatar_change(observed_at=snapshot.observed_at or None)
            self.current_avatar_id = avatar_id
            self.current_avatar_endpoint = snapshot.endpoint
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
            readable_paths = (snapshot.numeric_parameter_paths if snapshot.numeric_parameter_paths is not None
                              else snapshot.parameter_paths)
            query_zones = self._query_zones(readable_paths)
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
            source = ''
        if snapshot.endpoint:
            parameter_paths = (snapshot.numeric_parameter_paths
                               if snapshot.numeric_parameter_paths is not None
                               else snapshot.parameter_paths)
        else:
            parameter_paths = getattr(avatar, 'numeric_parameter_paths', None)
        for handler in self.handlers:
            if isinstance(handler, ShockHandler):
                handler.set_avatar_parameters(parameter_paths)
        event = {
            'type': 'avatar',
            'avatar_id': avatar_id,
            'avatar_name': avatar.avatar_name if avatar else (avatar_id or '当前 Avatar'),
            'found': bool(snapshot.endpoint or avatar is not None),
            'source': source,
            'parameter_availability': {
                'known': parameter_paths is not None,
                'socket': bool(sockets), 'plug': bool(plugs),
                'pcs': PCS_PROXIMITY_ADDRESS in (parameter_paths or ()),
                'lms': LMS_PROXIMITY_ADDRESS in (parameter_paths or ()),
            },
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
        self.last_avatar_metadata = metadata
        self._apply_query_values(snapshot)

    def _apply_query_values(self, snapshot):
        if (self.router.paused or not snapshot.endpoint or self.dispatcher is None
                or not snapshot.observed_at):
            return
        for address, value in snapshot.parameter_values:
            for binding in self.dispatcher.handlers_for_address(address):
                handler = getattr(binding.callback, '__self__', None)
                if isinstance(handler, ShockHandler):
                    handler.receive_query_value(address, binding.args, value, snapshot.observed_at)

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
        # Match BLE connect/resume: an App must not replay inputs cached before
        # pairing. Fresh OSC/OSCQuery values can re-arm only its own routes.
        for handler in self.handlers:
            if isinstance(handler, ShockHandler) and self.router.config(handler.device_kind)['mode'] == 'app':
                handler.reset_for_avatar_change()
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
            await self.chatbox_manager.update_chatbox(
                (), self.handlers, device_states=self.router.device_states(),
            )
            await asyncio.sleep(0.5)

    async def _diagnostics_task(self):
        previous = {}
        while True:
            snapshot = self.snapshot()
            for kind, channels in snapshot['device_channels'].items():
                for channel, info in channels.items():
                    key = kind, channel
                    hosts = info['device_strengths']
                    values = (round(info['calculated_strength'], 2), info['actual_strength'], tuple(
                        (item['slot_id'], item['calculated_strength'], item['actual_strength']) for item in hosts),
                        round(info['strength_percentage'], 4))
                    if values != previous.get(key):
                        logger.bind(strength_event=True, device_kind=kind, channel=channel,
                                    calculated=values[0], actual=values[1], strength_hosts=hosts).info(
                            '计算强度 device={} channel={} calculated={:.2f} actual={} ratio={:.4f} source={} cap={} paused={} hosts={}',
                            kind, channel, values[0], values[1], info['strength_percentage'], info['trigger_type'],
                            info['upper_strength'], snapshot['output_paused'], hosts)
                        previous[key] = values
            await asyncio.sleep(0.2)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        osc_transport = None
        chatbox_task = None
        diagnostics_task = None
        try:
            dispatcher = self._build_dispatcher()
            self.dispatcher = dispatcher
            for handler in self.handlers:
                start = getattr(handler, 'start_background_jobs', None)
                if start:
                    start()
            chatbox_task = asyncio.create_task(self._chatbox_task())
            diagnostics_task = asyncio.create_task(self._diagnostics_task())

            osc_address = (self.settings['osc']['listen_host'], self.settings['osc']['listen_port'])
            osc_server = AsyncIOOSCUDPServer(osc_address, dispatcher, self.loop)
            try:
                osc_transport, _ = await osc_server.create_serve_endpoint()
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE and getattr(exc, 'winerror', None) not in (10048, 10013):
                    raise
                # OSCQuery negotiates the actual port; don't compete with VRCFT
                # or other OSC applications for the legacy fixed port.
                osc_server = AsyncIOOSCUDPServer((osc_address[0], 0), dispatcher, self.loop)
                osc_transport, _ = await osc_server.create_serve_endpoint()
                logger.info('OSC 端口 {} 不可用，自动改用 {}。',
                            osc_address[1], osc_transport.get_extra_info('sockname')[1])
            self.oscquery = OSCQueryService(
                self.settings,
                avatar_callback=self._oscquery_avatar_callback,
            )
            self.oscquery.osc_port = osc_transport.get_extra_info('sockname')[1]
            self.oscquery.start()
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
            if diagnostics_task is not None:
                diagnostics_task.cancel()
                await asyncio.gather(diagnostics_task, return_exceptions=True)
            if chatbox_task is not None:
                chatbox_task.cancel()
                await asyncio.gather(chatbox_task, return_exceptions=True)
            try:
                await self.router.emergency_stop()
            except Exception as exc:
                logger.warning('服务退出时归零未确认：{}', exc)
            await self.router.close()
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
            self.dispatcher = None
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
        device_states = self.router.device_states()
        device_ready = bool(device_states)
        device_channels = {'coyote': {}, 'opossum': {}}
        for handler in self.handlers:
            if not isinstance(handler, ShockHandler):
                continue
            info = handler.get_mode_info()
            host = self.router.hosts.get(handler.device_kind)
            if self.router.config(handler.device_kind)['mode'] == 'bluetooth':
                from srv.waveform import frame_amplitudes
                frame = host.sent_frames[handler.channel] if host and host.connected else '0A0A0A0A00000000'
                info['sent_frame'] = frame
                info['sent_strengths'] = frame_amplitudes(frame)
            if self.router.paused:
                info['sent_strengths'] = (0, 0, 0, 0)
            device_strengths = []
            for state in device_states:
                if state['device_kind'] != handler.device_kind:
                    continue
                device_strengths.append({
                    'slot_id': state['slot_id'],
                    'device_name': state['device_name'],
                    'communication_latency': state.get('communication_latency'),
                    'actual_strength': state.get('reported_strength', state['strength']).get(handler.channel),
                    'upper_strength': int(state['upper_strength'].get(handler.channel, 0)),
                    'calculated_strength': (0.0 if self.router.paused else round(info['strength_percentage'] * int(state['upper_strength'].get(handler.channel, 0)), 2)),
                })
            primary = next(iter(device_strengths), {'actual_strength': None, 'upper_strength': 0})
            device_channels[handler.device_kind][handler.channel] = {
                **info,
                'actual_strength': primary['actual_strength'],
                'upper_strength': primary['upper_strength'],
                'device_strengths': device_strengths,
                'calculated_strength': max((item['calculated_strength'] for item in device_strengths), default=0.0),
            }
        # Keep the original key as a compatibility view for API consumers.
        channels = device_channels['coyote']
        return {
            'connected': device_ready,
            'app_connected': connection is not None,
            'protocol': getattr(connection, 'protocol_version', '') if connection else '',
            'output_paused': self.router.paused,
            'bluetooth': {kind: host.snapshot() for kind, host in self.router.hosts.items()},
            'device_type': getattr(connection, 'device_type', '') if connection else '',
            'device_name': getattr(connection, 'device_name', '') if connection else '',
            'devices': list(device_states),
            'device_id': (
                device_states[0]['slot_id'] if device_ready else ''
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

    def apply_settings(self, settings, basic_settings):
        """Apply editable settings without rebinding ports or dropping the App."""
        global SETTINGS, SETTINGS_BASIC, SERVER_IP
        with self._lock:
            settings = copy.deepcopy(settings)
            basic_settings = copy.deepcopy(basic_settings)
            validate_config(settings, basic_settings)
            runtime_settings = apply_basic_settings(settings, basic_settings)
            if self.runtime is not None:
                cold_sections = ('ws', 'osc', 'oscquery', 'relay', 'web_server')
                for section in cold_sections:
                    if settings.get(section) != SETTINGS.get(section):
                        raise ValueError(f'{section} 监听设置需重启软件后生效。')
                self.runtime.apply_settings(runtime_settings)
            SETTINGS = settings
            SETTINGS_BASIC = basic_settings
            SERVER_IP = settings.get('SERVER_IP') or detect_current_ip(settings)

    def snapshot(self):
        if self.runtime is None:
            return {'connected': False, 'device_id': '', 'relay_packets': 0, 'channels': {}}
        return self.runtime.snapshot()


class ConfigFileInited(Exception):
    """Kept for source compatibility with v0.2 integrations."""


def config_init(config_dir=None, log_dir=None):
    global CONFIG_MANAGER, CONFIG_FILENAME, CONFIG_FILENAME_BASIC, SETTINGS, SETTINGS_BASIC, SERVER_IP, RUN_LOG_PATH
    CONFIG_MANAGER = ConfigManager(APP_DIR, config_dir=config_dir)
    SETTINGS, SETTINGS_BASIC = CONFIG_MANAGER.load()
    CONFIG_FILENAME = CONFIG_MANAGER.path
    CONFIG_FILENAME_BASIC = CONFIG_MANAGER.path
    SERVER_IP = SETTINGS.get('SERVER_IP') or detect_current_ip(SETTINGS)
    logger.remove()
    from srv.diagnostics import brand_log_record
    logger.configure(patcher=brand_log_record)
    from datetime import datetime
    program_dir = Path(APP_DIR)
    if getattr(sys, 'frozen', False) and program_dir.name.lower() == 'backend':
        program_dir = program_dir.parent
    preferred = Path(log_dir) if log_dir is not None else (Path(config_dir) if config_dir is not None else program_dir) / 'log'
    fallback = CONFIG_MANAGER.config_dir / 'log'
    filename = f'neko-vrc-{datetime.now():%Y%m%d-%H%M%S-%f}-{os.getpid()}.log'
    file_level = SETTINGS['log_level'] if SETTINGS.get('log_level') in ('TRACE', 'DEBUG') else 'INFO'
    log_error = None
    for destination in dict.fromkeys((preferred, fallback)):
        try:
            destination.mkdir(parents=True, exist_ok=True)
            candidate = destination / filename
            logger.add(candidate, level=file_level, rotation='20 MB', encoding='utf-8')
            RUN_LOG_PATH = candidate.resolve()
            break
        except OSError as exc:
            log_error = exc
    else:
        raise OSError('无法创建运行日志。') from log_error
    if log_error is not None:
        logger.warning('程序目录日志不可写，已回退：{}（{}）', RUN_LOG_PATH, log_error)
    logger.info('运行日志：{}', RUN_LOG_PATH)
    if sys.stderr is not None:
        logger.add(sys.stderr, level=SETTINGS.get('log_level', 'INFO'))
    return SETTINGS, SETTINGS_BASIC


def config_save():
    if CONFIG_MANAGER is None:
        raise RuntimeError('配置尚未初始化。')
    CONFIG_MANAGER.save(SETTINGS, SETTINGS_BASIC)


def legacy_main():
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


def main():
    """Launch WinUI 3; older frontends require an explicit opt-in."""
    if '--legacy-ui' in sys.argv:
        return legacy_main()
    import os
    import subprocess
    executable = APP_DIR / 'Neko-VRC.exe'
    if '--wpf-ui' in sys.argv:
        executable = APP_DIR / 'desktop' / 'NekoVRC.Desktop' / 'bin' / 'Debug' / 'net10.0-windows' / 'Neko-VRC.exe'
    if not executable.exists():
        if '--wpf-ui' not in sys.argv:
            executable = APP_DIR / 'desktop' / 'NekoVRC.WinUI' / 'bin' / 'x64' / 'Debug' / 'net10.0-windows10.0.19041.0' / 'win-x64' / 'Neko-VRC.exe'
    if not executable.exists():
        frontend = 'WPF' if '--wpf-ui' in sys.argv else 'WinUI 3'
        raise RuntimeError(f'请先构建 {frontend} 前端，或使用完整的 Neko-VRC 发布包。')
    environment = os.environ.copy()
    local_sdk = APP_DIR / '.tools' / 'dotnet'
    if local_sdk.exists():
        environment['DOTNET_ROOT'] = str(local_sdk)
    subprocess.Popen([str(executable)], cwd=str(APP_DIR), env=environment,
                     creationflags=subprocess.CREATE_NO_WINDOW)


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
