import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from loguru import logger
from pythonosc.osc_message import OscMessage

import shocking_vrchat as core
from srv.desktop_backend import BackendAPI
from srv.oscquery import OGB_ENABLED_INTERVAL, OSCQueryAvatarSnapshot, VRChatOSCQueryClient
from srv.diagnostics import websocket_latency
from types import SimpleNamespace


class OGBHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.receiver.bind(('127.0.0.1', 0))
        self.receiver.settimeout(0.1)
        self.addCleanup(self.receiver.close)
        self.host_info = {'OSC_IP': '127.0.0.1', 'OSC_PORT': self.receiver.getsockname()[1], 'OSC_TRANSPORT': 'UDP'}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.assertEqual(self.path, '/?HOST_INFO')
                body = json.dumps(outer.host_info).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.http.shutdown)
        self.client = VRChatOSCQueryClient(None, lambda _snapshot: None, request_timeout=0.2)
        self.client.last_query_success = time.perf_counter()
        self.client._publish(OSCQueryAvatarSnapshot('', (), f'http://127.0.0.1:{self.http.server_port}'))

    def test_sends_boolean_true_to_discovered_osc_not_http_port(self):
        self.assertTrue(self.client.send_ogb_enabled())
        packet = self.receiver.recv(4096)
        message = OscMessage(packet)
        self.assertEqual(message.address, '/avatar/parameters/OGB_ENABLED')
        self.assertEqual(message.params, [True])
        self.assertIn(b',T\0', packet)

    def test_wildcard_ip_resolves_to_discovered_game_host(self):
        self.host_info['OSC_IP'] = '0.0.0.0'
        self.assertTrue(self.client.send_ogb_enabled())
        self.assertEqual(OscMessage(self.receiver.recv(4096)).params, [True])

    def test_heartbeat_waits_five_seconds_and_stops_without_false(self):
        self.client.stop_event = Mock()
        self.client.stop_event.wait.side_effect = [False, False, True]
        self.client.send_ogb_enabled = Mock()
        self.client._ogb_heartbeat()
        self.assertEqual(OGB_ENABLED_INTERVAL, 5)
        self.assertEqual(self.client.send_ogb_enabled.call_count, 2)
        self.assertEqual([call.args for call in self.client.stop_event.wait.call_args_list], [(5,), (5,), (5,)])

    def test_stale_or_stopped_game_sends_nothing(self):
        self.client.last_query_success = time.perf_counter() - 6
        self.assertFalse(self.client.send_ogb_enabled())
        self.client.last_query_success = time.perf_counter()
        self.client.stop()
        self.assertFalse(self.client.send_ogb_enabled())
        with self.assertRaises(socket.timeout):
            self.receiver.recv(4096)

    def test_bad_host_info_does_not_guess_port_or_send(self):
        for port in (None, True, 0, 65536, '9000'):
            self.host_info['OSC_PORT'] = port
            self.assertFalse(self.client.send_ogb_enabled())
        with self.assertRaises(socket.timeout):
            self.receiver.recv(4096)

    def test_worker_stops_promptly_before_next_heartbeat(self):
        self.client.heartbeat_thread = threading.Thread(target=self.client._ogb_heartbeat, daemon=True)
        self.client.heartbeat_thread.start()
        started = time.perf_counter()
        thread = self.client.heartbeat_thread
        self.client.stop()
        self.assertFalse(thread.is_alive())
        self.assertLess(time.perf_counter() - started, 0.5)


