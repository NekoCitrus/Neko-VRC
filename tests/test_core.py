import asyncio
import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import patch

import shocking_vrchat
import srv
from srv.advanced_chatbox_manager import AdvancedChatboxManager
from srv.handler.base_handler import BaseHandler
from srv.handler.shock_handler import ShockHandler
from srv.sps_depth import (
    SPSDepthCalculator,
    SPS_PLUG,
    SPS_SOCKET,
    find_avatar_sps_config,
    load_avatar_sps_config,
)
from srv.waveform import (
    frame_amplitudes,
    normalize_socket_frame,
    scale_coyote_frame,
)


class FakeDGConnection:
    def __init__(self):
        self.waves = []
        self.cleared = []

    async def broadcast_wave(self, channel, wavestr, device_kind='coyote'):
        self.waves.append((device_kind, channel, json.loads(wavestr)))

    async def broadcast_clear_wave(self, channel, device_kind='coyote'):
        self.cleared.append((device_kind, channel))


def make_settings():
    return {
        'dglab3': {
            device_kind: {
                'channel_a': {
                    'socket_zone': 'TestSocket',
                    'plug_zone': 'Test',
                    'extra_parameters': {
                        'enabled': True,
                        'paths': ['/avatar/parameters/Shock/TouchAreaA'],
                        'bottom': 0.0,
                        'top': 1.0,
                    },
                    'strength_limit': 100,
                    'depth': {
                        'freq_ms': 10,
                        'waveform': srv.WAVEFORM_NAMES_BY_DEVICE[device_kind][0],
                    },
                },
            }
            for device_kind in ('coyote', 'opossum')
        },
    }


class BaseHandlerTests(unittest.TestCase):
    def test_bool_is_normalized_to_integer(self):
        self.assertEqual(BaseHandler.param_sanitizer((True,)), 1)
        self.assertIs(type(BaseHandler.param_sanitizer((True,))), int)

    def test_multiple_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            BaseHandler.param_sanitizer((0.1, 0.2))


class ShockHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_kinds_use_independent_source_settings(self):
        settings = make_settings()
        settings['dglab3']['coyote']['channel_a'].update({
            'socket_zone': 'CoyoteSocket',
        })
        settings['dglab3']['opossum']['channel_a'].update({
            'plug_zone': 'OpossumPlug',
        })

        coyote = ShockHandler(settings, FakeDGConnection(), 'A', device_kind='coyote')
        opossum = ShockHandler(settings, FakeDGConnection(), 'A', device_kind='opossum')

        self.assertEqual(coyote.get_mode_info()['device_kind'], 'coyote')
        self.assertEqual(opossum.get_mode_info()['device_kind'], 'opossum')
        self.assertTrue(any('/OGB/Orf/CoyoteSocket/' in path for path, _field in coyote.osc_bindings()))
        self.assertTrue(any('/OGB/Pen/OpossumPlug/' in path for path, _field in opossum.osc_bindings()))
        self.assertNotEqual(coyote.waveform, opossum.waveform)

    async def test_depth_updates_activity_state(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        await handler.handler_depth(0.5)
        self.assertTrue(handler.is_active)
        self.assertEqual(handler.current_strength_percentage, 0.5)

    async def test_debug_state_tracks_exact_waveform_strengths_sent_to_app(self):
        connection = FakeDGConnection()
        handler = ShockHandler(make_settings(), connection, 'A')
        handler.depth_current_strength = 0.5
        task = asyncio.create_task(handler.depth_background_wave_feeder())
        try:
            for _ in range(30):
                if connection.waves:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(connection.waves)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        sent_frame = connection.waves[-1][2][0]
        self.assertEqual(handler.last_sent_frame, sent_frame)
        self.assertEqual(handler.last_sent_strengths, frame_amplitudes(sent_frame))
        self.assertEqual(handler.get_mode_info()['sent_strengths'], frame_amplitudes(sent_frame))

    async def test_debug_event_contains_parameter_raw_and_mapped_values(self):
        events = []
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A', event_callback=events.append)
        result = handler.osc_handler(
            '/avatar/parameters/OGB/Pen/Test/PenOthers',
            (SPS_PLUG, 'PenOthers'), 0.25,
        )
        await asyncio.sleep(0)
        self.assertIsNone(result)
        self.assertEqual(handler.last_parameter, '/avatar/parameters/OGB/Pen/Test/PenOthers')
        self.assertEqual(handler.last_raw_value, 0.25)
        self.assertTrue(any(event.get('strength_percentage') == 0.25 for event in events))

    async def test_any_zone_uses_the_deepest_currently_triggering_zone(self):
        settings = make_settings()
        settings['dglab3']['coyote']['channel_a']['plug_zone'] = '*'
        handler = ShockHandler(settings, FakeDGConnection(), 'A')
        binding = (SPS_PLUG, 'PenOthers')
        handler.osc_handler('/avatar/parameters/OGB/Pen/First/PenOthers', binding, 0.25)
        handler.osc_handler('/avatar/parameters/OGB/Pen/Second/PenOthers', binding, 0.7)
        await asyncio.sleep(0)
        self.assertEqual(handler.active_zone, 'Second')
        self.assertEqual(handler.current_strength_percentage, 0.7)
        handler.osc_handler('/avatar/parameters/OGB/Pen/Second/PenOthers', binding, 0.0)
        await asyncio.sleep(0)
        self.assertEqual(handler.active_zone, 'First')
        self.assertEqual(handler.current_strength_percentage, 0.25)

    async def test_avatar_change_clears_previous_zone_values(self):
        settings = make_settings()
        handler = ShockHandler(settings, FakeDGConnection(), 'A')
        handler.osc_handler(
            '/avatar/parameters/OGB/Pen/Test/PenOthers',
            (SPS_PLUG, 'PenOthers'), 0.6,
        )
        await asyncio.sleep(0)
        handler.reset_for_avatar_change()
        await asyncio.sleep(0)
        self.assertEqual(handler.calculators, {})
        self.assertEqual(handler.active_zone, '')
        self.assertEqual(handler.current_strength_percentage, 0.0)

    async def test_socket_plug_and_extra_parameter_use_maximum(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        handler.osc_handler(
            '/avatar/parameters/OGB/Pen/Test/PenOthers',
            (SPS_PLUG, 'PenOthers'), 0.35,
        )
        handler.osc_handler(
            '/avatar/parameters/OGB/Orf/TestSocket/PenOthers',
            (SPS_SOCKET, 'PenOthers'), 0.6,
        )
        handler.osc_handler(
            '/avatar/parameters/Shock/TouchAreaA', ('extra', 'value'), 0.8,
        )
        await asyncio.sleep(0)
        self.assertEqual(handler.current_strength_percentage, 0.8)
        self.assertEqual(handler.active_source, 'extra')

    async def test_extra_parameter_expires_after_half_second(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        handler.osc_handler(
            '/avatar/parameters/Shock/TouchAreaA', ('extra', 'value'), 0.8,
        )
        task = asyncio.create_task(handler.clear_check())
        try:
            await asyncio.sleep(0.56)
            self.assertEqual(handler.current_strength_percentage, 0.0)
            self.assertFalse(handler.is_active)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class SPSDepthTests(unittest.TestCase):
    def test_avatar_json_exposes_socket_and_plug_dropdown_options(self):
        document = {
            'id': 'avtr_test',
            'name': 'Test Avatar',
            'parameters': [
                {
                    'name': 'OGB/Orf/Test Socket/PenOthersNewRoot',
                    'output': {
                        'address': '/avatar/parameters/OGB/Orf/Test_Socket/PenOthersNewRoot',
                        'type': 'Float',
                    },
                },
                {
                    'name': 'OGB/Pen/Test Plug/PenOthers',
                    'output': {
                        'address': '/avatar/parameters/OGB/Pen/Test_Plug/PenOthers',
                        'type': 'Float',
                    },
                },
            ],
        }
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'avtr_test.json'
            path.write_text(json.dumps(document), encoding='utf-8')
            avatar = load_avatar_sps_config(path)
        self.assertEqual([(item.label, item.zone_id) for item in avatar.sockets], [('Test Socket', 'Test_Socket')])
        self.assertEqual([(item.label, item.zone_id) for item in avatar.plugs], [('Test Plug', 'Test_Plug')])

    def test_avatar_lookup_uses_the_id_reported_by_vrchat(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            for avatar_id, avatar_name in (
                ('avtr_old', 'Old Avatar'),
                ('avtr_current', 'Current Avatar'),
            ):
                (root_path / f'{avatar_id}.json').write_text(
                    json.dumps({'id': avatar_id, 'name': avatar_name, 'parameters': []}),
                    encoding='utf-8',
                )
            avatar = find_avatar_sps_config('avtr_current', root_path)
        self.assertIsNotNone(avatar)
        self.assertEqual(avatar.avatar_id, 'avtr_current')
        self.assertEqual(avatar.avatar_name, 'Current Avatar')
        self.assertIsNone(find_avatar_sps_config('../invalid', '.'))

    def test_plug_uses_deepest_self_or_other_socket(self):
        calculator = SPSDepthCalculator(SPS_PLUG)
        calculator.update('PenSelf', 0.25)
        self.assertEqual(calculator.update('PenOthers', 0.7), 0.7)

    def test_socket_root_tip_depth_matches_ogb_formula(self):
        calculator = SPSDepthCalculator(SPS_SOCKET)
        for _ in range(4):
            calculator.update('PenOthersNewRoot', 0.2)
            calculator.update('PenOthersNewTip', 0.6)
        calculator.update('PenOthersNewRoot', 0.8)
        self.assertAlmostEqual(calculator.update('PenOthersNewTip', 1.0), 0.5)


class WaveformProtocolTests(unittest.TestCase):
    def test_official_device_waveform_libraries_are_separate(self):
        self.assertEqual(len(srv.COYOTE_WAVEFORMS), 24)
        self.assertEqual(len(srv.OPOSSUM_WAVEFORMS), 20)
        self.assertNotEqual(
            tuple(srv.COYOTE_WAVEFORMS), tuple(srv.OPOSSUM_WAVEFORMS),
        )

    def test_named_coyote_frame_is_scaled_by_depth(self):
        self.assertEqual(
            scale_coyote_frame('0A141E2864643219', 0.5),
            '0A141E283232190C',
        )

    def test_ovc_socket_frame_uses_fixed_prefix_and_selected_amplitudes(self):
        self.assertEqual(
            normalize_socket_frame('1919181864643219', 'OVC_1'),
            '0A0A0A0A64643219',
        )

    def test_coyote_socket_frame_is_preserved(self):
        self.assertEqual(
            normalize_socket_frame('1919181864643219', 'COYOTE_030'),
            '1919181864643219',
        )


class ConfigAndApiTests(unittest.TestCase):
    def setUp(self):
        self.original_global_settings = copy.deepcopy(shocking_vrchat.SETTINGS)
        self.settings = copy.deepcopy(self.original_global_settings)
        self.basic = copy.deepcopy(shocking_vrchat.SETTINGS_BASIC)
        self.settings['ws']['master_uuid'] = str(uuid.uuid4())

    def tearDown(self):
        shocking_vrchat.SETTINGS.clear()
        shocking_vrchat.SETTINGS.update(self.original_global_settings)

    def test_default_config_is_valid(self):
        shocking_vrchat.validate_config(self.settings, self.basic)

    def test_invalid_sps_zone_is_rejected(self):
        self.basic['dglab3']['coyote']['channel_a']['socket_zone'] = 'bad/zone'
        with self.assertRaises(ValueError):
            shocking_vrchat.validate_config(self.settings, self.basic)

    def test_debug_sendwav_route_was_removed(self):
        response = shocking_vrchat.app.test_client().get('/sendwav')
        self.assertEqual(response.status_code, 404)

    def test_control_api_is_disabled_by_default(self):
        shocking_vrchat.SETTINGS['api']['control_enabled'] = False
        response = shocking_vrchat.app.test_client().get(
            '/api/v1/shock/A/1',
            headers={'User-Agent': 'UnityPlayer/test'},
        )
        self.assertEqual(response.status_code, 401)

    def test_wave_request_validation(self):
        self.assertEqual(
            shocking_vrchat.normalize_wave_request('a', '2', '0A0A0A0A64646464'),
            ('A', 2, '0A0A0A0A64646464'),
        )
        with self.assertRaises(ValueError):
            shocking_vrchat.normalize_wave_request('C', 2, '0A0A0A0A64646464')

    def test_qr_content_uses_official_v4_pairing_format(self):
        content = shocking_vrchat.build_qr_content(self.settings, server_ip='192.168.1.8')
        parsed = urlsplit(content)
        self.assertEqual(parsed.scheme, 'https')
        self.assertEqual(parsed.netloc, 'dungeon-lab.cn')
        self.assertEqual(parsed.path, '/s/')
        query = parse_qs(parsed.query)
        self.assertEqual(query['v'], ['1'])
        self.assertEqual(query['action'], ['socket'])
        websocket_url = unquote(query['url'][0])
        self.assertEqual(
            websocket_url,
            f'ws://192.168.1.8:{self.settings["ws"]["listen_port"]}/'
            f'?tid={self.settings["ws"]["master_uuid"]}',
        )
        self.assertEqual(
            websocket_url,
            shocking_vrchat.build_websocket_url(self.settings, server_ip='192.168.1.8'),
        )

    @patch('shocking_vrchat.socket.getaddrinfo')
    @patch('shocking_vrchat.socket.socket')
    def test_lan_ip_detection_falls_back_to_adapter_addresses(self, socket_factory, getaddrinfo):
        socket_factory.return_value.__enter__.return_value.connect.side_effect = OSError
        getaddrinfo.return_value = [
            (2, 2, 17, '', ('127.0.0.1', 0)),
            (2, 2, 17, '', ('192.168.50.12', 0)),
        ]
        self.assertEqual(shocking_vrchat.detect_current_ip(self.settings), '192.168.50.12')

    @patch('shocking_vrchat.socket.getaddrinfo')
    @patch('shocking_vrchat.socket.socket')
    def test_lan_ip_detection_ignores_proxy_benchmark_adapter(self, socket_factory, getaddrinfo):
        route_socket = socket_factory.return_value.__enter__.return_value
        route_socket.getsockname.return_value = ('198.18.0.1', 53123)
        getaddrinfo.return_value = [
            (2, 2, 17, '', ('198.18.0.1', 0)),
            (2, 2, 17, '', ('192.168.5.2', 0)),
        ]
        self.assertEqual(shocking_vrchat.detect_current_ip(self.settings), '192.168.5.2')


class ChatboxTests(unittest.TestCase):
    def test_disabled_manager_still_accepts_channel_updates(self):
        manager = AdvancedChatboxManager({'chatbox': {'enable': False}})
        manager.update_channel_mode('A', 'sps_socket', 0.5, is_active=True)
        self.assertTrue(manager.channel_modes['coyote']['A']['is_active'])

    def test_cleanup_closes_udp_socket(self):
        manager = AdvancedChatboxManager({
            'chatbox': {
                'enable': True,
                'osc_host': '127.0.0.1',
                'osc_port': 9000,
                'set_avatar_parameter': False,
            },
        })
        osc_socket = manager.osc_client._sock
        manager.cleanup()
        self.assertEqual(osc_socket.fileno(), -1)
        self.assertIsNone(manager.osc_client)


if __name__ == '__main__':
    unittest.main()
