import asyncio
import copy
import json
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from pythonosc.udp_client import SimpleUDPClient
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosedError

import srv
import shocking_vrchat
from shocking_vrchat import DeviceRuntime, ServiceController
from srv.config_manager import (
    DEFAULT_BASIC_SETTINGS,
    DEFAULT_SETTINGS,
    ConfigManager,
    apply_basic_settings,
    parse_endpoint,
    validate_config,
)
from srv.connector.coyotev4ws import DGV4Connection
from srv.oscquery import OSCQueryAvatarSnapshot, OSCQueryService, parse_vrchat_avatar_node
from srv.udp_relay import create_udp_relay
from srv.steamvr_autostart import (
    APPLICATION_KEY,
    OpenVRApplicationsBackend,
    configure_steamvr_autostart,
    write_manifest,
)
from srv.win32_ui import COPYRIGHT_ENTRIES, FRONTEND_CONTRIBUTORS, DesktopApplication


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def free_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ConfigManagerTests(unittest.TestCase):
    def test_first_run_creates_unified_appdata_config(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConfigManager(app_dir=Path(root) / 'app', config_dir=Path(root) / 'config')
            settings, basic = manager.load()
            self.assertTrue(manager.path.exists())
            self.assertEqual(manager.path.name, 'settings-v0.9.yaml')
            self.assertIsNotNone(settings['ws']['master_uuid'])
            self.assertEqual(basic['dglab3']['coyote']['channel_a']['strength_limit'], 100)
            self.assertEqual(basic['dglab3']['opossum']['channel_a']['strength_limit'], 100)
            self.assertFalse(settings['general']['steamvr_auto_start'])

    def test_v02_files_are_migrated_without_removal(self):
        with tempfile.TemporaryDirectory() as root:
            app_dir = Path(root) / 'app'
            config_dir = Path(root) / 'config'
            app_dir.mkdir()
            advanced = copy.deepcopy(DEFAULT_SETTINGS)
            basic = {
                'version': 'v0.2',
                'dglab3': {
                    channel: {
                        'trigger_type': 'sps_socket' if channel == 'channel_a' else 'sps_plug',
                        'zone': '*',
                        'strength_limit': 77,
                    }
                    for channel in ('channel_a', 'channel_b')
                },
            }
            advanced['version'] = 'v0.2'
            advanced.pop('relay')
            advanced['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
            advanced_path = app_dir / 'settings-advanced-v0.2.yaml'
            basic_path = app_dir / 'settings-v0.2.yaml'
            advanced_path.write_text(yaml.safe_dump(advanced), encoding='utf-8')
            basic_path.write_text(yaml.safe_dump(basic), encoding='utf-8')

            manager = ConfigManager(app_dir=app_dir, config_dir=config_dir)
            settings, migrated_basic = manager.load()

            self.assertEqual(migrated_basic['dglab3']['coyote']['channel_a']['strength_limit'], 77)
            self.assertEqual(migrated_basic['dglab3']['opossum']['channel_a']['strength_limit'], 77)
            self.assertIn('relay', settings)
            self.assertTrue(advanced_path.exists())
            self.assertTrue(basic_path.exists())
            self.assertEqual(manager.migrated_from, str(app_dir))

    def test_v03_unified_config_migrates_strength_but_drops_legacy_modes(self):
        with tempfile.TemporaryDirectory() as root:
            app_dir = Path(root) / 'app'
            config_dir = Path(root) / 'config'
            app_dir.mkdir()
            config_dir.mkdir()
            old_settings = copy.deepcopy(DEFAULT_SETTINGS)
            old_settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
            old_settings['dglab3']['channel_a'] = {
                'mode_config': {
                    'distance': {'freq_ms': 20},
                    'trigger_range': {'bottom': 0.2, 'top': 0.8},
                },
            }
            old_basic = {
                'dglab3': {
                    'channel_a': {
                        'avatar_params': ['/avatar/parameters/legacy'],
                        'mode': 'distance',
                        'strength_limit': 73,
                    },
                    'channel_b': {
                        'avatar_params': ['/avatar/parameters/legacy'],
                        'mode': 'shock',
                        'strength_limit': 64,
                    },
                },
            }
            old_path = config_dir / 'settings-v0.3.yaml'
            old_path.write_text(yaml.safe_dump({
                'version': 'v0.3',
                'settings': old_settings,
                'channels': old_basic,
            }), encoding='utf-8')

            manager = ConfigManager(app_dir=app_dir, config_dir=config_dir)
            settings, basic = manager.load()

            self.assertEqual(basic['dglab3']['coyote']['channel_a']['strength_limit'], 73)
            self.assertEqual(basic['dglab3']['opossum']['channel_a']['strength_limit'], 73)
            self.assertEqual(basic['dglab3']['coyote']['channel_b']['strength_limit'], 64)
            self.assertEqual(basic['dglab3']['opossum']['channel_b']['strength_limit'], 64)
            self.assertNotIn('avatar_params', basic['dglab3']['coyote']['channel_a'])
            self.assertNotIn('mode', basic['dglab3']['coyote']['channel_a'])
            self.assertNotIn('mode_config', settings['dglab3']['coyote']['channel_a'])
            self.assertTrue(old_path.exists())

    def test_v07_shared_settings_are_migrated_to_both_device_menus(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            legacy_path = root_path / 'settings-v0.7.yaml'
            legacy_settings = {
                'dglab3': {
                    'channel_a': {
                        'depth': {'freq_ms': 23, 'waveform': srv.WAVEFORM_NAMES[1]},
                    },
                    'channel_b': {
                        'depth': {'freq_ms': 42, 'waveform': srv.WAVEFORM_NAMES[2]},
                    },
                },
            }
            legacy_basic = {'dglab3': {}}
            for channel, value in (('channel_a', 81), ('channel_b', 62)):
                legacy_basic['dglab3'][channel] = {
                    'trigger_type': 'sps_socket' if channel == 'channel_a' else 'sps_plug',
                    'zone': '*',
                    'strength_limit': value,
                }
            legacy_path.write_text(yaml.safe_dump({
                'version': 'v0.7',
                'settings': legacy_settings,
                'channels': legacy_basic,
            }), encoding='utf-8')

            manager = ConfigManager(app_dir=root_path, config_dir=root_path)
            _settings, basic = manager.load()

            self.assertEqual(manager.migrated_from, str(legacy_path))
            self.assertEqual(basic['dglab3']['coyote']['channel_a']['strength_limit'], 81)
            self.assertEqual(basic['dglab3']['opossum']['channel_a']['strength_limit'], 81)
            self.assertEqual(basic['dglab3']['coyote']['channel_b']['strength_limit'], 62)
            self.assertEqual(basic['dglab3']['opossum']['channel_b']['strength_limit'], 62)
            self.assertEqual(_settings['dglab3']['coyote']['channel_a']['depth']['freq_ms'], 23)
            self.assertEqual(_settings['dglab3']['opossum']['channel_a']['depth']['freq_ms'], 23)
            self.assertEqual(
                _settings['dglab3']['coyote']['channel_b']['depth']['waveform'],
                srv.WAVEFORM_NAMES[2],
            )
            self.assertEqual(
                _settings['dglab3']['opossum']['channel_b']['depth']['waveform'],
                srv.WAVEFORM_NAMES_BY_DEVICE['opossum'][0],
            )
            self.assertTrue(manager.path.exists())
            self.assertTrue(legacy_path.exists())

    def test_direct_ble_settings_are_removed_during_migration(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
            settings['device'] = {'type': 'opossum'}
            settings['opossum'] = {'device_name': '47L127000'}
            manager = ConfigManager(app_dir=root_path, config_dir=root_path)
            manager.path.write_text(yaml.safe_dump({
                'version': 'v0.6',
                'settings': settings,
                'channels': copy.deepcopy(DEFAULT_BASIC_SETTINGS),
            }), encoding='utf-8')

            migrated, _basic = manager.load()

            self.assertNotIn('device', migrated)
            self.assertNotIn('opossum', migrated)
            saved = yaml.safe_load(manager.path.read_text(encoding='utf-8'))
            self.assertNotIn('device', saved['settings'])
            self.assertNotIn('opossum', saved['settings'])

    def test_default_channels_enable_all_depth_sources(self):
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        for device_kind in ('coyote', 'opossum'):
            for channel in ('channel_a', 'channel_b'):
                config = basic['dglab3'][device_kind][channel]
                self.assertEqual(config['socket_zone'], '*')
                self.assertEqual(config['plug_zone'], '*')
                self.assertTrue(config['extra_parameters']['enabled'])
                self.assertEqual(len(config['extra_parameters']['paths']), 3)

    def test_steamvr_auto_start_requires_a_boolean(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['general']['steamvr_auto_start'] = 'true'
        with self.assertRaisesRegex(ValueError, 'general.steamvr_auto_start'):
            validate_config(settings, copy.deepcopy(DEFAULT_BASIC_SETTINGS))

    def test_endpoint_parser_supports_ipv4_and_ipv6(self):
        self.assertEqual(parse_endpoint('127.0.0.1:9001'), ('127.0.0.1', 9001))
        self.assertEqual(parse_endpoint('[::1]:9021'), ('::1', 9021))

    def test_relay_cannot_bind_to_its_internal_target(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['relay']['enabled'] = True
        settings['relay']['internal_port'] = settings['relay']['listen_port']
        with self.assertRaises(ValueError):
            validate_config(settings, basic)

    def test_relay_rejects_vrcft_loop_and_duplicate_targets(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['relay']['enabled'] = True
        settings['relay']['vrcft_port'] = settings['relay']['listen_port']
        with self.assertRaises(ValueError):
            validate_config(settings, basic)

        settings['relay']['vrcft_port'] = 9011
        settings['relay']['internal_host'] = settings['relay']['vrcft_host']
        settings['relay']['internal_port'] = settings['relay']['vrcft_port']
        with self.assertRaises(ValueError):
            validate_config(settings, basic)


class DatagramReceiver(asyncio.DatagramProtocol):
    def __init__(self, future):
        self.future = future

    def datagram_received(self, data, address):
        if not self.future.done():
            self.future.set_result((data, address))


class UDPRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_datagram_is_forwarded_unchanged_to_both_targets(self):
        loop = asyncio.get_running_loop()
        futures = [loop.create_future(), loop.create_future()]
        receivers = []
        targets = []
        for future in futures:
            transport, _ = await loop.create_datagram_endpoint(
                lambda item=future: DatagramReceiver(item),
                local_addr=('127.0.0.1', 0),
            )
            receivers.append(transport)
            targets.append(transport.get_extra_info('sockname'))

        relay_transport, _ = await create_udp_relay(
            loop,
            ('127.0.0.1', 0),
            targets,
        )
        sender, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            remote_addr=relay_transport.get_extra_info('sockname'),
        )
        try:
            packet = b'\x2favatar\x00\x00raw-osc-payload'
            sender.sendto(packet)
            received = await asyncio.wait_for(asyncio.gather(*futures), timeout=2)
            self.assertEqual([item[0] for item in received], [packet, packet])
        finally:
            sender.close()
            relay_transport.close()
            for receiver in receivers:
                receiver.close()


class DeviceRuntimeApplySettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconfigures_existing_handlers_and_dispatcher_in_place(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['chatbox']['enable'] = False
        runtime = DeviceRuntime(apply_basic_settings(settings, basic))
        runtime.dispatcher = runtime._build_dispatcher()
        handlers = tuple(runtime.handlers)

        basic['dglab3']['coyote']['channel_a']['plug_zone'] = 'Hot_Plug'
        basic['dglab3']['coyote']['channel_a']['strength_limit'] = 75
        updated = apply_basic_settings(settings, basic)
        await runtime._apply_settings(updated)

        self.assertEqual(tuple(runtime.handlers), handlers)
        self.assertEqual(runtime.handlers[0].plug_zone, 'Hot_Plug')
        self.assertTrue(any(
            '/OGB/Pen/Hot_Plug/' in address
            for address, _binding in runtime.handlers[0].osc_bindings()
        ))


class OSCQueryServiceTests(unittest.TestCase):
    def make_service(self):
        settings = apply_basic_settings(
            copy.deepcopy(DEFAULT_SETTINGS),
            copy.deepcopy(DEFAULT_BASIC_SETTINGS),
        )
        return OSCQueryService(settings)

    def test_root_node_uses_nested_contents(self):
        root = self.make_service().root_node()
        node = root['CONTENTS']['avatar']
        for part in ('parameters', 'OGB', 'Orf', '*'):
            self.assertIn('CONTENTS', node)
            node = node['CONTENTS'][part]
        leaf = node['CONTENTS']['PenOthers']
        self.assertEqual(leaf['FULL_PATH'], '/avatar/parameters/OGB/Orf/*/PenOthers')
        self.assertEqual(leaf['TYPE'], 'f')
        self.assertEqual(leaf['ACCESS'], 2)

    def test_dynamic_wildcard_parameter_can_be_queried(self):
        node = self.make_service().node_for(
            '/avatar/parameters/OGB/Pen/CurrentPlug/PenOthers'
        )
        self.assertEqual(
            node,
            {
                'FULL_PATH': '/avatar/parameters/OGB/Pen/CurrentPlug/PenOthers',
                'TYPE': 'f',
                'ACCESS': 2,
            },
        )

    def test_vrchat_avatar_node_exposes_current_id_and_parameter_tree(self):
        document = {
            'FULL_PATH': '/avatar',
            'CONTENTS': {
                'change': {
                    'FULL_PATH': '/avatar/change',
                    'TYPE': 's',
                    'VALUE': ['avtr_current'],
                },
                'parameters': {
                    'FULL_PATH': '/avatar/parameters',
                    'CONTENTS': {
                        'OGB': {
                            'FULL_PATH': '/avatar/parameters/OGB',
                            'CONTENTS': {
                                'Pen': {
                                    'FULL_PATH': '/avatar/parameters/OGB/Pen',
                                    'CONTENTS': {
                                        'Query_Plug': {
                                            'FULL_PATH': '/avatar/parameters/OGB/Pen/Query_Plug',
                                            'CONTENTS': {
                                                'PenOthers': {
                                                    'FULL_PATH': '/avatar/parameters/OGB/Pen/Query_Plug/PenOthers',
                                                    'TYPE': 'f',
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
        snapshot = parse_vrchat_avatar_node(document, 'http://127.0.0.1:9001')
        self.assertEqual(snapshot.avatar_id, 'avtr_current')
        self.assertEqual(snapshot.endpoint, 'http://127.0.0.1:9001')
        self.assertIn(
            '/avatar/parameters/OGB/Pen/Query_Plug/PenOthers',
            snapshot.parameter_paths,
        )

    def test_runtime_uses_oscquery_tree_as_avatar_zone_source(self):
        events = []
        runtime = DeviceRuntime(copy.deepcopy(DEFAULT_SETTINGS), event_callback=events.append)
        local_avatar = SimpleNamespace(
            avatar_name='Current Avatar',
            sockets=(),
            plugs=(SimpleNamespace(kind='sps_plug', zone_id='Query_Plug', label='Query Plug'),),
        )
        snapshot = OSCQueryAvatarSnapshot(
            'avtr_current',
            ('/avatar/parameters/OGB/Pen/Query_Plug/PenOthers',),
            'http://127.0.0.1:9001',
        )
        with patch('shocking_vrchat.find_avatar_sps_config', return_value=local_avatar):
            runtime._apply_avatar_snapshot(snapshot)
        event = events[-1]
        self.assertEqual(event['source'], 'OSCQuery')
        self.assertEqual(event['avatar_id'], 'avtr_current')
        self.assertEqual(event['plugs'], [{'zone_id': 'Query_Plug', 'label': 'Query Plug'}])


class V4ConnectionStateTests(unittest.IsolatedAsyncioTestCase):
    def make_connection(self):
        connection = object.__new__(DGV4Connection)
        connection.devices = {}
        connection.device_states = {}
        connection.slot_id = None
        connection.device_type = None
        connection.device_name = None
        connection.strength = {'A': 0, 'B': 0}
        connection.strength_max = {'A': 200, 'B': 200}
        connection.strength_limits = {
            'coyote': {'A': 80, 'B': 90},
            'opossum': {'A': 120, 'B': 130},
        }
        connection.strength_limit = connection.strength_limits['coyote']
        return connection

    async def test_coyote_and_opossum_keep_independent_state_and_operations(self):
        connection = self.make_connection()
        connection._replace_devices([
            {
                'slotId': 'coyote',
                'type': 'COYOTE_030',
                'name': 'Coyote',
                'props': {'intensityA': 50, 'intensityB': 40},
                'slotState': {
                    'hasDevice': True,
                    'channelA': {'intensityMax': 60},
                    'channelB': {'intensityMax': 70},
                },
            },
            {
                'slotId': 'opossum',
                'type': 'OVC_1',
                'name': '负鼠',
                'props': {'intensityA': 10, 'intensityB': 20},
                'slotState': {'hasDevice': True},
            },
        ])

        self.assertEqual(set(connection.device_states), {'coyote', 'opossum'})
        self.assertEqual(connection.strength, {'A': 50, 'B': 40})
        self.assertEqual(connection.strength_max, {'A': 60, 'B': 70})
        self.assertEqual(connection.device_states['opossum']['strength'], {'A': 10, 'B': 20})
        self.assertEqual(connection.device_states['opossum']['strength_max'], {'A': 200, 'B': 200})
        self.assertEqual(connection.get_upper_strength('A', slot_id='coyote'), 60)
        self.assertEqual(connection.get_upper_strength('A', slot_id='opossum'), 120)

        requests = []

        async def record_request(method, data=None):
            requests.append((method, data))
            return True

        connection._send_request = record_request
        await connection._sync_strength_limits()
        strength_requests = [data for method, data in requests if method == 'device.op']
        self.assertEqual(
            [(item['s'], item['c'], item['v']) for item in strength_requests],
            [('coyote', 0, 10), ('coyote', 1, 30)],
        )
        self.assertEqual(connection.device_states['opossum']['strength'], {'A': 10, 'B': 20})

        requests.clear()
        await connection.send_wave('A', '["1919181864643219"]')
        strength_requests = [
            data for method, data in requests
            if method == 'device.op' and data['t'] in (3, 7)
        ]
        self.assertEqual(
            [(item['s'], item['c'], item['v']) for item in strength_requests],
            [('opossum', 0, 110)],
        )
        waves = [
            data for method, data in requests
            if method == 'device.op' and data['t'] == 0
        ]
        self.assertEqual([item['s'] for item in waves], ['coyote', 'opossum'])
        self.assertEqual(waves[0]['v'], ['1919181864643219'])
        self.assertEqual(waves[1]['v'], ['0A0A0A0A64643219'])

        requests.clear()
        await connection.clear_wave('A', device_kind='opossum')
        self.assertEqual(
            requests,
            [
                ('device.op.clear', {'s': 'opossum', 'c': 0}),
                ('device.op', {'s': 'opossum', 't': 7, 'c': 0, 'p': 1, 'v': 0}),
            ],
        )
        self.assertEqual(connection.device_states['opossum']['strength']['A'], 0)

        requests.clear()
        await connection.send_wave('A', '["1919181864643219"]', device_kind='opossum')
        waves = [
            data for method, data in requests
            if method == 'device.op' and data['t'] == 0
        ]
        self.assertEqual([item['s'] for item in waves], ['opossum'])
        self.assertEqual(waves[0]['v'], ['0A0A0A0A64643219'])


class ServiceControllerTests(unittest.TestCase):
    def test_apply_settings_keeps_the_existing_runtime(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        original_settings = shocking_vrchat.SETTINGS
        original_basic = shocking_vrchat.SETTINGS_BASIC
        applied = []

        class Runtime:
            def apply_settings(self, value):
                applied.append(value)

        controller = ServiceController()
        runtime = Runtime()
        controller.runtime = runtime
        shocking_vrchat.SETTINGS = copy.deepcopy(settings)
        shocking_vrchat.SETTINGS_BASIC = copy.deepcopy(basic)
        try:
            basic['dglab3']['opossum']['channel_a']['strength_limit'] = 77
            controller.apply_settings(settings, basic)
            self.assertIs(controller.runtime, runtime)
            self.assertEqual(
                applied[0]['dglab3']['opossum']['channel_a']['strength_limit'], 77,
            )
        finally:
            shocking_vrchat.SETTINGS = original_settings
            shocking_vrchat.SETTINGS_BASIC = original_basic

    def test_status_api_reports_the_actual_device_type(self):
        connection = SimpleNamespace(
            is_device_ready=lambda: True,
            uuid='test-device',
            get_device_states=lambda: ({
                'slot_id': 'coyote-slot',
                'device_type': 'COYOTE_030',
                'strength': {'A': 80, 'B': 60},
            }, {
                'slot_id': 'opossum-slot',
                'device_type': 'OVC_1',
                'strength': {'A': 100, 'B': 80},
            }),
        )
        with patch('shocking_vrchat.srv.get_ws_connections', return_value=(connection,)):
            response = shocking_vrchat.build_status_response()
        self.assertEqual(
            [(item['device'], item['attr']['slot_id']) for item in response['devices']],
            [('coyotev3', 'coyote-slot'), ('opossum', 'opossum-slot')],
        )

    def test_service_can_start_and_stop_without_a_device(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False
        controller = ServiceController()
        try:
            controller.start(settings, basic)
            self.assertEqual(controller.state, 'running')
            self.assertFalse(controller.snapshot()['connected'])
        finally:
            controller.stop()
        self.assertEqual(controller.state, 'stopped')

    def test_real_udp_dispatch_does_not_attempt_to_reply_with_a_task(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False
        basic['dglab3']['coyote']['channel_a']['plug_zone'] = 'Integration'
        controller = ServiceController()
        loop_errors = []
        try:
            controller.start(settings, basic)
            controller.runtime.loop.call_soon_threadsafe(
                controller.runtime.loop.set_exception_handler,
                lambda _loop, context: loop_errors.append(
                    context.get('exception') or context.get('message')
                ),
            )
            client = SimpleUDPClient('127.0.0.1', settings['osc']['listen_port'])
            try:
                # Invalid payloads are ignored without escaping into asyncio's
                # exception handler or changing the last valid value.
                address = '/avatar/parameters/OGB/Pen/Integration/PenOthers'
                client.send_message(address, ['invalid'])
                client.send_message(address, [0.2, 0.3])
                client.send_message(address, [0.4])
                deadline = time.monotonic() + 2
                raw_value = -1
                while time.monotonic() < deadline:
                    raw_value = (
                        controller.snapshot()['channels']
                        .get('A', {})
                        .get('raw_value', -1)
                    )
                    if abs(raw_value - 0.4) < 0.00001:
                        break
                    time.sleep(0.02)
                self.assertAlmostEqual(raw_value, 0.4, places=5)
                time.sleep(0.1)
                self.assertEqual(loop_errors, [])
            finally:
                client._sock.close()
        finally:
            controller.stop()

    def test_real_udp_any_plug_tracks_the_deepest_active_zone(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False
        basic['dglab3']['coyote']['channel_a']['plug_zone'] = '*'
        controller = ServiceController()
        client = None
        try:
            controller.start(settings, basic)
            client = SimpleUDPClient('127.0.0.1', settings['osc']['listen_port'])
            client.send_message('/avatar/parameters/OGB/Pen/First/PenOthers', [0.3])
            client.send_message('/avatar/parameters/OGB/Pen/Second/PenOthers', [0.8])
            deadline = time.monotonic() + 2
            info = {}
            while time.monotonic() < deadline:
                info = controller.snapshot()['channels'].get('A', {})
                if info.get('active_zone') == 'Second':
                    break
                time.sleep(0.02)
            self.assertEqual(info.get('active_zone'), 'Second')
            self.assertAlmostEqual(info.get('strength_percentage', 0), 0.8)
        finally:
            if client is not None:
                client._sock.close()
            controller.stop()

    def test_avatar_change_event_is_used_as_query_fallback(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False
        events = []
        avatar = SimpleNamespace(
            avatar_name='OSC Current Avatar',
            sockets=(SimpleNamespace(zone_id='Socket_One', label='Socket One'),),
            plugs=(SimpleNamespace(zone_id='Plug_One', label='Plug One'),),
        )
        controller = ServiceController(event_callback=events.append)
        client = None
        with patch('shocking_vrchat.find_avatar_sps_config', return_value=avatar) as lookup:
            try:
                controller.start(settings, basic)
                client = SimpleUDPClient('127.0.0.1', settings['osc']['listen_port'])
                client.send_message('/avatar/change', ['avtr_current'])
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not any(
                    event.get('type') == 'avatar' for event in events
                ):
                    time.sleep(0.02)
                avatar_event = next(event for event in events if event.get('type') == 'avatar')
                self.assertEqual(avatar_event['avatar_id'], 'avtr_current')
                self.assertEqual(avatar_event['avatar_name'], 'OSC Current Avatar')
                self.assertEqual(avatar_event['sockets'][0]['zone_id'], 'Socket_One')
                lookup.assert_called_once_with('avtr_current', shocking_vrchat.APP_DIR)
            finally:
                if client is not None:
                    client._sock.close()
                controller.stop()

    def test_real_udp_socket_root_tip_is_converted_to_depth(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False
        basic['dglab3']['coyote']['channel_a']['socket_zone'] = 'Integration_Socket'
        controller = ServiceController()
        client = None
        try:
            controller.start(settings, basic)
            client = SimpleUDPClient('127.0.0.1', settings['osc']['listen_port'])
            root = '/avatar/parameters/OGB/Orf/Integration_Socket/PenOthersNewRoot'
            tip = '/avatar/parameters/OGB/Orf/Integration_Socket/PenOthersNewTip'
            for _ in range(4):
                client.send_message(root, [0.2])
                client.send_message(tip, [0.6])
            client.send_message(root, [0.8])
            client.send_message(tip, [1.0])
            deadline = time.monotonic() + 2
            depth = -1
            while time.monotonic() < deadline:
                depth = controller.snapshot()['channels'].get('A', {}).get('strength_percentage', -1)
                if abs(depth - 0.5) < 0.01:
                    break
                time.sleep(0.02)
            self.assertAlmostEqual(depth, 0.5, places=2)
        finally:
            if client is not None:
                client._sock.close()
            controller.stop()

    def test_failed_web_thread_start_closes_server_and_runtime(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False

        class FakeServer:
            closed = False

            def serve_forever(self):
                pass

            def server_close(self):
                self.closed = True

        class FailingThread:
            def start(self):
                raise RuntimeError('thread start failed')

        fake_server = FakeServer()
        real_thread = threading.Thread

        def thread_factory(*args, **kwargs):
            if kwargs.get('name') == 'status-web-server':
                return FailingThread()
            return real_thread(*args, **kwargs)

        controller = ServiceController()
        with patch('shocking_vrchat.make_server', return_value=fake_server), patch(
            'shocking_vrchat.Thread', side_effect=thread_factory
        ):
            with self.assertRaisesRegex(RuntimeError, 'thread start failed'):
                controller.start(settings, basic)

        self.assertTrue(fake_server.closed)
        self.assertIsNone(controller.web_server)
        self.assertIsNone(controller.web_thread)
        self.assertIsNone(controller.runtime)
        self.assertEqual(controller.state, 'error')


class WebSocketPairingTests(unittest.IsolatedAsyncioTestCase):
    def make_settings(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False
        for device_kind in ('coyote', 'opossum'):
            basic['dglab3'][device_kind]['channel_a']['plug_zone'] = 'V4_Test'
            basic['dglab3'][device_kind]['channel_b']['socket_zone'] = 'Unused'
            basic['dglab3'][device_kind]['channel_b']['plug_zone'] = 'Unused'
        return settings, basic

    async def wait_for_message_type(self, websocket, frame_type, timeout=2):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.fail(f'未收到 V4 {frame_type} 帧')
            frame = json.loads(await asyncio.wait_for(websocket.recv(), remaining))
            if frame.get('type') == frame_type:
                return frame

    async def test_official_v4_pairing_snapshot_ping_and_wave_flow(self):
        settings, basic = self.make_settings()
        controller = ServiceController()
        client = None
        try:
            controller.start(settings, basic)
            uri = (
                f'ws://127.0.0.1:{settings["ws"]["listen_port"]}/'
                f'?tid={settings["ws"]["master_uuid"]}'
            )
            async with websocket_connect(uri) as websocket:
                hello = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                attached = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                devices_request = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                self.assertEqual(hello['type'], 'hello')
                self.assertRegex(hello['clientId'], r'^[0-9a-f]{8}$')
                self.assertEqual(
                    attached,
                    {'type': 'controller_attached', 'clientId': settings['ws']['master_uuid']},
                )
                self.assertEqual(devices_request['type'], 'message')
                self.assertEqual(devices_request['data']['m'], 'devices.get')
                self.assertTrue(controller.snapshot()['app_connected'])
                self.assertFalse(controller.snapshot()['connected'])

                await websocket.send(json.dumps({'type': 'ping'}))
                pong = await self.wait_for_message_type(websocket, 'pong')
                self.assertIsInstance(pong['ts'], int)

                await websocket.send(json.dumps({
                    'type': 'message',
                    'data': {
                        't': 'ev',
                        'ev': 'devices.snapshot',
                        'devices': [{
                            'slotId': 'coyote-slot',
                            'name': 'Coyote 3',
                            'type': 'COYOTE_030',
                            'props': {'intensityA': 12, 'intensityB': 7},
                            'slotState': {
                                'hasDevice': True,
                                'channelA': {'intensityMax': 80},
                                'channelB': {'intensityMax': 60},
                            },
                        }],
                    },
                }))
                deadline = time.monotonic() + 2
                snapshot = {}
                while time.monotonic() < deadline:
                    snapshot = controller.snapshot()
                    if snapshot.get('connected'):
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(snapshot['connected'])
                self.assertEqual(snapshot['protocol'], 'v4')
                self.assertEqual(snapshot['device_id'], 'coyote-slot')
                self.assertEqual(snapshot['channels']['A']['upper_strength'], 80)
                self.assertEqual(snapshot['channels']['A']['actual_strength'], 80)

                client = SimpleUDPClient('127.0.0.1', settings['osc']['listen_port'])
                client.send_message('/avatar/parameters/OGB/Pen/V4_Test/PenOthers', [0.4])
                deadline = asyncio.get_running_loop().time() + 2
                operation = None
                strength_operations = []
                while asyncio.get_running_loop().time() < deadline:
                    remaining = deadline - asyncio.get_running_loop().time()
                    frame = json.loads(await asyncio.wait_for(websocket.recv(), remaining))
                    data = frame.get('data') or {}
                    if frame.get('type') == 'message' and data.get('m') == 'device.op':
                        candidate = data['data']
                        if candidate.get('t') == 0:
                            operation = candidate
                            break
                        strength_operations.append(candidate)
                self.assertIsNotNone(operation)
                self.assertEqual(
                    [(item['c'], item['t'], item['v']) for item in strength_operations],
                    [(0, 3, 68), (1, 3, 53)],
                )
                self.assertEqual(operation['s'], 'coyote-slot')
                self.assertEqual(operation['t'], 0)
                self.assertEqual(operation['c'], 0)
                self.assertEqual(operation['d'], 100)
                self.assertTrue(operation['im'])

                await websocket.send(json.dumps({
                    'type': 'message',
                    'data': {
                        't': 'ev',
                        'ev': 'slots.patch',
                        'slots': [{
                            'slotId': 'coyote-slot',
                            'slotState': {'hasDevice': False},
                        }],
                    },
                }))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and controller.snapshot()['connected']:
                    await asyncio.sleep(0.02)
                snapshot = controller.snapshot()
                self.assertFalse(snapshot['connected'])
                self.assertTrue(snapshot['app_connected'])
        finally:
            if client is not None:
                client._sock.close()
            controller.stop()
            self.assertEqual(srv.get_ws_connections(), ())

    async def test_v4_opossum_is_auto_detected_and_receives_ovc_waveform(self):
        settings, basic = self.make_settings()
        settings['dglab3']['opossum']['channel_a']['depth']['waveform'] = (
            srv.WAVEFORM_NAMES_BY_DEVICE['opossum'][6]
        )
        controller = ServiceController()
        client = None
        try:
            controller.start(settings, basic)
            uri = (
                f'ws://127.0.0.1:{settings["ws"]["listen_port"]}/'
                f'?tid={settings["ws"]["master_uuid"]}'
            )
            async with websocket_connect(uri) as websocket:
                for _ in range(3):
                    await asyncio.wait_for(websocket.recv(), 2)
                await websocket.send(json.dumps({
                    'type': 'message',
                    'data': {
                        't': 'ev',
                        'ev': 'devices.snapshot',
                        'devices': [{
                            'slotId': 'ovc-slot',
                            'name': '负鼠振动控制器',
                            'type': 'OVC_1',
                            'props': {'intensityA': 0, 'intensityB': 0},
                            'slotState': {'hasDevice': True},
                        }],
                    },
                }))
                deadline = time.monotonic() + 2
                snapshot = {}
                while time.monotonic() < deadline:
                    snapshot = controller.snapshot()
                    if snapshot.get('connected'):
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(snapshot['connected'])
                self.assertEqual(snapshot['device_type'], 'OVC_1')
                self.assertEqual(snapshot['device_name'], '负鼠振动控制器')

                # A zero-intensity Opossum must remain silent until OSC
                # produces a real SPS trigger.
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(websocket.recv(), 0.2)

                client = SimpleUDPClient('127.0.0.1', settings['osc']['listen_port'])
                client.send_message('/avatar/parameters/OGB/Pen/V4_Test/PenOthers', [0.5])
                strength_operation = None
                operation = None
                deadline = asyncio.get_running_loop().time() + 2
                while asyncio.get_running_loop().time() < deadline:
                    frame = json.loads(await asyncio.wait_for(
                        websocket.recv(), deadline - asyncio.get_running_loop().time()
                    ))
                    data = frame.get('data') or {}
                    candidate = data.get('data') or {}
                    if data.get('m') == 'device.op' and candidate.get('t') == 3:
                        strength_operation = candidate
                    if data.get('m') == 'device.op' and candidate.get('t') == 0:
                        operation = candidate
                        break
                self.assertIsNotNone(strength_operation)
                self.assertEqual(strength_operation['v'], 100)
                self.assertIsNotNone(operation)
                self.assertEqual(operation['s'], 'ovc-slot')
                self.assertTrue(operation['v'][0].startswith('0A0A0A0A'))
        finally:
            if client is not None:
                client._sock.close()
            controller.stop()
            self.assertEqual(srv.get_ws_connections(), ())

    async def test_one_v4_socket_controls_coyote_and_opossum_independently(self):
        settings, basic = self.make_settings()
        settings['dglab3']['coyote']['channel_a']['depth']['waveform'] = (
            srv.WAVEFORM_NAMES_BY_DEVICE['coyote'][6]
        )
        settings['dglab3']['opossum']['channel_a']['depth']['waveform'] = (
            srv.WAVEFORM_NAMES_BY_DEVICE['opossum'][6]
        )
        for device_kind in ('coyote', 'opossum'):
            basic['dglab3'][device_kind]['channel_b']['socket_zone'] = 'Unused'
            basic['dglab3'][device_kind]['channel_b']['plug_zone'] = 'Unused'
        basic['dglab3']['coyote']['channel_a']['strength_limit'] = 80
        basic['dglab3']['coyote']['channel_b']['strength_limit'] = 60
        basic['dglab3']['opossum']['channel_a']['strength_limit'] = 120
        basic['dglab3']['opossum']['channel_b']['strength_limit'] = 130
        controller = ServiceController()
        client = None
        try:
            controller.start(settings, basic)
            uri = (
                f'ws://127.0.0.1:{settings["ws"]["listen_port"]}/'
                f'?tid={settings["ws"]["master_uuid"]}'
            )
            async with websocket_connect(uri) as websocket:
                for _ in range(3):
                    await asyncio.wait_for(websocket.recv(), 2)
                await websocket.send(json.dumps({
                    'type': 'message',
                    'data': {
                        't': 'ev',
                        'ev': 'devices.snapshot',
                        'devices': [
                            {
                                'slotId': 'coyote-slot',
                                'name': 'Coyote',
                                'type': 'COYOTE_030',
                                'props': {'intensityA': 10, 'intensityB': 20},
                                'slotState': {
                                    'hasDevice': True,
                                    'channelA': {'intensityMax': 70},
                                    'channelB': {'intensityMax': 90},
                                },
                            },
                            {
                                'slotId': 'opossum-slot',
                                'name': 'Opossum',
                                'type': 'OVC_1',
                                'props': {'intensityA': 30, 'intensityB': 40},
                                'slotState': {'hasDevice': True},
                            },
                        ],
                    },
                }))

                strength_operations = []
                deadline = asyncio.get_running_loop().time() + 2
                while len(strength_operations) < 2:
                    remaining = deadline - asyncio.get_running_loop().time()
                    self.assertGreater(remaining, 0)
                    frame = json.loads(await asyncio.wait_for(websocket.recv(), remaining))
                    data = frame.get('data') or {}
                    operation = data.get('data') or {}
                    if data.get('m') == 'device.op' and operation.get('t') in (3, 7):
                        strength_operations.append(operation)
                self.assertEqual(
                    [(item['s'], item['c'], item['v']) for item in strength_operations],
                    [
                        ('coyote-slot', 0, 60),
                        ('coyote-slot', 1, 40),
                    ],
                )

                snapshot = controller.snapshot()
                self.assertEqual(
                    {(item['slot_id'], item['device_kind']) for item in snapshot['devices']},
                    {('coyote-slot', 'coyote'), ('opossum-slot', 'opossum')},
                )
                self.assertEqual(
                    snapshot['device_channels']['coyote']['A']['device_strengths'][0]['upper_strength'], 70,
                )
                self.assertEqual(
                    snapshot['device_channels']['opossum']['A']['device_strengths'][0]['upper_strength'], 120,
                )

                client = SimpleUDPClient('127.0.0.1', settings['osc']['listen_port'])
                client.send_message('/avatar/parameters/OGB/Pen/V4_Test/PenOthers', [0.5])
                wave_operations = {}
                deadline = asyncio.get_running_loop().time() + 2
                while len(wave_operations) < 2:
                    remaining = deadline - asyncio.get_running_loop().time()
                    self.assertGreater(remaining, 0)
                    frame = json.loads(await asyncio.wait_for(websocket.recv(), remaining))
                    data = frame.get('data') or {}
                    operation = data.get('data') or {}
                    if data.get('m') == 'device.op' and operation.get('t') == 0 and operation.get('c') == 0:
                        wave_operations.setdefault(operation['s'], operation)
                coyote_frame = wave_operations['coyote-slot']['v'][0]
                opossum_frame = wave_operations['opossum-slot']['v'][0]
                self.assertNotEqual(coyote_frame[:8], '0A0A0A0A')
                self.assertEqual(opossum_frame[:8], '0A0A0A0A')
                self.assertNotEqual(coyote_frame, opossum_frame)
                coyote_strengths = tuple(bytes.fromhex(coyote_frame[8:]))
                opossum_strengths = tuple(bytes.fromhex(opossum_frame[8:]))
                deadline = time.monotonic() + 1
                sent_strengths = {'coyote': (), 'opossum': ()}
                while time.monotonic() < deadline:
                    snapshot = controller.snapshot()
                    sent_strengths = {
                        device_kind: tuple(
                            snapshot['device_channels'][device_kind]['A'].get('sent_strengths', ())
                        )
                        for device_kind in ('coyote', 'opossum')
                    }
                    if sent_strengths == {
                        'coyote': coyote_strengths,
                        'opossum': opossum_strengths,
                    }:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(sent_strengths['coyote'], coyote_strengths)
                self.assertEqual(sent_strengths['opossum'], opossum_strengths)
        finally:
            if client is not None:
                client._sock.close()
            controller.stop()
            self.assertEqual(srv.get_ws_connections(), ())

    async def test_wrong_v4_target_id_is_rejected(self):
        settings, basic = self.make_settings()
        controller = ServiceController()
        try:
            controller.start(settings, basic)
            uri = f'ws://127.0.0.1:{settings["ws"]["listen_port"]}/?tid=wrong'
            async with websocket_connect(uri) as websocket:
                with self.assertRaises(ConnectionClosedError) as raised:
                    await websocket.recv()
                self.assertEqual(raised.exception.code, 1008)
        finally:
            controller.stop()

    async def test_legacy_v3_pairing_path_remains_compatible(self):
        settings, basic = self.make_settings()
        controller = ServiceController()
        try:
            controller.start(settings, basic)
            uri = (
                f'ws://127.0.0.1:{settings["ws"]["listen_port"]}/'
                f'{settings["ws"]["master_uuid"]}'
            )
            async with websocket_connect(uri) as websocket:
                bind = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                self.assertEqual(bind['type'], 'bind')
                self.assertEqual(bind['message'], 'targetId')
                await websocket.send(json.dumps({
                    'type': 'bind',
                    'clientId': settings['ws']['master_uuid'],
                    'targetId': bind['clientId'],
                    'message': 'targetId',
                }))
                result = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                self.assertEqual(result['type'], 'bind')
                self.assertEqual(result['message'], '200')
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not controller.snapshot()['connected']:
                    await asyncio.sleep(0.02)
                snapshot = controller.snapshot()
                self.assertTrue(snapshot['connected'])
                self.assertEqual(snapshot['protocol'], 'v3')
        finally:
            controller.stop()


class DesktopApplicationTests(unittest.TestCase):
    def test_copyright_page_lists_all_requested_sources(self):
        self.assertEqual(
            COPYRIGHT_ENTRIES,
            (
                ('DG-LAB', 'https://github.com/dungeonlab-open', '设备协议与官方波形'),
                ('Shocking-VRChat', 'https://github.com/VRChatNext/Shocking-VRChat', '原始项目与代码来源'),
                ('DG-LAB-VRCOSC', 'https://github.com/ccvrc/DG-LAB-VRCOSC', 'Chatbox 发送部分来源'),
                ('OscGoesBrrr / OSC Toys', 'https://osc.toys', 'SPS/OGB 深度算法与参数协议参考'),
            ),
        )
        self.assertEqual(FRONTEND_CONTRIBUTORS, ('WenX1ang', '猫橘Citrus', 'ChatGPT'))

    def test_polished_window_has_room_for_full_labels(self):
        self.assertGreaterEqual(DesktopApplication.WIDTH, 1120)
        self.assertGreaterEqual(DesktopApplication.HEIGHT, 760)

    def test_busy_save_does_not_write_partially_applied_settings(self):
        calls = []

        class BusyApplication:
            action_running = True

            def _message(self, message, error=False):
                calls.append((message, error))

            def _read_form(self):
                raise AssertionError('busy operation must not read or save the form')

        DesktopApplication._save_and_apply(BusyApplication())
        self.assertEqual(calls, [('当前操作尚未完成，请稍候再试。', True)])


class SteamVRAutoStartTests(unittest.TestCase):
    class Backend:
        def __init__(self, installed=True, auto_launch=False, install_on_add=True):
            self.installed = installed
            self.auto_launch = auto_launch
            self.install_on_add = install_on_add
            self.added = []
            self.removed = []
            self.set_values = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def is_installed(self):
            return self.installed

        def add_manifest(self, path):
            self.added.append(Path(path))
            if self.install_on_add:
                self.installed = True

        def remove_manifest(self, path):
            self.removed.append(Path(path))
            self.installed = False

        def set_auto_launch(self, enabled):
            self.set_values.append(enabled)
            self.auto_launch = enabled

        def get_auto_launch(self):
            return self.auto_launch

    def test_manifest_uses_absolute_binary_and_required_overlay_flag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / 'ShockingVRChat.exe'
            manifest_path = write_manifest(temp_dir, executable, '')
            document = json.loads(manifest_path.read_text(encoding='utf-8'))
            application = document['applications'][0]
            self.assertEqual(application['app_key'], APPLICATION_KEY)
            self.assertEqual(application['binary_path_windows'], str(executable.resolve()))
            self.assertTrue(application['is_dashboard_overlay'])

    def test_openvr_utility_mode_handles_running_runtime_without_a_headset(self):
        calls = []
        applications = object()

        class HmdNotFound(Exception):
            pass

        def init(application_type):
            calls.append(application_type)
            if application_type == 3:
                raise HmdNotFound

        fake_openvr = SimpleNamespace(
            VRApplication_Background=3,
            VRApplication_Utility=4,
            error_code=SimpleNamespace(InitError_Init_HmdNotFound=HmdNotFound),
            init=init,
            shutdown=lambda: calls.append('shutdown'),
            VRApplications=lambda: applications,
        )
        with patch.dict(sys.modules, {'openvr': fake_openvr}):
            with OpenVRApplicationsBackend() as backend:
                self.assertIs(backend.applications, applications)
        self.assertEqual(calls, [3, 'shutdown', 4, 'shutdown'])

    def test_enable_registers_manifest_and_verifies_auto_launch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = self.Backend(installed=False)
            result = configure_steamvr_autostart(
                temp_dir,
                True,
                executable=Path(temp_dir) / 'ShockingVRChat.exe',
                backend_factory=lambda: backend,
            )
            self.assertTrue(result.enabled)
            self.assertFalse(result.pending_restart)
            self.assertEqual(len(backend.added), 1)
            self.assertEqual(backend.set_values, [True])
            self.assertTrue(backend.auto_launch)

    def test_first_registration_can_report_required_steamvr_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = self.Backend(installed=False, install_on_add=False)
            result = configure_steamvr_autostart(
                temp_dir,
                True,
                executable=Path(temp_dir) / 'ShockingVRChat.exe',
                backend_factory=lambda: backend,
            )
            self.assertTrue(result.pending_restart)
            self.assertEqual(backend.set_values, [])

    def test_disable_verifies_setting_and_removes_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_manifest(
                temp_dir,
                Path(temp_dir) / 'ShockingVRChat.exe',
                '',
            )
            backend = self.Backend(installed=True, auto_launch=True)
            result = configure_steamvr_autostart(
                temp_dir,
                False,
                backend_factory=lambda: backend,
            )
            self.assertFalse(result.enabled)
            self.assertEqual(backend.set_values, [False])
            self.assertEqual(backend.removed, [manifest_path])
            self.assertFalse(manifest_path.exists())


if __name__ == '__main__':
    unittest.main()
