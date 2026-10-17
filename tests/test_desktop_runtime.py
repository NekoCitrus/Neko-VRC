import asyncio
import copy
import ctypes
from ctypes import wintypes
import json
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.request import Request, urlopen

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
    TRIGGER_SWITCH_FIELDS,
    default_trigger_switches,
)
from srv.connector.coyotev4ws import DGV4Connection
from srv.oscquery import (
    OSCQueryAvatarSnapshot, OSCQueryService, VRChatOSCQueryClient,
    ServiceStateChange, parse_vrchat_avatar_node,
)
from srv.sps_depth import LMS_PROXIMITY_ADDRESS, PCS_PROXIMITY_ADDRESS, SPSZone, SPS_SOCKET, SPS_PLUG
from srv.udp_relay import create_udp_relay
from srv.steamvr_autostart import (
    APPLICATION_KEY,
    OpenVRApplicationsBackend,
    configure_steamvr_autostart,
    write_manifest,
)
from srv.win32_ui import (
    COPYRIGHT_ENTRIES, FRONTEND_CONTRIBUTORS, DesktopApplication,
    user32, gdi32, kernel32,
)


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def free_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ConfigManagerTests(unittest.TestCase):
    def test_new_profiles_default_to_bluetooth_without_selecting_an_address(self):
        with tempfile.TemporaryDirectory() as root:
            settings, _basic = ConfigManager(app_dir=root, config_dir=Path(root) / 'profile').load()
            for connection in settings['connections'].values():
                self.assertEqual(connection['mode'], 'bluetooth')
                self.assertEqual(connection['address'], '')

    def test_saved_app_mode_is_not_overwritten_by_bluetooth_default(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConfigManager(app_dir=root, config_dir=Path(root) / 'profile')
            settings, basic = manager.load()
            for connection in settings['connections'].values():
                connection['mode'] = 'app'
            manager.save(settings, basic)
            settings, _basic = manager.load()
            self.assertTrue(all(connection['mode'] == 'app' for connection in settings['connections'].values()))

    def test_default_config_uses_neko_roaming_without_creating_legacy_directory(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {'APPDATA': root}):
            manager = ConfigManager(app_dir=Path(root) / 'app')
            manager.load()
            self.assertEqual(manager.path, Path(root) / 'Neko-VRC' / 'settings-v2.0.yaml')
            self.assertTrue(manager.path.is_file())
            self.assertFalse((Path(root) / 'ShockingVRChat').exists())

    def test_old_roaming_config_migrates_read_only_and_new_config_takes_priority(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {'APPDATA': root}):
            legacy = ConfigManager(app_dir=Path(root) / 'app', config_dir=Path(root) / 'ShockingVRChat')
            settings, basic = legacy.load()
            basic['dglab3']['opossum']['channel_b']['strength_limit'] = 71
            legacy.save(settings, basic)
            old_bytes = legacy.path.read_bytes()
            manager = ConfigManager(app_dir=Path(root) / 'app')
            migrated_settings, migrated_basic = manager.load()
            self.assertEqual(manager.migrated_from, str(legacy.path))
            self.assertEqual(migrated_settings['ws']['master_uuid'], settings['ws']['master_uuid'])
            self.assertEqual(migrated_basic['dglab3']['opossum']['channel_b']['strength_limit'], 71)
            self.assertEqual(legacy.path.read_bytes(), old_bytes)
            self.assertEqual(list(legacy.config_dir.iterdir()), [legacy.path])
            basic['dglab3']['opossum']['channel_b']['strength_limit'] = 30
            legacy.save(settings, basic)
            reloaded = ConfigManager(app_dir=Path(root) / 'app')
            self.assertEqual(reloaded.load()[1]['dglab3']['opossum']['channel_b']['strength_limit'], 71)
            self.assertIsNone(reloaded.migrated_from)

    def test_explicit_config_override_does_not_import_legacy_roaming(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {'APPDATA': root}):
            old = ConfigManager(app_dir=Path(root) / 'app', config_dir=Path(root) / 'ShockingVRChat')
            settings, basic = old.load()
            basic['dglab3']['coyote']['channel_a']['strength_limit'] = 17
            old.save(settings, basic)
            manager = ConfigManager(app_dir=Path(root) / 'app', config_dir=Path(root) / 'isolated')
            self.assertEqual(manager.load()[1]['dglab3']['coyote']['channel_a']['strength_limit'], 100)
            self.assertIsNone(manager.migrated_from)

    def test_default_config_without_appdata_uses_neko_under_home(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {'APPDATA': ''}), patch('srv.config_manager.Path.home', return_value=Path(root)):
            manager = ConfigManager(app_dir=Path(root) / 'app')
            self.assertEqual(manager.config_dir, Path(root) / 'AppData' / 'Roaming' / 'Neko-VRC')

    def test_new_channel_source_switches_have_requested_device_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConfigManager(app_dir=root, config_dir=root)
            settings, basic = manager.load()
            runtime = apply_basic_settings(settings, basic)
            for kind in ('coyote', 'opossum'):
                for channel in ('channel_a', 'channel_b'):
                    config = basic['dglab3'][kind][channel]
                    self.assertFalse(config['pcs_enabled'])
                    self.assertFalse(config['lms_enabled'])
                    for field, expected in default_trigger_switches(kind).items():
                        self.assertIs(config[field], expected)
                        self.assertIs(runtime['dglab3'][kind][channel][field], expected)
                    self.assertTrue(config['extra_parameters']['enabled'])

    def test_fallback_defaults_missing_fields_and_explicit_choices_all_channels(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConfigManager(app_dir=root, config_dir=root)
            settings, basic = manager.load()
            for kind in ('coyote', 'opossum'):
                for name in ('channel_a', 'channel_b'):
                    basic['dglab3'][kind][name].update(pcs_enabled=True, lms_enabled=True)
            manager.save(settings, basic)
            saved_settings, saved_basic = manager.load()
            for kind in ('coyote', 'opossum'):
                for name in ('channel_a', 'channel_b'):
                    config = saved_basic['dglab3'][kind][name]
                    self.assertTrue(config['pcs_enabled'])
                    self.assertTrue(config['lms_enabled'])
                    del config['pcs_enabled'], config['lms_enabled']
            runtime = apply_basic_settings(saved_settings, saved_basic)
            for kind in ('coyote', 'opossum'):
                for name in ('channel_a', 'channel_b'):
                    self.assertFalse(runtime['dglab3'][kind][name]['pcs_enabled'])
                    self.assertFalse(runtime['dglab3'][kind][name]['lms_enabled'])

    def test_source_switches_survive_save_load_and_old_extra_switch_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConfigManager(app_dir=root, config_dir=root)
            settings, basic = manager.load()
            config = basic['dglab3']['coyote']['channel_a']
            config.update(socket_enabled=False, plug_enabled=False, pcs_enabled=False, lms_enabled=True)
            config['extra_parameters']['enabled'] = False
            manager.save(settings, basic)
            _settings, restored = manager.load()
            self.assertEqual(restored['dglab3']['coyote']['channel_a'], config)
            document = yaml.safe_load(manager.path.read_text(encoding='utf-8'))
            for kind in ('coyote', 'opossum'):
                for channel in ('channel_a', 'channel_b'):
                    for field in TRIGGER_SWITCH_FIELDS:
                        document['channels']['dglab3'][kind][channel].pop(field)
            manager.path.write_text(yaml.safe_dump(document), encoding='utf-8')
            _settings, migrated = manager.load()
            old = migrated['dglab3']['coyote']['channel_a']
            self.assertFalse(old['extra_parameters']['enabled'])
            for field, expected in default_trigger_switches('coyote').items():
                self.assertIs(old[field], expected)

    def test_source_switch_config_rejects_non_boolean_values(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        for field in TRIGGER_SWITCH_FIELDS:
            for invalid in ('false', 0, None):
                basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
                basic['dglab3']['opossum']['channel_b'][field] = invalid
                with self.subTest(field=field, value=invalid), self.assertRaises(ValueError):
                    validate_config(settings, basic)

    def test_first_run_creates_unified_appdata_config(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConfigManager(app_dir=Path(root) / 'app', config_dir=Path(root) / 'config')
            settings, basic = manager.load()
            self.assertTrue(manager.path.exists())
            self.assertEqual(manager.path.name, 'settings-v2.0.yaml')
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
    async def test_chatbox_uses_connected_app_and_bluetooth_routes(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['connections']['coyote']['mode'] = 'app'
        settings['connections']['opossum']['mode'] = 'bluetooth'
        runtime = DeviceRuntime(settings)
        runtime.chatbox_manager = SimpleNamespace(update_chatbox=AsyncMock())
        coyote = {'slot_id': 'app-coyote', 'device_kind': 'coyote', 'upper_strength': {'A': 80, 'B': 70}}
        ignored = {'slot_id': 'app-opossum', 'device_kind': 'opossum', 'upper_strength': {'A': 200, 'B': 200}}
        opossum = {'slot_id': 'ble-opossum', 'device_kind': 'opossum', 'upper_strength': {'A': 60, 'B': 50}}
        runtime.router.hosts['opossum'] = SimpleNamespace(connected=True, snapshot=lambda: opossum)
        app = SimpleNamespace(uuid='app', get_device_states=lambda: [coyote, ignored])
        with patch('srv.get_ws_connections', return_value=[app]), patch(
            'shocking_vrchat.asyncio.sleep', side_effect=asyncio.CancelledError,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await runtime._chatbox_task()
        runtime.chatbox_manager.update_chatbox.assert_awaited_once_with(
            (), [], device_states=({**coyote, 'transport': 'app', 'connection_uuid': 'app'}, opossum),
        )
        runtime.router.hosts['opossum'].connected = False
        with patch('srv.get_ws_connections', return_value=[]):
            self.assertEqual(runtime.router.device_states(), ())

    async def test_hot_save_preserves_display_for_later_disconnect_clear(self):
        settings = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS))
        settings['chatbox'].update(enable=True, set_avatar_parameter=False)
        old_client, new_client = Mock(), Mock()
        with patch('srv.advanced_chatbox_manager.SimpleUDPClient', side_effect=[old_client, new_client]):
            runtime = DeviceRuntime(settings)
            runtime.dispatcher = runtime._build_dispatcher()
            runtime.chatbox_manager.last_status_text = '郊狼 MAX A:80 B:70'
            await runtime._apply_settings(copy.deepcopy(settings))
        old_client.send_message.assert_not_called()
        old_client._sock.close.assert_not_called()
        await runtime.chatbox_manager.update_chatbox([])
        old_client.send_message.assert_called_once_with('/chatbox/input', ['', True, False])
        new_client.send_message.assert_not_called()
        runtime.chatbox_manager.cleanup(notify=False)

    async def test_disabling_chatbox_clears_previous_display(self):
        settings = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS))
        settings['chatbox'].update(enable=True, set_avatar_parameter=False)
        client = Mock()
        with patch('srv.advanced_chatbox_manager.SimpleUDPClient', return_value=client):
            runtime = DeviceRuntime(settings)
            runtime.dispatcher = runtime._build_dispatcher()
            runtime.chatbox_manager.last_status_text = '郊狼 MAX A:80 B:70'
            updated = copy.deepcopy(settings)
            updated['chatbox']['enable'] = False
            await runtime._apply_settings(updated)
        client.send_message.assert_called_once_with('/chatbox/input', ['', True, False])
        client._sock.close.assert_called_once()
        self.assertFalse(runtime.chatbox_manager.enabled)

    async def test_changing_chatbox_endpoint_clears_old_destination(self):
        settings = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS))
        settings['chatbox'].update(enable=True, set_avatar_parameter=False)
        old_client, new_client = Mock(), Mock()
        with patch('srv.advanced_chatbox_manager.SimpleUDPClient', side_effect=[old_client, new_client]):
            runtime = DeviceRuntime(settings)
            runtime.dispatcher = runtime._build_dispatcher()
            runtime.chatbox_manager.last_status_text = '郊狼 MAX A:80 B:70'
            updated = copy.deepcopy(settings)
            updated['chatbox']['osc_port'] = settings['chatbox']['osc_port'] + 1
            await runtime._apply_settings(updated)
        old_client.send_message.assert_called_once_with('/chatbox/input', ['', True, False])
        self.assertIsNone(runtime.chatbox_manager.last_status_text)
        runtime.chatbox_manager.cleanup(notify=False)

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
    def test_live_inventory_counts_only_readable_complete_depth_and_exposes_fallbacks(self):
        events = []
        runtime = DeviceRuntime(apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS)), event_callback=events.append)
        runtime.dispatcher = runtime._build_dispatcher()
        self.addCleanup(runtime.chatbox_manager.cleanup, notify=False)
        paths = (
            '/avatar/parameters/OGB/Orf/Touch/TouchSelf',
            '/avatar/parameters/OGB/Orf/Half/PenSelfNewRoot',
            '/avatar/parameters/OGB/Orf/Complete/PenSelfNewRoot',
            '/avatar/parameters/OGB/Orf/Complete/PenSelfNewTip',
            '/avatar/parameters/OGB/Orf/WriteOnly/PenOthers',
            '/avatar/parameters/OGB/Pen/Frot/FrotSelf',
            '/avatar/parameters/OGB/Pen/Good/PenOthers',
            PCS_PROXIMITY_ADDRESS, LMS_PROXIMITY_ADDRESS,
        )
        readable = tuple(path for path in paths if '/WriteOnly/' not in path)
        async def apply():
            try:
                with patch('shocking_vrchat.find_avatar_sps_config', return_value=None):
                    runtime._apply_avatar_snapshot(OSCQueryAvatarSnapshot('', paths, 'http://127.0.0.1:19000', readable))
                await asyncio.sleep(0)
                event = next(event for event in events if event['type'] == 'avatar')
                self.assertEqual([zone['zone_id'] for zone in event['sockets']], ['Complete'])
                self.assertEqual([zone['zone_id'] for zone in event['plugs']], ['Good'])
                self.assertEqual(event['parameter_availability'], {'known': True, 'socket': True, 'plug': True, 'pcs': True, 'lms': True})
            finally:
                for handler in runtime.handlers:
                    await handler.stop_background_jobs()
        asyncio.run(apply())

    def test_query_access_and_scalar_values_are_validated(self):
        path = '/avatar/parameters/OGB/Pen/Locked/PenOthers'
        for access in (None, 0, 1, 2, 3, -1, 5, True, '1'):
            with self.subTest(access=access):
                document = self.avatar_document()
                leaf = document['CONTENTS']['parameters']['CONTENTS']['Depth']
                if access is not None:
                    leaf['ACCESS'] = access
                snapshot = parse_vrchat_avatar_node(document, observed_at=10)
                readable = access is None or (type(access) is int and access in (1, 3))
                self.assertEqual(snapshot.parameter_paths, (path,))
                self.assertEqual(snapshot.numeric_parameter_paths, (path,) if readable else ())
                self.assertEqual(snapshot.parameter_values, ((path, 1.0),) if readable else ())
                self.assertEqual(snapshot.observed_at, 10)
        for value, expected in (([0], 0), ([True], 1), ([-2], 0), ([2], 1),
                                (['1'], None), ([float('nan')], None),
                                ([float('inf')], None), ([0, 1], None), ([], None)):
            with self.subTest(value=value):
                document = self.avatar_document()
                document['CONTENTS']['parameters']['CONTENTS']['Depth']['VALUE'] = value
                snapshot = parse_vrchat_avatar_node(document)
                self.assertEqual(snapshot.parameter_values, () if expected is None else ((path, expected),))

    def test_fresh_values_are_published_without_repeating_the_same_response(self):
        published = []
        client = VRChatOSCQueryClient(None, published.append)
        first = parse_vrchat_avatar_node(self.avatar_document(), observed_at=10)
        client._publish(first)
        client._publish(first)
        client._publish(parse_vrchat_avatar_node(self.avatar_document(), observed_at=11))
        self.assertEqual(len(published), 2)

    def test_write_only_socket_does_not_block_readable_pcs_on_locked_avatar(self):
        settings = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS))
        for kind in ('coyote', 'opossum'):
            for channel in ('channel_a', 'channel_b'):
                settings['dglab3'][kind][channel]['pcs_enabled'] = True
        runtime = DeviceRuntime(settings)
        runtime.dispatcher = runtime._build_dispatcher()
        self.addCleanup(runtime.chatbox_manager.cleanup, notify=False)
        document = self.avatar_document()
        document['CONTENTS']['parameters']['CONTENTS'] = {
            'Root': {'FULL_PATH': '/avatar/parameters/OGB/Orf/Locked/PenSelfNewRoot',
                     'TYPE': 'f', 'ACCESS': 2, 'VALUE': [1]},
            'Tip': {'FULL_PATH': '/avatar/parameters/OGB/Orf/Locked/PenSelfNewTip',
                    'TYPE': 'f', 'ACCESS': 2, 'VALUE': [1]},
            'PCS': {'FULL_PATH': PCS_PROXIMITY_ADDRESS, 'TYPE': 'f', 'ACCESS': 1, 'VALUE': [0.8]},
        }
        snapshot = parse_vrchat_avatar_node(document, 'http://127.0.0.1:19000')
        async def apply():
            try:
                runtime._apply_avatar_snapshot(snapshot)
                await asyncio.sleep(0)
                for handler in runtime.handlers:
                    self.assertFalse(handler.socket_depth_available)
                    self.assertEqual(handler.current_strength_percentage, 0.8)
                    self.assertIn('PCS 回退已启用', handler.parameter_hint)
                    self.assertFalse(handler.parameter_availability['socket'])
            finally:
                for handler in runtime.handlers:
                    await handler.stop_background_jobs()
        asyncio.run(apply())

    def test_query_tree_does_not_announce_individually_disabled_inputs(self):
        service = self.make_service()
        for kind in ('coyote', 'opossum'):
            for channel in ('channel_a', 'channel_b'):
                config = service.settings['dglab3'][kind][channel]
                config.update(socket_enabled=False, plug_enabled=False, pcs_enabled=False, lms_enabled=False)
                config['extra_parameters']['enabled'] = False
        self.assertEqual(service._paths(), [])
        self.assertIsNone(service.node_for(PCS_PROXIMITY_ADDRESS))
        self.assertIsNone(service.node_for(LMS_PROXIMITY_ADDRESS))
        # Only observe Plug for the LMS attachment guard; no Socket/PCS.
        service.settings['dglab3']['coyote']['channel_a']['lms_enabled'] = True
        paths = service._paths()
        self.assertIn(LMS_PROXIMITY_ADDRESS, paths)
        self.assertTrue(any('/OGB/Pen/' in path for path in paths))
        self.assertFalse(any('/OGB/Orf/' in path for path in paths))

    def test_query_keeps_paths_but_excludes_non_numeric_socket_signals(self):
        document = self.avatar_document(['avtr_current'])
        base = '/avatar/parameters/OGB/Orf/One/'
        document['CONTENTS']['parameters']['CONTENTS'] = {
            'Root': {'FULL_PATH': base + 'PenSelfNewRoot', 'TYPE': 'f'},
            'Tip': {'FULL_PATH': base + 'PenSelfNewTip', 'TYPE': 's'},
        }
        snapshot = parse_vrchat_avatar_node(document, 'http://127.0.0.1:19000')
        self.assertEqual(len(snapshot.parameter_paths), 2)
        self.assertEqual(snapshot.numeric_parameter_paths, (base + 'PenSelfNewRoot',))
        published = []
        client = VRChatOSCQueryClient(None, published.append)
        client._publish(snapshot)
        document['CONTENTS']['parameters']['CONTENTS']['Tip']['TYPE'] = 'f'
        client._publish(parse_vrchat_avatar_node(document, snapshot.endpoint))
        self.assertEqual(len(published), 2)

    def test_repeated_avatar_change_republishes_unchanged_inventory(self):
        published = []
        client = VRChatOSCQueryClient(None, published.append)
        snapshot = OSCQueryAvatarSnapshot('', (), 'http://127.0.0.1:19000')
        client._publish(snapshot)
        client._publish(snapshot)
        self.assertEqual(len(published), 1)
        client.request_refresh('')
        client._publish(snapshot)
        self.assertEqual(len(published), 2)

    def test_receiver_announces_pcs_without_the_extra_switch(self):
        service = self.make_service()
        service.settings['dglab3']['coyote']['channel_a']['pcs_enabled'] = True
        for kind in ('coyote', 'opossum'):
            for channel in ('channel_a', 'channel_b'):
                service.settings['dglab3'][kind][channel]['extra_parameters']['enabled'] = False
        self.assertEqual(service.node_for(PCS_PROXIMITY_ADDRESS), {
            'FULL_PATH': PCS_PROXIMITY_ADDRESS, 'TYPE': 'f', 'ACCESS': 2,
        })

    def test_lms_fallback_is_announced_when_extra_parameters_are_disabled(self):
        service = self.make_service()
        service.settings['dglab3']['opossum']['channel_a']['lms_enabled'] = True
        for kind in ('coyote', 'opossum'):
            for channel in ('channel_a', 'channel_b'):
                service.settings['dglab3'][kind][channel]['extra_parameters']['enabled'] = False
        self.assertEqual(service.node_for(LMS_PROXIMITY_ADDRESS), {
            'FULL_PATH': LMS_PROXIMITY_ADDRESS, 'TYPE': 'f', 'ACCESS': 2,
        })

    def avatar_document(self, avatar_value=None):
        return {
            'FULL_PATH': '/avatar',
            'CONTENTS': {
                'change': {'TYPE': 's', 'VALUE': avatar_value},
                'parameters': {
                    'FULL_PATH': '/avatar/parameters',
                    'CONTENTS': {
                        'Depth': {
                            'FULL_PATH': '/avatar/parameters/OGB/Pen/Locked/PenOthers',
                            'TYPE': 'f', 'VALUE': [1.0],
                        },
                    },
                },
            },
        }

    def test_unknown_avatar_id_does_not_discard_live_parameters(self):
        for value in (None, [], [None], ['Unknown'], [''], [False]):
            with self.subTest(value=value):
                snapshot = parse_vrchat_avatar_node(self.avatar_document(value))
                self.assertIsNotNone(snapshot)
                self.assertEqual(snapshot.avatar_id, '')
                self.assertEqual(snapshot.parameter_paths, (
                    '/avatar/parameters/OGB/Pen/Locked/PenOthers',
                ))
        document = self.avatar_document()
        del document['CONTENTS']['change']
        self.assertIsNotNone(parse_vrchat_avatar_node(document))

    def test_invalid_parameter_tree_is_not_an_avatar(self):
        for parameters in (None, [], 'invalid'):
            document = self.avatar_document()
            document['CONTENTS']['parameters'] = parameters
            self.assertIsNone(parse_vrchat_avatar_node(document))

    def test_receiver_explicitly_announces_avatar_change(self):
        service = self.make_service()
        self.assertEqual(service.node_for('/avatar/change'), {
            'FULL_PATH': '/avatar/change', 'TYPE': 's', 'ACCESS': 2,
        })
        self.assertEqual(service.node_for('/avatar')['ACCESS'], 2)

    def test_discovery_does_not_treat_other_receivers_as_vrchat(self):
        zeroconf = Mock()
        zeroconf.get_service_info.return_value = SimpleNamespace(
            port=19000, parsed_addresses=lambda: ['127.0.0.1'],
        )
        client = VRChatOSCQueryClient(zeroconf, lambda snapshot: None)
        client.service_names = [
            'Neko-VRC._oscjson._tcp.local.',
            'VRCFT-other._oscjson._tcp.local.',
            'VRChat-Client-test._oscjson._tcp.local.',
        ]
        expected = OSCQueryAvatarSnapshot('', ('/avatar/parameters/Test',), 'http://127.0.0.1:19000')
        with patch.object(client, '_fetch', return_value=expected) as fetch:
            self.assertEqual(client._query_services(), expected)
        self.assertEqual(zeroconf.get_service_info.call_count, 1)
        fetch.assert_called_once_with('127.0.0.1', 19000)

    def test_endpoint_change_publishes_an_otherwise_identical_avatar(self):
        published = []
        client = VRChatOSCQueryClient(None, published.append)
        for endpoint in ('http://127.0.0.1:10001', 'http://127.0.0.1:10002'):
            client._publish(OSCQueryAvatarSnapshot('', (), endpoint))
        self.assertEqual(len(published), 2)

    def test_anonymous_live_avatar_seeds_fresh_values_without_rebuilding_metadata(self):
        events = []
        runtime = DeviceRuntime(apply_basic_settings(
            copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS),
        ), event_callback=events.append)
        runtime.dispatcher = runtime._build_dispatcher()
        self.addCleanup(runtime.chatbox_manager.cleanup, notify=False)
        snapshot = parse_vrchat_avatar_node(self.avatar_document(), 'http://127.0.0.1:10001')
        async def apply():
            runtime._apply_avatar_snapshot(snapshot)
            await asyncio.sleep(0)
            self.assertTrue(all(handler.get_mode_info()['is_active'] for handler in runtime.handlers))
            runtime._apply_avatar_snapshot(snapshot)
            await asyncio.sleep(0)
            self.assertEqual(sum(event['type'] == 'avatar' for event in events), 1)
            runtime.router.paused = True
            paused = parse_vrchat_avatar_node(self.avatar_document(), snapshot.endpoint)
            with patch.object(runtime.handlers[0], 'receive_query_value') as receive:
                runtime._apply_avatar_snapshot(paused)
                receive.assert_not_called()
            runtime._avatar_change_handler('/avatar/change', '')
            runtime._apply_avatar_snapshot(snapshot)
            await asyncio.sleep(0)
            self.assertFalse(any(handler.is_active for handler in runtime.handlers))
            for handler in runtime.handlers:
                await handler.stop_background_jobs()
        asyncio.run(apply())
        event = next(event for event in events if event['type'] == 'avatar')
        self.assertEqual(event['avatar_name'], '当前 Avatar')
        self.assertEqual(event['plugs'], [{'zone_id': 'Locked', 'label': 'Locked'}])

    def test_avatar_change_resets_anonymous_avatar_before_query_finishes(self):
        runtime = DeviceRuntime(apply_basic_settings(
            copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS),
        ))
        runtime._build_dispatcher()
        self.addCleanup(runtime.chatbox_manager.cleanup, notify=False)
        runtime.oscquery = Mock()
        async def change():
            runtime._avatar_change_handler('/avatar/change', '')
            runtime._avatar_change_handler('/avatar/change', '')
            await asyncio.sleep(0)
            for handler in runtime.handlers:
                await handler.stop_background_jobs()
        with patch.object(runtime.handlers[0], 'reset_for_avatar_change') as reset:
            asyncio.run(change())
        self.assertEqual(reset.call_count, 2)
        self.assertEqual(runtime.oscquery.request_avatar_refresh.call_count, 2)

    def test_http_negotiation_and_both_mdns_advertisements(self):
        service = self.make_service()
        service.http_port = 0
        zeroconf = Mock()
        with patch('srv.oscquery.Zeroconf', return_value=zeroconf):
            try:
                service.start()
                service.register_thread.join(timeout=2)
                infos = [call.args[0] for call in zeroconf.register_service.call_args_list]
                self.assertEqual({info.type for info in infos}, {
                    '_oscjson._tcp.local.', '_osc._udp.local.',
                })
                self.assertEqual(infos[0].name.split('._osc')[0], infos[1].name.split('._osc')[0])
                self.assertEqual(service.service_info.port, service.http_port)
                self.assertEqual(service.osc_service_info.port, service.osc_port)
                service.vrchat_client = Mock()
                endpoint = f'http://127.0.0.1:{service.http_port}'
                with urlopen(f'{endpoint}/?HOST_INFO', timeout=2) as response:
                    host_info = json.load(response)
                self.assertEqual(host_info['OSC_IP'], '127.0.0.1')
                self.assertEqual(host_info['OSC_PORT'], service.osc_port)
                self.assertEqual(host_info['EXTENSIONS'], {'ACCESS': True, 'TYPE': True})
                with urlopen(f'{endpoint}/avatar/change', timeout=2) as response:
                    self.assertEqual(json.load(response)['TYPE'], 's')
                self.assertTrue(service.vrchat_client.request_refresh.called)
            finally:
                service.stop()
        self.assertEqual(zeroconf.unregister_service.call_count, 2)

    def test_live_http_tree_refreshes_after_unlock_without_avatar_change(self):
        # Query the live server, never a stale avatar JSON. Unlocking may add
        # parameters without changing the avatar ID or sending /avatar/change.
        server = self.make_service()
        server.http_port = 0
        document = self.avatar_document([])
        document['CONTENTS']['parameters']['CONTENTS'].clear()
        server.node_for = Mock(return_value=document)
        published = []
        client = VRChatOSCQueryClient(None, published.append)
        with patch('srv.oscquery.Zeroconf', return_value=Mock()):
            try:
                server.start()
                snapshot = client._fetch('127.0.0.1', server.http_port)
                client._publish(snapshot)
                self.assertEqual(snapshot.parameter_paths, ())
                document['CONTENTS']['parameters'] = self.avatar_document()['CONTENTS']['parameters']
                snapshot = client._fetch('127.0.0.1', server.http_port)
                client._publish(snapshot)
                self.assertEqual(len(published), 2)
                self.assertEqual(snapshot.avatar_id, '')
                self.assertEqual(snapshot.parameter_paths, (
                    '/avatar/parameters/OGB/Pen/Locked/PenOthers',
                ))
            finally:
                server.stop()

    def test_mdns_callback_accepts_zeroconf_keyword_arguments(self):
        client = VRChatOSCQueryClient(None, lambda snapshot: None)
        for change, expected in ((ServiceStateChange.Added, {'VRChat'}),
                                 (ServiceStateChange.Removed, set())):
            client.refresh_event.clear()
            client._service_changed(
                zeroconf=None, service_type='_oscjson._tcp.local.',
                name='VRChat', state_change=change,
            )
            self.assertEqual(client.service_names, expected)
            self.assertTrue(client.refresh_event.is_set())

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
    async def test_request_ack_latency_is_exposed_per_app_connection(self):
        connection = DGV4Connection(SimpleNamespace(send=AsyncMock(), latency=0.0125), settings=apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS)))
        self.addCleanup(srv.remove_ws_connection, connection)
        connection._replace_devices([
            {'slotId': 'opossum', 'type': 'OVC_1', 'slotState': {'hasDevice': True}},
        ])
        self.assertEqual(connection.get_device_states()[0]['communication_latency'], {'kind': 'app_ping', 'milliseconds': 12.5})
        await connection._send_request('device.op', {'s': 'opossum'})
        request_id = next(iter(connection._pending_requests))
        connection._handle_data({'t': 'resp', 'reqId': request_id, 'result': {}})
        sample = connection.get_device_states()[0]['communication_latency']
        self.assertEqual(sample['kind'], 'app_ack')
        self.assertGreaterEqual(sample['milliseconds'], 0)
        connection._last_request_ack = (5, time.perf_counter() - 31)
        self.assertEqual(connection.get_device_states()[0]['communication_latency']['kind'], 'app_ping')

    async def test_hot_caps_raise_coyote_from_zero_without_arming_idle_opossum(self):
        connection = self.make_connection()
        connection._replace_devices([
            {'slotId': 'coyote', 'type': 'COYOTE_030', 'props': {'intensityA': 0},
             'slotState': {'hasDevice': True, 'channelA': {'intensityMax': 50}}},
            {'slotId': 'opossum', 'type': 'OVC_1', 'props': {'intensityA': 100}, 'slotState': {'hasDevice': True}},
        ])
        connection.strength_limits['coyote']['A'] = 0
        connection.set_strength = AsyncMock()
        connection._send_request = AsyncMock(return_value=True)
        updated = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS))
        updated['dglab3']['coyote']['channel_a']['strength_limit'] = 70
        updated['dglab3']['opossum']['channel_a']['strength_limit'] = 60
        await connection.apply_settings(updated)
        self.assertTrue(any(call.kwargs.get('slot_id') == 'coyote' and call.args == ('A',) and call.kwargs['value'] == 50 for call in connection.set_strength.await_args_list))
        self.assertFalse(any(call.kwargs.get('slot_id') == 'opossum' for call in connection.set_strength.await_args_list))
        await connection.send_wave('A', '["0A0A0A0A64646464"]', device_kind='opossum')
        connection.set_strength.reset_mock()
        updated['dglab3']['opossum']['channel_a']['strength_limit'] = 75
        await connection.apply_settings(updated)
        connection.set_strength.assert_awaited_once_with('A', value=75, slot_id='opossum')
        await connection.clear_wave('A', 'opossum')
        connection.set_strength.reset_mock()
        updated['dglab3']['opossum']['channel_a']['strength_limit'] = 85
        await connection.apply_settings(updated)
        connection.set_strength.assert_not_awaited()

    def make_connection(self):
        connection = object.__new__(DGV4Connection)
        connection.devices = {}
        connection.device_states = {}
        connection._attached_at = time.perf_counter()
        connection._active_outputs = set()
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
    def setUp(self):
        # Keep real loopback UDP/HTTP, but do not advertise test receivers to
        # a running game or let its live avatar replace the synthetic fixtures.
        for target in ('srv.oscquery.Zeroconf', 'srv.oscquery.ServiceBrowser'):
            isolated = patch(target)
            isolated.start()
            self.addCleanup(isolated.stop)

    def test_real_udp_source_checkboxes_hot_update_without_restarting_runtime(self):
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        for channel in ('channel_a', 'channel_b'):
            basic['dglab3']['coyote'][channel]['pcs_enabled'] = True
            basic['dglab3']['opossum'][channel]['lms_enabled'] = True
        disabled = basic['dglab3']['opossum']['channel_a']
        disabled.update(socket_enabled=False, plug_enabled=False, pcs_enabled=False, lms_enabled=False)
        disabled['extra_parameters']['enabled'] = False
        settings = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), basic)
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['oscquery']['listen_port'] = 0
        settings['chatbox']['enable'] = False
        runtime = DeviceRuntime(settings)
        client = None

        def assert_depths(expected):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                values = [handler.current_strength_percentage for handler in runtime.handlers]
                if all(abs(value - target) < 0.00001 for value, target in zip(values, expected)):
                    break
                time.sleep(0.02)
            self.assertEqual(len(values), 4)
            for value, target in zip(values, expected):
                self.assertAlmostEqual(value, target, places=5)

        try:
            runtime.start()
            original_query = runtime.oscquery
            original_thread = runtime.thread
            original_handlers = tuple(runtime.handlers)
            client = SimpleUDPClient('127.0.0.1', runtime.oscquery.osc_port)
            client.send_message(PCS_PROXIMITY_ADDRESS, 0.9)
            client.send_message(LMS_PROXIMITY_ADDRESS, 0.7)
            assert_depths([0.9, 0.9, 0, 0.7])
            updated = copy.deepcopy(settings)
            updated['dglab3']['coyote']['channel_a'].update(pcs_enabled=False, lms_enabled=True)
            updated['dglab3']['opossum']['channel_a']['pcs_enabled'] = True
            runtime.apply_settings(updated)
            client.send_message(PCS_PROXIMITY_ADDRESS, 0.9)
            client.send_message(LMS_PROXIMITY_ADDRESS, 0.7)
            assert_depths([0.7, 0.9, 0.9, 0.7])
            for kind in ('coyote', 'opossum'):
                for channel in ('channel_a', 'channel_b'):
                    config = updated['dglab3'][kind][channel]
                    config.update(socket_enabled=False, plug_enabled=False, pcs_enabled=False, lms_enabled=False)
                    config['extra_parameters']['enabled'] = False
            runtime.apply_settings(updated)
            client.send_message(PCS_PROXIMITY_ADDRESS, 1)
            client.send_message(LMS_PROXIMITY_ADDRESS, 1)
            client.send_message('/avatar/parameters/Shock/TouchAreaA', 1)
            assert_depths([0, 0, 0, 0])
            self.assertIs(runtime.oscquery, original_query)
            self.assertIs(runtime.thread, original_thread)
            self.assertEqual(tuple(runtime.handlers), original_handlers)
        finally:
            if client is not None:
                client._sock.close()
            runtime.stop()

    def test_real_udp_pcs_competes_with_live_socket_per_channel(self):
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        for kind in ('coyote', 'opossum'):
            basic['dglab3'][kind]['channel_a']['socket_zone'] = 'One'
            basic['dglab3'][kind]['channel_b']['socket_zone'] = 'Missing'
            for channel in ('channel_a', 'channel_b'):
                basic['dglab3'][kind][channel]['pcs_enabled'] = True
                basic['dglab3'][kind][channel]['extra_parameters']['enabled'] = False
        settings = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), basic)
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['oscquery']['listen_port'] = 0
        settings['chatbox']['enable'] = False
        runtime = DeviceRuntime(settings)
        client = None
        base = '/avatar/parameters/OGB/Orf/One/'
        document = {
            'CONTENTS': {'parameters': {'CONTENTS': {
                'Root': {'FULL_PATH': base + 'PenSelfNewRoot', 'TYPE': 'f', 'VALUE': [0]},
                'Tip': {'FULL_PATH': base + 'PenSelfNewTip', 'TYPE': 's', 'VALUE': ['invalid']},
            }}},
        }

        def apply_inventory():
            snapshot = parse_vrchat_avatar_node(document, 'http://127.0.0.1:19000')
            async def apply():
                runtime._apply_avatar_snapshot(snapshot)
                await asyncio.sleep(0)
            asyncio.run_coroutine_threadsafe(apply(), runtime.loop).result(timeout=2)

        def assert_depths(a, b):
            expected = [a, b, a, b]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                values = [handler.current_strength_percentage for handler in runtime.handlers]
                if all(abs(value - target) < 0.00001 for value, target in zip(values, expected)):
                    break
                time.sleep(0.02)
            self.assertEqual(len(values), 4)
            for value, target in zip(values, expected):
                self.assertAlmostEqual(value, target, places=5)

        try:
            runtime.start()
            client = SimpleUDPClient('127.0.0.1', runtime.oscquery.osc_port)
            apply_inventory()
            client.send_message(PCS_PROXIMITY_ADDRESS, 0.6)
            assert_depths(0.6, 0.6)
            document['CONTENTS']['parameters']['CONTENTS']['Tip']['TYPE'] = 'f'
            document['CONTENTS']['parameters']['CONTENTS']['Tip']['VALUE'] = [0]
            apply_inventory()
            assert_depths(0, 0)  # Fresh zero-valued Root/Tip are valid evidence.
            client.send_message(PCS_PROXIMITY_ADDRESS, 0.9)
            assert_depths(0.9, 0.9)  # A's valid socket must not suppress PCS.
            for _ in range(4):
                client.send_message(base + 'PenSelfNewRoot', 0.2)
                client.send_message(base + 'PenSelfNewTip', 0.6)
            client.send_message(base + 'PenSelfNewRoot', 0.8)
            client.send_message(base + 'PenSelfNewTip', 1)
            client.send_message(PCS_PROXIMITY_ADDRESS, 0.9)
            assert_depths(0.9, 0.9)
            client.send_message(PCS_PROXIMITY_ADDRESS, 0.2)
            assert_depths(0.5, 0.2)  # Socket wins only where its depth is higher.
            document['CONTENTS']['parameters']['CONTENTS'].clear()
            apply_inventory()
            assert_depths(0, 0)
            client.send_message(PCS_PROXIMITY_ADDRESS, 0.7)
            assert_depths(0.7, 0.7)
        finally:
            if client is not None:
                client._sock.close()
            runtime.stop()

    def test_real_udp_lms_gate_is_opossum_only_and_channel_specific(self):
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        for channel in ('channel_a', 'channel_b'):
            basic['dglab3']['opossum'][channel]['lms_enabled'] = True
        for channel in ('channel_a', 'channel_b'):
            basic['dglab3']['coyote'][channel].update({'plug_zone': 'Never', 'socket_zone': 'Never'})
        basic['dglab3']['opossum']['channel_a']['plug_zone'] = 'First'
        basic['dglab3']['opossum']['channel_b']['plug_zone'] = 'Second'
        # Explicit and wildcard extra bindings must not bypass the LMS gate.
        basic['dglab3']['opossum']['channel_a']['extra_parameters'].update({
            'top': 0.5, 'paths': [LMS_PROXIMITY_ADDRESS],
        })
        basic['dglab3']['opossum']['channel_b']['extra_parameters']['paths'] = [
            '/avatar/parameters/lms/contact/*',
        ]
        settings = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), basic)
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['oscquery']['listen_port'] = 0
        settings['chatbox']['enable'] = False
        runtime = DeviceRuntime(settings)
        client = None

        def assert_depths(a, b):
            expected = [0, 0, a, b]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                infos = [handler.get_mode_info() for handler in runtime.handlers]
                if all(abs(info['strength_percentage'] - value) < 0.00001
                       for info, value in zip(infos, expected)):
                    break
                time.sleep(0.02)
            self.assertEqual(len(infos), 4)
            for info, value in zip(infos, expected):
                self.assertAlmostEqual(info['strength_percentage'], value, places=5)

        try:
            runtime.start()
            client = SimpleUDPClient('127.0.0.1', runtime.oscquery.osc_port)
            first = '/avatar/parameters/OGB/Pen/First/PenSelf'
            second = '/avatar/parameters/OGB/Pen/Second/PenOthers'
            client.send_message(LMS_PROXIMITY_ADDRESS, 0.4)
            assert_depths(0.8, 0.4)
            client.send_message(first, 0.2)
            client.send_message(LMS_PROXIMITY_ADDRESS, 0.4)
            assert_depths(0.2, 0.4)
            client.send_message(second, 0.3)
            client.send_message(first, 0)
            client.send_message(LMS_PROXIMITY_ADDRESS, 0.4)
            assert_depths(0.8, 0.3)
            client.send_message(second, 0)
            client.send_message(LMS_PROXIMITY_ADDRESS, 0.4)
            assert_depths(0.8, 0.4)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and any(handler.is_active for handler in runtime.handlers):
                time.sleep(0.02)
            assert_depths(0, 0)
        finally:
            if client is not None:
                client._sock.close()
            runtime.stop()

    def test_real_udp_self_interaction_reaches_all_four_channels(self):
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        for kind in ('coyote', 'opossum'):
            for channel, zone in (('channel_a', '*'), ('channel_b', 'Own')):
                basic['dglab3'][kind][channel].update({
                    'socket_zone': zone, 'plug_zone': zone,
                })
        settings = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), basic)
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['oscquery']['listen_port'] = 0
        settings['chatbox']['enable'] = False
        runtime = DeviceRuntime(settings)
        client = None

        def assert_depth(expected):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                depths = [handler.get_mode_info()['strength_percentage']
                          for handler in runtime.handlers]
                if all(abs(depth - expected) < 0.00001 for depth in depths):
                    break
                time.sleep(0.02)
            self.assertEqual(len(depths), 4)
            for depth in depths:
                self.assertAlmostEqual(depth, expected, places=5)

        try:
            runtime.start()
            client = SimpleUDPClient('127.0.0.1', runtime.oscquery.osc_port)
            root = '/avatar/parameters/OGB/Orf/Own/PenSelfNewRoot'
            tip = '/avatar/parameters/OGB/Orf/Own/PenSelfNewTip'
            own_plug = '/avatar/parameters/OGB/Pen/Own/PenSelf'
            other_plug = '/avatar/parameters/OGB/Pen/Own/PenOthers'
            for _ in range(4):
                client.send_message(root, 0.2)
                client.send_message(tip, 0.6)
            client.send_message(root, 0.8)
            client.send_message(tip, 1.0)
            assert_depth(0.5)
            client.send_message(own_plug, 0.7)
            assert_depth(0.7)
            client.send_message(other_plug, 0.9)
            assert_depth(0.9)
            client.send_message(other_plug, 0)
            assert_depth(0.7)
            client.send_message(own_plug, 0)
            assert_depth(0.5)
            client.send_message(root, 0)
            client.send_message(tip, 0)
            assert_depth(0)
            self.assertTrue(all(not handler.is_active for handler in runtime.handlers))
        finally:
            if client is not None:
                client._sock.close()
            runtime.stop()

    def test_occupied_osc_and_query_ports_negotiate_available_ports(self):
        settings = apply_basic_settings(
            copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS),
        )
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['chatbox']['enable'] = False
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied_udp, \
                socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied_http:
            occupied_udp.bind(('127.0.0.1', 0))
            occupied_http.bind(('127.0.0.1', 0))
            occupied_http.listen()
            settings['osc']['listen_port'] = occupied_udp.getsockname()[1]
            settings['oscquery']['listen_port'] = occupied_http.getsockname()[1]
            original_settings = copy.deepcopy(settings)
            runtime = DeviceRuntime(settings)
            client = None
            try:
                runtime.start()
                self.assertNotEqual(runtime.oscquery.osc_port, settings['osc']['listen_port'])
                self.assertNotEqual(runtime.oscquery.http_port, settings['oscquery']['listen_port'])
                with urlopen(f'http://127.0.0.1:{runtime.oscquery.http_port}/?HOST_INFO', timeout=2) as response:
                    host_info = json.load(response)
                self.assertEqual(host_info['OSC_PORT'], runtime.oscquery.osc_port)
                client = SimpleUDPClient(host_info['OSC_IP'], host_info['OSC_PORT'])
                client.send_message('/avatar/parameters/OGB/Pen/Locked/PenOthers', 0.7)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    value = runtime.handlers[0].get_mode_info()['strength_percentage']
                    if abs(value - 0.7) < 0.00001:
                        break
                    time.sleep(0.02)
                self.assertAlmostEqual(value, 0.7, places=5)
                self.assertEqual(settings, original_settings)
            finally:
                if client is not None:
                    client._sock.close()
                runtime.stop()

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
                'upper_strength': {'A': 120, 'B': 110},
            }, {
                'slot_id': 'opossum-slot',
                'device_type': 'OVC_1',
                'strength': {'A': 100, 'B': 80},
                'upper_strength': {'A': 150, 'B': 140},
            }),
        )
        with patch('shocking_vrchat.srv.get_ws_connections', return_value=(connection,)):
            response = shocking_vrchat.build_status_response()
            legacy_response = shocking_vrchat.build_legacy_status_response()
        self.assertEqual(
            [(item['device'], item['attr']['slot_id']) for item in response['devices']],
            [('coyotev3', 'coyote-slot'), ('opossum', 'opossum-slot')],
        )
        self.assertEqual(len(legacy_response['devices']), 2)
        self.assertEqual(legacy_response['devices'][0]['device'], 'coyotev3')
        self.assertEqual(
            legacy_response['devices'][0]['attr']['strength'],
            {'A': 120, 'B': 110},
        )
        self.assertEqual(
            legacy_response['devices'][0]['attr']['actual_strength'],
            {'A': 80, 'B': 60},
        )

    def test_map_status_prioritizes_coyote_and_supports_opossum_only(self):
        coyote = {
            'device': 'coyotev3',
            'attr': {'strength': {'A': 10, 'B': 20},
                     'upper_strength': {'A': 70, 'B': 60}},
        }
        opossum = {
            'device': 'opossum',
            'attr': {'strength': {'A': 0, 'B': 0},
                     'upper_strength': {'A': 120, 'B': 130}},
        }
        for devices, expected in (([opossum, coyote], ['coyotev3', 'opossum']),
                                  ([opossum], ['opossum']), ([], [])):
            with self.subTest(devices=expected), patch(
                'shocking_vrchat.build_status_response',
                return_value={'healthy': 'ok', 'devices': devices},
            ):
                response = shocking_vrchat.app.test_client().get('/api/v1/status').get_json()
                self.assertEqual([device['device'] for device in response['devices']], expected)
                if response['devices']:
                    upper = coyote if len(devices) == 2 else opossum
                    self.assertEqual(response['devices'][0]['attr']['strength'],
                                     upper['attr']['upper_strength'])

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
    def setUp(self):
        for target in ('srv.oscquery.Zeroconf', 'srv.oscquery.ServiceBrowser'):
            isolated = patch(target)
            isolated.start()
            self.addCleanup(isolated.stop)

    def make_settings(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        for connection in settings['connections'].values():
            connection['mode'] = 'app'
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
                # The App has only reported 12. Sending a target of 80 must
                # never impersonate a confirmed physical strength of 80.
                self.assertEqual(snapshot['channels']['A']['actual_strength'], 12)

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

    async def test_http_map_events_use_shared_switches_caps_and_hot_updates(self):
        settings, basic = self.make_settings()
        enabled = {('coyote', 'A'), ('opossum', 'B')}
        for kind in ('coyote', 'opossum'):
            for channel in ('A', 'B'):
                config = basic['dglab3'][kind][f'channel_{channel.lower()}']
                config['extra_parameters']['enabled'] = (kind, channel) in enabled
                config['strength_limit'] = 80 if kind == 'coyote' else 130
        controller = ServiceController()

        def request_event(channel):
            url = f'http://127.0.0.1:{settings["web_server"]["listen_port"]}/api/v1/shock/{channel}/0.3'
            request = Request(url, headers={'User-Agent': 'UnityPlayer/test'})
            with urlopen(request, timeout=2) as response:
                return json.load(response)

        async def collect_operations(websocket):
            operations = []
            deadline = asyncio.get_running_loop().time() + 0.7
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return operations
                try:
                    frame = json.loads(await asyncio.wait_for(websocket.recv(), remaining))
                except asyncio.TimeoutError:
                    return operations
                data = frame.get('data') or {}
                if data.get('m') in ('device.op', 'device.op.clear'):
                    operations.append((data['m'], data['data']))

        try:
            controller.start(settings, basic)
            uri = f'ws://127.0.0.1:{settings["ws"]["listen_port"]}/?tid={settings["ws"]["master_uuid"]}'
            async with websocket_connect(uri) as websocket:
                for _ in range(3):
                    await asyncio.wait_for(websocket.recv(), 2)
                await websocket.send(json.dumps({
                    'type': 'message',
                    'data': {
                        't': 'ev', 'ev': 'devices.snapshot',
                        'devices': [{
                            'slotId': 'coyote-slot', 'type': 'COYOTE_030',
                            'props': {'intensityA': 0, 'intensityB': 0},
                            'slotState': {
                                'hasDevice': True,
                                'channelA': {'intensityMax': 70},
                                'channelB': {'intensityMax': 90},
                            },
                        }, {
                            'slotId': 'opossum-slot', 'type': 'OVC_1',
                            'props': {'intensityA': 0, 'intensityB': 0},
                            'slotState': {'hasDevice': True},
                        }],
                    },
                }))
                deadline = time.monotonic() + 2
                while not controller.snapshot()['connected'] and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(controller.snapshot()['connected'])
                self.assertEqual(await asyncio.to_thread(request_event, 'all'), {'result': 'OK'})
                operations = await collect_operations(websocket)
                map_waves = [op for method, op in operations if method == 'device.op' and op['t'] == 0]
                self.assertTrue(map_waves)
                self.assertTrue(all(op['v'] == ['0A0A0A0A64646464'] for op in map_waves))
                self.assertEqual(
                    {(op['s'], op['c']) for method, op in operations
                     if method == 'device.op' and op['t'] == 0},
                    {('coyote-slot', 0), ('opossum-slot', 1)},
                )
                self.assertIn(
                    ('device.op', {'s': 'opossum-slot', 't': 3, 'c': 1, 'p': 1, 'v': 130}),
                    operations,
                )
                self.assertIn(
                    ('device.op', {'s': 'opossum-slot', 't': 7, 'c': 1, 'p': 1, 'v': 0}),
                    operations,
                )
                self.assertEqual(
                    controller.snapshot()['device_channels']['coyote']['A']['upper_strength'], 70,
                )
                self.assertFalse(controller.snapshot()['device_channels']['opossum']['B']['is_active'])
                for kind in ('coyote', 'opossum'):
                    for channel in ('A', 'B'):
                        config = basic['dglab3'][kind][f'channel_{channel.lower()}']
                        config['extra_parameters']['enabled'] = (kind, channel) not in enabled
                connection = srv.get_ws_connections()[0]
                await asyncio.to_thread(controller.apply_settings, settings, basic)
                self.assertIs(srv.get_ws_connections()[0], connection)
                self.assertEqual(await asyncio.to_thread(request_event, 'A'), {'result': 'OK'})
                operations = await collect_operations(websocket)
                self.assertEqual(
                    {(op['s'], op['c']) for method, op in operations
                     if method == 'device.op' and op['t'] == 0},
                    {('opossum-slot', 0)},
                )
                self.assertFalse(controller.snapshot()['device_channels']['opossum']['A']['is_active'])
        finally:
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
    def test_native_channel_checkboxes_fit_and_save_independently(self):
        # A hidden native parent exercises actual controls without starting
        # services, writing user settings, or connecting to an APP/device.
        app = object.__new__(DesktopApplication)
        app.settings = copy.deepcopy(DEFAULT_SETTINGS)
        app.settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        app.basic_settings = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        app.controls = {}
        app.panels = {'coyote': [], 'opossum': []}
        app._combo_buffers = []
        app.sps_zone_options = {
            SPS_SOCKET: [SPSZone(SPS_SOCKET, '*', '任何 Socket')],
            SPS_PLUG: [SPSZone(SPS_PLUG, '*', '任何 Plug')],
        }
        app.channel_zone_selections = {
            (kind, channel): {SPS_SOCKET: '*', SPS_PLUG: '*'}
            for kind in ('coyote', 'opossum') for channel in ('A', 'B')
        }
        app.hwnd = user32.CreateWindowExW(
            0, 'STATIC', 'checkbox layout test', 0, 0, 0, 600, 540,
            None, None, kernel32.GetModuleHandleW(None), None,
        )
        self.assertTrue(app.hwnd)
        app.font = gdi32.CreateFontW(-17, 0, 0, 0, 400, False, False, False,
                                   1, 0, 0, 5, 0, 'Segoe UI')
        self.assertTrue(app.font)
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        gdi32.SelectObject.restype = wintypes.HANDLE
        gdi32.GetTextExtentPoint32W.argtypes = [
            wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.SIZE),
        ]
        hdc = user32.GetDC(app.hwnd)
        self.assertTrue(hdc)
        old_font = gdi32.SelectObject(hdc, app.font)
        expected = {}
        try:
            for key in ('chatbox', 'background', 'steamvr_auto_start'):
                app._check(key, key, False, 0, 0, 100)
            for kind in ('coyote', 'opossum'):
                for index, channel in enumerate(('A', 'B')):
                    app._build_device_channel(kind, channel, 0, index * 252, kind)
                    for field, default in default_trigger_switches(kind).items():
                        key = f'{field}_{kind}_{channel.lower()}'
                        self.assertIs(app._is_checked(key), default)
                    extra_key = f'extra_enabled_{kind}_{channel.lower()}'
                    self.assertTrue(app._is_checked(extra_key))
                    keys = [f'{field}_{kind}_{channel.lower()}' for field in TRIGGER_SWITCH_FIELDS] + [extra_key]
                    for position, key in enumerate(keys):
                        text = app._get_text(key)
                        extent, rect = wintypes.SIZE(), wintypes.RECT()
                        self.assertTrue(gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(extent)))
                        self.assertTrue(user32.GetWindowRect(app.controls[key], ctypes.byref(rect)))
                        self.assertLessEqual(extent.cx + 24, rect.right - rect.left, text)
                        self.assertLessEqual(extent.cy, rect.bottom - rect.top, text)
                        value = (position + index + (kind == 'opossum')) % 2 == 0
                        app._set_checked(key, value)
                        expected[key] = value
            _settings, basic = app._read_form()
            for kind in ('coyote', 'opossum'):
                for channel in ('a', 'b'):
                    config = basic['dglab3'][kind][f'channel_{channel}']
                    for field in TRIGGER_SWITCH_FIELDS:
                        self.assertIs(config[field], expected[f'{field}_{kind}_{channel}'])
                    self.assertIs(config['extra_parameters']['enabled'], expected[f'extra_enabled_{kind}_{channel}'])
                    self.assertEqual(config['socket_zone'], '*')
                    self.assertEqual(config['plug_zone'], '*')
            self.assertTrue(app.basic_settings['dglab3']['coyote']['channel_a']['socket_enabled'])
        finally:
            gdi32.SelectObject(hdc, old_font)
            user32.ReleaseDC(app.hwnd, hdc)
            user32.DestroyWindow(app.hwnd)
            gdi32.DeleteObject(app.font)

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