class RunLogLocationTests(unittest.TestCase):
    def setUp(self):
        self.saved = {key: getattr(core, key) for key in (
            'SETTINGS', 'SETTINGS_BASIC', 'CONFIG_MANAGER', 'CONFIG_FILENAME',
            'CONFIG_FILENAME_BASIC', 'SERVER_IP', 'RUN_LOG_PATH', 'APP_DIR')}
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.addCleanup(self.restore)
        self.program = Path(self.root.name) / 'program'
        self.roaming = Path(self.root.name) / 'roaming'
        self.program.mkdir()
        core.APP_DIR = self.program

    def restore(self):
        logger.remove()
        for key, value in self.saved.items():
            setattr(core, key, value)

    def test_production_backend_writes_next_to_frontend_and_exposes_path(self):
        with patch.dict('os.environ', {'APPDATA': str(self.roaming)}):
            api = BackendAPI(launch_executable=self.program / 'Neko-VRC.exe')
        try:
            path = Path(api.state()['log_path'])
            self.assertEqual(path.parent, self.program / 'log')
            logger.info('计算强度 device=coyote channel=A calculated=35.00')
            logger.info('延迟 stage=app.req_ack last_ms=4.000')
            contents = path.read_text(encoding='utf-8')
            self.assertIn('calculated=35.00', contents)
            self.assertIn('app.req_ack', contents)
            self.assertIn('neko-vrc:', contents)
            self.assertNotIn('shocking_vrchat:', contents)
            self.assertEqual(api.state()['logs'], [])  # General/file logs are hidden from Debug.
            logger.bind(strength_event=True, device_kind='coyote', channel='A', calculated=35.0, actual=None, strength_hosts=[]).info('private diagnostic fields')
            self.assertIn('郊狼 A：计算 35 / 实际 未反馈', api.state()['logs'][-1])
            self.assertNotIn('private diagnostic', api.state()['logs'][-1])
            count = len(api.state()['logs'])
            logger.bind(strength_event=True, device_kind='coyote', channel='A', calculated=35.0, actual=None, strength_hosts=[]).info('same values')
            self.assertEqual(len(api.state()['logs']), count)
            logger.bind(strength_event=True, device_kind='coyote', channel='A', calculated=35.0, actual=40, strength_hosts=[]).info('feedback changed')
            self.assertEqual(len(api.state()['logs']), count + 1)
            self.assertIn('实际 40', api.state()['logs'][-1])
            self.assertEqual(api.manager.path.parent, self.roaming / 'Neko-VRC')
        finally:
            api.close()

    def test_unwritable_program_directory_falls_back_to_config_log(self):
        preferred = self.program / 'log'
        real_mkdir = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path == preferred:
                raise PermissionError('test protected installation directory')
            return real_mkdir(path, *args, **kwargs)

        with patch.dict('os.environ', {'APPDATA': str(self.roaming)}), patch.object(Path, 'mkdir', mkdir):
            core.config_init()
        self.assertEqual(core.RUN_LOG_PATH.parent, self.roaming / 'Neko-VRC' / 'log')
        self.assertIn('已回退', core.RUN_LOG_PATH.read_text(encoding='utf-8'))

    def test_explicit_test_profile_stays_isolated(self):
        profile = Path(self.root.name) / 'test-profile'
        api = BackendAPI(config_dir=profile, launch_executable=self.program / 'Neko-VRC.exe')
        try:
            self.assertEqual(Path(api.initial()['log_path']).parent, profile / 'log')
            self.assertFalse((self.program / 'log').exists())
        finally:
            api.close()

    def test_strength_info_is_saved_even_with_warning_console_level(self):
        profile = Path(self.root.name) / 'test-profile'
        manager = core.ConfigManager(self.program, config_dir=profile)
        settings, basic = manager.load()
        settings['log_level'] = 'WARNING'
        manager.save(settings, basic)
        core.config_init(profile)
        logger.info('calculated=42.00 diagnostic-info-marker')
        self.assertIn('diagnostic-info-marker', core.RUN_LOG_PATH.read_text(encoding='utf-8'))


class AppLatencyTests(unittest.TestCase):
    def test_no_sample_or_invalid_sample_is_not_zero_latency(self):
        for seconds in (None, 0, -1, float('nan'), float('inf'), True, Mock()):
            self.assertIsNone(websocket_latency(SimpleNamespace(latency=seconds)))

    def test_app_ping_seconds_are_presented_as_milliseconds(self):
        self.assertEqual(websocket_latency(SimpleNamespace(latency=0.0125)), {'kind': 'app_ping', 'milliseconds': 12.5})

    def test_ack_is_preferred_but_old_ack_expires(self):
        connection = SimpleNamespace(latency=0.0125)
        self.assertEqual(websocket_latency(connection, (5, time.perf_counter())), {'kind': 'app_ack', 'milliseconds': 5})
        self.assertEqual(websocket_latency(connection, (5, time.perf_counter() - 31)), {'kind': 'app_ping', 'milliseconds': 12.5})
