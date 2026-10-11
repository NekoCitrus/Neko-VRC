import copy
import json
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch
from loguru import logger

import shocking_vrchat as core
from srv.desktop_backend import BackendAPI, serve_stream
from srv.config_manager import DEFAULT_SETTINGS, DEFAULT_BASIC_SETTINGS


class DuplexFixture:
    def __init__(self, requests):
        self.lines = queue.Queue()
        for request in requests:
            self.lines.put(json.dumps(request).encode() + b'\n')
        self.lines.put(b'')
        self.responses = []

    def readline(self, limit):
        return self.lines.get()

    def write(self, response):
        self.responses.append(json.loads(response))

    def flush(self):
        pass


class PipeAPITests(unittest.TestCase):
    def test_request_errors_do_not_kill_protocol_and_shutdown_closes(self):
        api = Mock()
        api.dispatch.side_effect = lambda method, data: {'method': method}
        stream = DuplexFixture([[], {'id': 1, 'method': 'snapshot'}, {'id': 2, 'method': 'shutdown'}])
        serve_stream(stream, api)
        self.assertFalse(stream.responses[0]['ok'])
        self.assertEqual(stream.responses[1]['data'], {'method': 'snapshot'})
        self.assertEqual(stream.responses[2]['id'], 2)
        api.close.assert_called_once()

    def test_parent_eof_always_closes_service(self):
        api = Mock()
        serve_stream(DuplexFixture([]), api)
        api.close.assert_called_once()

    def test_oversized_payload_closes_without_dispatch(self):
        api = Mock()
        stream = DuplexFixture([])
        stream.lines = queue.Queue()
        stream.lines.put(b'x' * (1024 * 1024 + 1))
        serve_stream(stream, api)
        api.dispatch.assert_not_called()
        api.close.assert_called_once()


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved_globals = {key: getattr(core, key) for key in (
            'SETTINGS', 'SETTINGS_BASIC', 'CONFIG_MANAGER', 'CONFIG_FILENAME',
            'CONFIG_FILENAME_BASIC', 'SERVER_IP', 'ACTIVE_CONTROLLER')}
        # Exclude workspace legacy files from isolated tests.
        with patch.object(core, 'APP_DIR', self.temp.name):
            self.api = BackendAPI(self.temp.name, 'C:/Neko-VRC.exe')

    def tearDown(self):
        self.api.close()
        logger.remove()  # Close the test process's file sink before temp cleanup.
        for key, value in self.saved_globals.items():
            setattr(core, key, value)
        self.temp.cleanup()

    def test_initial_config_has_four_independent_channels_and_routes(self):
        data = self.api.dispatch('init')
        self.assertEqual(set(data['waveforms']), {'coyote', 'opossum'})
        self.assertEqual(data['settings']['connections'], DEFAULT_SETTINGS['connections'])
        data['basic']['dglab3']['coyote']['channel_a']['socket_enabled'] = False
        self.assertTrue(self.api.basic['dglab3']['coyote']['channel_a']['socket_enabled'])

    def test_save_retains_sources_and_connection_configuration(self):
        settings, basic = copy.deepcopy(self.api.settings), copy.deepcopy(self.api.basic)
        settings['connections']['opossum'].update(mode='bluetooth', address='11:22')
        basic['dglab3']['opossum']['channel_b']['plug_enabled'] = False
        self.api.save(settings, basic)
        loaded, loaded_basic = self.api.manager.load()
        self.assertEqual(loaded['connections']['opossum']['address'], '11:22')
        self.assertFalse(loaded_basic['dglab3']['opossum']['channel_b']['plug_enabled'])

    def test_disk_failure_rolls_live_config_back(self):
        previous = copy.deepcopy(self.api.settings)
        changed = copy.deepcopy(previous)
        changed['chatbox']['enable'] = not changed['chatbox']['enable']
        with patch.object(self.api.controller, 'apply_settings') as apply, patch.object(
            self.api.manager, 'save', side_effect=OSError('disk full')
        ):
            with self.assertRaises(OSError):
                self.api.save(changed, self.api.basic)
            self.assertEqual(apply.call_count, 2)
            self.assertEqual(apply.call_args.args[0], previous)
        self.assertEqual(self.api.settings, previous)

    def test_steamvr_registration_uses_wpf_entrypoint_and_correct_argument_order(self):
        changed = copy.deepcopy(self.api.settings)
        changed['general']['steamvr_auto_start'] = True
        with patch('srv.desktop_backend.configure_steamvr_autostart') as register:
            self.api.save(changed, self.api.basic)
        register.assert_called_once_with(self.api.manager.config_dir, True, executable='C:/Neko-VRC.exe')

    def test_qr_matrix_does_not_depend_on_pillow(self):
        response = self.api.dispatch('qr')
        self.assertGreater(len(response['matrix']), 20)
        self.assertTrue(all(isinstance(pixel, bool) for row in response['matrix'] for pixel in row))

    def test_device_actions_require_running_service(self):
        with self.assertRaises(RuntimeError):
            self.api.dispatch('scan')


if __name__ == '__main__':
    unittest.main()
