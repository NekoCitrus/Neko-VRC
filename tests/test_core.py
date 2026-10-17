import asyncio
import copy
import json
import time
from itertools import product
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import Mock, patch

import shocking_vrchat
import srv
from srv.advanced_chatbox_manager import AdvancedChatboxManager
from srv.config_manager import default_trigger_switches
from srv.handler.base_handler import BaseHandler
from srv.handler.shock_handler import ShockHandler, SOURCE_LMS, SOURCE_PCS
from srv.sps_depth import (
    SPSDepthCalculator,
    SPS_PLUG,
    SPS_SOCKET,
    find_avatar_sps_config,
    load_avatar_sps_config,
    LMS_PROXIMITY_ADDRESS,
    PCS_PROXIMITY_ADDRESS,
    has_socket_depth_parameters,
)
from srv.waveform import (
    frame_amplitudes,
    load_waveform_frames,
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
                    **default_trigger_switches(device_kind),
                    # Explicit opt-ins for fallback-engine behavior tests;
                    # product defaults are checked by ConfigManager/UI tests.
                    'pcs_enabled': device_kind == 'coyote',
                    'lms_enabled': device_kind == 'opossum',
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
    def test_nonfinite_values_are_rejected(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BaseHandler.param_sanitizer((value,))

    def test_bool_is_normalized_to_integer(self):
        self.assertEqual(BaseHandler.param_sanitizer((True,)), 1)
        self.assertIs(type(BaseHandler.param_sanitizer((True,))), int)

    def test_multiple_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            BaseHandler.param_sanitizer((0.1, 0.2))


class ShockHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_lms_zero_and_timeout_never_clear_live_sps_depth(self):
        for kind in ('coyote', 'opossum'):
            for trigger, ogb in ((SPS_SOCKET, 'Orf'), (SPS_PLUG, 'Pen')):
                settings = make_settings()
                settings['dglab3'][kind]['channel_a']['lms_enabled'] = True
                handler = ShockHandler(settings, FakeDGConnection(), 'A', device_kind=kind)
                handler.start_background_jobs()
                try:
                    handler.osc_handler(f'/avatar/parameters/OGB/{ogb}/Test/PenSelf', (trigger, 'PenSelf'), 0.6)
                    handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 0.9)
                    handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 0)
                    await asyncio.sleep(0.58)
                    self.assertEqual(handler.current_strength_percentage, 0.6)
                    self.assertEqual(handler.active_source, trigger)
                finally:
                    await handler.stop_background_jobs()

    async def test_map_wave_is_constant_for_both_devices_and_restores_sps_preset(self):
        for kind in ('coyote', 'opossum'):
            connection = FakeDGConnection()
            handler = ShockHandler(make_settings(), connection, 'A', device_kind=kind)
            handler.wave_frames = ('1919191950505050', '1919191928282828')
            handler.depth_update_time_window = 0.01
            handler.start_background_jobs()
            try:
                handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf', (SPS_PLUG, 'PenSelf'), 0.25)
                await handler.trigger_map_event(1)
                await asyncio.sleep(0.06)
                self.assertTrue(connection.waves)
                self.assertTrue(all(wave[2][0] == '0A0A0A0A64646464' for wave in connection.waves))
                self.assertEqual(handler.wave_frame_index, 0)
                handler.map_event_until = time.monotonic() - 1
                for _ in range(30):
                    if handler.active_source == SPS_PLUG:
                        break
                    await asyncio.sleep(0.01)
                start = len(connection.waves)
                await asyncio.sleep(0.03)
                self.assertEqual(handler.current_strength_percentage, 0.25)
                self.assertTrue(connection.waves[start:])
                self.assertTrue(all(wave[2][0].startswith('19191919') and frame_amplitudes(wave[2][0]) in ((20,) * 4, (10,) * 4) for wave in connection.waves[start:]))
            finally:
                await handler.stop_background_jobs()

    async def test_catalog_lms_shutdown_preserves_other_live_depth_signals(self):
        for kind in ('coyote', 'opossum'):
            for trigger, ogb_kind, zone in ((SPS_SOCKET, 'Orf', 'TestSocket'), (SPS_PLUG, 'Pen', 'Test')):
                with self.subTest(kind=kind, trigger=trigger):
                    settings = make_settings()
                    settings['dglab3'][kind]['channel_a']['lms_enabled'] = True
                    handler = ShockHandler(settings, FakeDGConnection(), 'A', device_kind=kind)
                    live = f'/avatar/parameters/OGB/{ogb_kind}/{zone}/PenSelf'
                    removed = f'/avatar/parameters/OGB/{ogb_kind}/{zone}/PenOthers'
                    try:
                        handler.set_avatar_parameters((live, removed, LMS_PROXIMITY_ADDRESS))
                        handler.osc_handler(live, (trigger, 'PenSelf'), 0.65)
                        handler.osc_handler(removed, (trigger, 'PenOthers'), 0.2)
                        handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 0)
                        await asyncio.sleep(0)
                        self.assertEqual(handler.current_strength_percentage, 0.65)
                        handler.set_avatar_parameters((live,))
                        await asyncio.sleep(0)
                        self.assertEqual(handler.current_strength_percentage, 0.65)
                        self.assertEqual(handler.active_source, trigger)
                        self.assertNotIn('PenOthers', handler.calculators[(trigger, zone)].values)
                        handler.set_avatar_parameters(())
                        await asyncio.sleep(0)
                        self.assertEqual(handler.current_strength_percentage, 0)
                    finally:
                        await handler.stop_background_jobs()

    async def test_availability_hints_follow_catalog_selected_zones_and_switches(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        try:
            self.assertEqual(handler.parameter_hint, '等待读取模型 OSC 参数')
            handler.set_avatar_parameters(())
            self.assertEqual(handler.parameter_hint, '无可用的 Socket、Plug、PCS、LMS 参数')
            handler.set_avatar_parameters((PCS_PROXIMITY_ADDRESS, LMS_PROXIMITY_ADDRESS))
            self.assertIn('PCS 回退已启用', handler.parameter_hint)
            self.assertIn('可启用 LMS 回退', handler.parameter_hint)
            handler.pcs_enabled = False
            self.assertIn('可启用 PCS 回退', handler.parameter_hint)
            handler.set_avatar_parameters((
                '/avatar/parameters/OGB/Orf/TestSocket/PenSelf',
                '/avatar/parameters/OGB/Pen/Test/PenOthers',
            ))
            self.assertEqual(handler.parameter_hint, '')
            self.assertFalse(handler.socket_depth_available)
            handler.socket_zone = 'Missing'
            self.assertIn('无 Socket 深度', handler.parameter_hint)
            self.assertIn('无 PCS/LMS 回退参数', handler.parameter_hint)
            self.assertEqual(handler.get_mode_info()['parameter_hint'], handler.parameter_hint)
        finally:
            await handler.stop_background_jobs()

    async def test_query_depth_expires_without_stopping_newer_udp_depth(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        handler.start_background_jobs()
        path = '/avatar/parameters/OGB/Orf/TestSocket/PenSelf'
        other = '/avatar/parameters/OGB/Orf/TestSocket/PenOthers'
        try:
            handler.receive_query_value(path, (SPS_SOCKET, 'PenSelf'), 0.9, time.perf_counter())
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.9)
            handler.osc_handler(other, (SPS_SOCKET, 'PenOthers'), 0.4)
            await asyncio.sleep(0.58)
            self.assertEqual(handler.current_strength_percentage, 0.4)
            self.assertNotIn('PenSelf', handler.calculators[(SPS_SOCKET, 'TestSocket')].values)
            handler.receive_query_value(path, (SPS_SOCKET, 'PenSelf'), 0.9, time.perf_counter())
            handler.osc_handler(path, (SPS_SOCKET, 'PenSelf'), 0.2)
            await asyncio.sleep(0.58)
            self.assertEqual(handler.calculators[(SPS_SOCKET, 'TestSocket')].values['PenSelf'], 0.2)
        finally:
            await handler.stop_background_jobs()

    async def test_query_only_depth_stops_after_timeout(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        handler.start_background_jobs()
        try:
            handler.receive_query_value('/avatar/parameters/OGB/Orf/TestSocket/PenSelf',
                                        (SPS_SOCKET, 'PenSelf'), 1, time.perf_counter())
            await asyncio.sleep(0)
            self.assertTrue(handler.is_active)
            await asyncio.sleep(0.58)
            self.assertFalse(handler.is_active)
            self.assertFalse(handler.socket_depth_available)
        finally:
            await handler.stop_background_jobs()

    async def test_stale_query_cannot_override_udp_hot_save_or_avatar_reset(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        path = '/avatar/parameters/OGB/Orf/TestSocket/PenSelf'
        binding = (SPS_SOCKET, 'PenSelf')
        try:
            before_udp = time.perf_counter()
            handler.osc_handler(path, binding, 0.2)
            handler.receive_query_value(path, binding, 1, before_udp)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.2)
            for timestamp in (time.perf_counter() - 1, time.perf_counter() + 10, float('nan')):
                handler.receive_query_value(path, binding, 1, timestamp)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.2)
            before_reset = time.perf_counter()
            await handler.reconfigure(copy.deepcopy(handler.shock_settings))
            handler.reset_for_avatar_change(observed_at=before_reset)
            handler.receive_query_value(path, binding, 1, before_reset)
            await asyncio.sleep(0)
            self.assertFalse(handler.is_active)
            handler.receive_query_value(path, binding, 0.4, time.perf_counter())
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.4)
            before_reset = time.perf_counter()
            handler.reset_for_avatar_change()
            handler.receive_query_value(path, binding, 1, before_reset)
            await asyncio.sleep(0)
            self.assertFalse(handler.is_active)
        finally:
            await handler.stop_background_jobs()

    async def test_queued_trigger_cannot_restore_strength_after_hot_disable(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A', device_kind='opossum')
        try:
            # Queue a positive input, then disable it before its async task runs.
            handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 1)
            config = copy.deepcopy(handler.shock_settings)
            config['lms_enabled'] = False
            await handler.reconfigure(config)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0)
            self.assertFalse(handler.is_active)
        finally:
            await handler.stop_background_jobs()

    async def test_all_source_switch_combinations_only_use_enabled_inputs(self):
        for kind in ('coyote', 'opossum'):
            for socket, plug, extra, pcs, lms in product((False, True), repeat=5):
                with self.subTest(device=kind, flags=(socket, plug, extra, pcs, lms)):
                    settings = make_settings()
                    config = settings['dglab3'][kind]['channel_a']
                    config.update(socket_enabled=socket, plug_enabled=plug,
                                  pcs_enabled=pcs, lms_enabled=lms)
                    config['extra_parameters']['enabled'] = extra
                    handler = ShockHandler(settings, FakeDGConnection(), 'A', device_kind=kind)
                    try:
                        handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 0.9)
                        handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 0.7)
                        handler.osc_handler('/avatar/parameters/Shock/TouchAreaA', ('extra', 'value'), 0.5)
                        await asyncio.sleep(0)
                        self.assertEqual(handler.current_strength_percentage,
                                         max(0.9 if pcs else 0, 0.7 if lms else 0, 0.5 if extra else 0))
                        handler.osc_handler('/avatar/parameters/OGB/Orf/TestSocket/PenSelf',
                                            (SPS_SOCKET, 'PenSelf'), 0.3)
                        handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf',
                                            (SPS_PLUG, 'PenSelf'), 0.4)
                        await asyncio.sleep(0)
                        expected = max(0.3 if socket else 0, 0.4 if plug else 0,
                                       0.9 if pcs else 0, 0.5 if extra else 0)
                        self.assertEqual(handler.current_strength_percentage, expected)
                        self.assertEqual(await handler.trigger_map_event(0.1), extra)
                        self.assertEqual(handler.current_strength_percentage, 1 if extra else expected)
                    finally:
                        await handler.stop_background_jobs()

    async def test_closed_sources_cannot_bypass_switches_via_extra_wildcard(self):
        settings = make_settings()
        settings['dglab3']['coyote']['channel_a'].update(
            socket_enabled=False, plug_enabled=False, pcs_enabled=False, lms_enabled=False,
        )
        handler = ShockHandler(settings, FakeDGConnection(), 'A')
        try:
            self.assertTrue(all(binding[0] == 'extra' for _path, binding in handler.osc_bindings()))
            for path in (PCS_PROXIMITY_ADDRESS, LMS_PROXIMITY_ADDRESS,
                         '/avatar/parameters/OGB/Orf/TestSocket/PenSelf',
                         '/avatar/parameters/OGB/Pen/Test/PenSelf'):
                handler.osc_handler(path, ('extra', 'value'), 1)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0)
            handler.osc_handler('/avatar/parameters/Shock/TouchAreaA', ('extra', 'value'), 0.6)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.6)
        finally:
            await handler.stop_background_jobs()

    async def test_disabled_plug_still_detects_attachment_for_enabled_lms(self):
        settings = make_settings()
        settings['dglab3']['opossum']['channel_a']['plug_enabled'] = False
        handler = ShockHandler(settings, FakeDGConnection(), 'A', device_kind='opossum')
        path = '/avatar/parameters/OGB/Pen/Test/PenSelf'
        try:
            self.assertIn(path, dict(handler.osc_bindings()))
            handler.osc_handler(path, (SPS_PLUG, 'PenSelf'), 0.4)
            handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 1)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0)
            handler.osc_handler(path, (SPS_PLUG, 'PenSelf'), 0)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 1)
        finally:
            await handler.stop_background_jobs()

    async def test_disabled_socket_does_not_suppress_enabled_pcs(self):
        settings = make_settings()
        settings['dglab3']['coyote']['channel_a']['socket_enabled'] = False
        handler = ShockHandler(settings, FakeDGConnection(), 'A')
        try:
            handler.osc_handler('/avatar/parameters/OGB/Orf/TestSocket/PenSelf', (SPS_SOCKET, 'PenSelf'), 0.4)
            handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 1)
            await asyncio.sleep(0)
            self.assertTrue(handler.socket_depth_available)
            self.assertEqual(handler.current_strength_percentage, 1)
        finally:
            await handler.stop_background_jobs()

    async def test_hot_disabling_every_source_clears_old_strength_and_bindings(self):
        connection = FakeDGConnection()
        handler = ShockHandler(make_settings(), connection, 'A', device_kind='opossum')
        try:
            handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 1)
            await asyncio.sleep(0)
            self.assertTrue(handler.is_active)
            config = copy.deepcopy(handler.shock_settings)
            config.update(socket_enabled=False, plug_enabled=False, pcs_enabled=False, lms_enabled=False)
            config['extra_parameters']['enabled'] = False
            await handler.reconfigure(config)
            self.assertEqual(handler.osc_bindings(), ())
            self.assertEqual(handler.current_strength_percentage, 0)
            self.assertFalse(handler.is_active)
            self.assertIn(('opossum', 'A'), connection.cleared)
            handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 1)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0)
        finally:
            await handler.stop_background_jobs()

    async def test_missing_socket_parameters_automatically_use_pcs_distance(self):
        for kind in ('coyote', 'opossum'):
            with self.subTest(device=kind):
                settings = make_settings()
                settings['dglab3'][kind]['channel_a']['pcs_enabled'] = True
                extra = settings['dglab3'][kind]['channel_a']['extra_parameters']
                extra.update({'enabled': False, 'bottom': 0.2, 'top': 0.8})
                handler = ShockHandler(settings, FakeDGConnection(), 'A', device_kind=kind)
                try:
                    self.assertIn(PCS_PROXIMITY_ADDRESS, dict(handler.osc_bindings()))
                    handler.set_avatar_parameters((PCS_PROXIMITY_ADDRESS,))
                    handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 0.5)
                    await asyncio.sleep(0)
                    self.assertFalse(handler.socket_depth_available)
                    self.assertAlmostEqual(handler.current_strength_percentage, 0.5)
                    self.assertEqual(handler.active_source, SOURCE_PCS)
                    self.assertEqual(handler.last_parameter, PCS_PROXIMITY_ADDRESS)
                finally:
                    await handler.stop_background_jobs()

    async def test_declared_and_live_socket_values_never_disable_pcs(self):
        base = '/avatar/parameters/OGB/Orf/TestSocket/'
        for signals in (('PenSelf',), ('PenOthersNewRoot', 'PenOthersNewTip')):
            with self.subTest(signals=signals):
                handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
                try:
                    handler.set_avatar_parameters(tuple(base + signal for signal in signals))
                    handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 1)
                    await asyncio.sleep(0)
                    self.assertFalse(handler.socket_depth_available)
                    self.assertEqual(handler.current_strength_percentage, 1)
                    for signal in signals:
                        handler.osc_handler(base + signal, (SPS_SOCKET, signal), 0)
                    await asyncio.sleep(0)
                    self.assertTrue(handler.socket_depth_available)
                    self.assertEqual(handler.current_strength_percentage, 1)
                    self.assertTrue(handler.is_active)
                finally:
                    await handler.stop_background_jobs()

    async def test_zero_valued_live_root_tip_pair_is_also_valid_socket_evidence(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        base = '/avatar/parameters/OGB/Orf/TestSocket/'
        try:
            handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 0.8)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.8)
            handler.osc_handler(base + 'PenSelfNewRoot', (SPS_SOCKET, 'PenSelfNewRoot'), 0)
            await asyncio.sleep(0)
            self.assertFalse(handler.socket_depth_available)
            handler.osc_handler(base + 'PenSelfNewTip', (SPS_SOCKET, 'PenSelfNewTip'), 0)
            await asyncio.sleep(0)
            self.assertTrue(handler.socket_depth_available)
            self.assertEqual(handler.current_strength_percentage, 0.8)
        finally:
            await handler.stop_background_jobs()

    async def test_pcs_extra_binding_uses_independent_pcs_source(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        try:
            handler.set_avatar_parameters(('/avatar/parameters/OGB/Orf/TestSocket/PenSelf',))
            handler.osc_handler('/avatar/parameters/OGB/Orf/TestSocket/PenSelf', (SPS_SOCKET, 'PenSelf'), 0)
            handler.osc_handler(PCS_PROXIMITY_ADDRESS, ('extra', 'value'), 1)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 1)
            self.assertEqual(handler.active_source, SOURCE_PCS)
            self.assertNotIn(PCS_PROXIMITY_ADDRESS, handler.extra_values)
        finally:
            await handler.stop_background_jobs()

    async def test_pcs_competes_with_live_socket_for_both_devices_and_channels(self):
        for kind, channel in product(('coyote', 'opossum'), ('A', 'B')):
            with self.subTest(device=kind, channel=channel):
                settings = make_settings()
                config = copy.deepcopy(settings['dglab3'][kind]['channel_a'])
                config.update(pcs_enabled=True, lms_enabled=False)
                settings['dglab3'][kind][f'channel_{channel.lower()}'] = config
                handler = ShockHandler(settings, FakeDGConnection(), channel, device_kind=kind)
                try:
                    handler.osc_handler('/avatar/parameters/OGB/Orf/TestSocket/PenSelf',
                                        (SPS_SOCKET, 'PenSelf'), 0.4)
                    for pcs, expected, source in ((0.8, 0.8, SOURCE_PCS), (0.2, 0.4, SPS_SOCKET)):
                        handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), pcs)
                        await asyncio.sleep(0)
                        self.assertTrue(handler.socket_depth_available)
                        self.assertEqual(handler.current_strength_percentage, expected)
                        self.assertEqual(handler.active_source, source)
                    handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 0.9)
                    self.assertEqual(handler._winner(now=handler.pcs_value[1] + 0.01)[0], 0.4)
                    updated = copy.deepcopy(handler.shock_settings)
                    updated['pcs_enabled'] = False
                    await handler.hot_update(updated)
                    handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 1)
                    await asyncio.sleep(0)
                    self.assertEqual(handler.current_strength_percentage, 0.4)
                    self.assertEqual(handler.active_source, SPS_SOCKET)
                finally:
                    await handler.stop_background_jobs()

    async def test_pcs_timeout_clears_output_without_restarting_old_value(self):
        connection = FakeDGConnection()
        handler = ShockHandler(make_settings(), connection, 'A')
        handler.start_background_jobs()
        try:
            handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 0.8)
            await asyncio.sleep(0.12)
            self.assertTrue(connection.waves)
            await asyncio.sleep(0.46)
            self.assertFalse(handler.is_active)
            self.assertIsNone(handler.pcs_value)
            count = len(connection.waves)
            await asyncio.sleep(0.12)
            self.assertEqual(len(connection.waves), count)
        finally:
            await handler.stop_background_jobs()

    async def test_pcs_availability_rechecks_selected_socket_on_hot_update(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        try:
            paths = ('/avatar/parameters/OGB/Orf/TestSocket/PenSelf',)
            handler.set_avatar_parameters(paths)
            handler.osc_handler(paths[0], (SPS_SOCKET, 'PenSelf'), 0)
            await asyncio.sleep(0)
            self.assertTrue(handler.socket_depth_available)
            config = copy.deepcopy(handler.shock_settings)
            config['socket_zone'] = 'Missing'
            await handler.reconfigure(config)
            self.assertFalse(handler.socket_depth_available)
            handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 0.8)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.8)
            handler.reset_for_avatar_change()
            await asyncio.sleep(0)
            self.assertIsNone(handler.pcs_value)
            self.assertIsNone(handler.avatar_parameter_paths)
        finally:
            await handler.stop_background_jobs()

    async def test_pcs_and_lms_fallbacks_keep_plug_extra_and_map_rules(self):
        settings = make_settings()
        settings['dglab3']['opossum']['channel_a']['pcs_enabled'] = True
        handler = ShockHandler(settings, FakeDGConnection(), 'A', device_kind='opossum')
        try:
            handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 0.6)
            handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 0.4)
            await asyncio.sleep(0)
            self.assertEqual(handler.active_source, SOURCE_PCS)
            handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf', (SPS_PLUG, 'PenSelf'), 0.2)
            handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 1)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.6)
            handler.osc_handler('/avatar/parameters/Shock/TouchAreaA', ('extra', 'value'), 0.8)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.8)
            await handler.trigger_map_event(1)
            self.assertEqual(handler.active_source, 'map')
        finally:
            await handler.stop_background_jobs()

    async def test_parameter_refresh_removes_stale_socket_and_pcs_values(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        socket = '/avatar/parameters/OGB/Orf/TestSocket/PenSelf'
        try:
            handler.set_avatar_parameters((socket,))
            handler.osc_handler(socket, (SPS_SOCKET, 'PenSelf'), 0.7)
            handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 0.8)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.8)
            handler.set_avatar_parameters((PCS_PROXIMITY_ADDRESS,))
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0)
            self.assertFalse(handler.socket_depth_available)
            handler.osc_handler(PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value'), 0.8)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.8)
        finally:
            await handler.stop_background_jobs()

    async def test_lms_binding_follows_explicit_fixture_opt_in(self):
        for kind in ('coyote', 'opossum'):
            handler = ShockHandler(make_settings(), FakeDGConnection(), 'A', device_kind=kind)
            self.assertEqual(LMS_PROXIMITY_ADDRESS in dict(handler.osc_bindings()), kind == 'opossum')

    async def test_lms_distance_uses_channel_range_even_without_extra_switch(self):
        settings = make_settings()
        extra = settings['dglab3']['opossum']['channel_a']['extra_parameters']
        extra.update({'enabled': False, 'bottom': 0.2, 'top': 0.8})
        handler = ShockHandler(settings, FakeDGConnection(), 'A', device_kind='opossum')
        try:
            for raw, expected in ((0.1, 0), (0.5, 0.5), (0.9, 1)):
                handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), raw)
                await asyncio.sleep(0)
                self.assertAlmostEqual(handler.current_strength_percentage, expected)
            self.assertEqual(handler.active_source, SOURCE_LMS)
            self.assertEqual(handler.last_parameter, LMS_PROXIMITY_ADDRESS)
            self.assertEqual(handler.last_raw_value, 0.9)
        finally:
            await handler.stop_background_jobs()

    async def test_positive_self_or_other_plug_blocks_lms_until_withdrawal(self):
        for signal in ('PenSelf', 'PenOthers'):
            with self.subTest(signal=signal):
                handler = ShockHandler(make_settings(), FakeDGConnection(), 'A', device_kind='opossum')
                path = f'/avatar/parameters/OGB/Pen/Test/{signal}'
                try:
                    handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 0.9)
                    await asyncio.sleep(0)
                    self.assertEqual(handler.current_strength_percentage, 0.9)
                    handler.osc_handler(path, (SPS_PLUG, signal), 0.1)
                    handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 1)
                    await asyncio.sleep(0)
                    self.assertEqual(handler.current_strength_percentage, 0.1)
                    self.assertEqual(handler.active_source, SPS_PLUG)
                    handler.osc_handler(path, (SPS_PLUG, signal), 0)
                    await asyncio.sleep(0)
                    self.assertEqual(handler.current_strength_percentage, 1)
                    self.assertEqual(handler.active_source, SOURCE_LMS)
                finally:
                    await handler.stop_background_jobs()

    async def test_lms_cannot_bypass_gate_through_extra_parameter_binding(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A', device_kind='opossum')
        try:
            handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf', (SPS_PLUG, 'PenSelf'), 0.2)
            handler.osc_handler(LMS_PROXIMITY_ADDRESS, ('extra', 'value'), 1)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.2)
            self.assertNotIn(LMS_PROXIMITY_ADDRESS, handler.extra_values)
        finally:
            await handler.stop_background_jobs()

    async def test_any_selected_plug_with_positive_depth_keeps_lms_blocked(self):
        settings = make_settings()
        settings['dglab3']['opossum']['channel_a']['plug_zone'] = '*'
        handler = ShockHandler(settings, FakeDGConnection(), 'A', device_kind='opossum')
        try:
            handler.osc_handler('/avatar/parameters/OGB/Pen/First/PenSelf', (SPS_PLUG, 'PenSelf'), 0.2)
            handler.osc_handler('/avatar/parameters/OGB/Pen/Second/PenOthers', (SPS_PLUG, 'PenOthers'), 0)
            handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 1)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.2)
        finally:
            await handler.stop_background_jobs()

    async def test_lms_keeps_socket_extra_and_map_maximum_logic(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A', device_kind='opossum')
        try:
            handler.osc_handler('/avatar/parameters/OGB/Orf/TestSocket/PenSelf', (SPS_SOCKET, 'PenSelf'), 0.7)
            handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 0.4)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.7)
            handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf', (SPS_PLUG, 'PenSelf'), 0.1)
            handler.osc_handler('/avatar/parameters/Shock/TouchAreaA', ('extra', 'value'), 0.8)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0.8)
            await handler.trigger_map_event(0.2)
            self.assertEqual(handler.current_strength_percentage, 1)
            self.assertEqual(handler.active_source, 'map')
        finally:
            await handler.stop_background_jobs()

    async def test_lms_timeout_stops_output_and_does_not_replay_after_withdrawal(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A', device_kind='opossum')
        handler.start_background_jobs()
        try:
            handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 0.8)
            await asyncio.sleep(0.12)
            self.assertTrue(handler.is_active)
            await asyncio.sleep(0.46)
            self.assertFalse(handler.is_active)
            self.assertEqual(handler.current_strength_percentage, 0)
            handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf', (SPS_PLUG, 'PenSelf'), 0.2)
            await asyncio.sleep(0)
            handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf', (SPS_PLUG, 'PenSelf'), 0)
            await asyncio.sleep(0)
            self.assertEqual(handler.current_strength_percentage, 0)
        finally:
            await handler.stop_background_jobs()

    async def test_avatar_change_and_hot_settings_discard_lms_values(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A', device_kind='opossum')
        try:
            for action in ('avatar', 'settings'):
                handler.osc_handler(LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value'), 0.8)
                await asyncio.sleep(0)
                if action == 'avatar':
                    handler.reset_for_avatar_change()
                    await asyncio.sleep(0)
                else:
                    await handler.reconfigure(copy.deepcopy(handler.shock_settings))
                self.assertEqual(handler._winner()[0], 0)
                self.assertEqual(handler.current_strength_percentage, 0)
        finally:
            await handler.stop_background_jobs()

    async def test_self_socket_and_plug_drive_both_devices_and_channels(self):
        for device_kind in ('coyote', 'opossum'):
            for channel in ('A', 'B'):
                with self.subTest(device=device_kind, channel=channel):
                    settings = make_settings()
                    settings['dglab3'][device_kind]['channel_b'] = copy.deepcopy(
                        settings['dglab3'][device_kind]['channel_a'],
                    )
                    connection = FakeDGConnection()
                    handler = ShockHandler(settings, connection, channel, device_kind=device_kind)
                    bindings = dict(handler.osc_bindings())
                    socket = '/avatar/parameters/OGB/Orf/TestSocket/'
                    plug = '/avatar/parameters/OGB/Pen/Test/'

                    async def send(path, value):
                        handler.osc_handler(path, bindings[path], value)
                        await asyncio.sleep(0)

                    try:
                        for _ in range(4):
                            await send(socket + 'PenSelfNewRoot', 0.2)
                            await send(socket + 'PenSelfNewTip', 0.6)
                        await send(socket + 'PenSelfNewRoot', 0.8)
                        await send(socket + 'PenSelfNewTip', 1.0)
                        self.assertAlmostEqual(handler.current_strength_percentage, 0.5)
                        self.assertEqual(handler.active_source, SPS_SOCKET)

                        feeder = handler._track_task(handler.depth_background_wave_feeder())
                        await asyncio.sleep(0.12)
                        self.assertTrue(connection.waves)
                        self.assertEqual(connection.waves[-1][:2], (device_kind, channel))
                        self.assertEqual(connection.waves[-1][2][0],
                                         scale_coyote_frame(handler.wave_frames[0], 0.5))
                        feeder.cancel()
                        await asyncio.gather(feeder, return_exceptions=True)

                        await send(plug + 'PenSelf', 0.7)
                        self.assertEqual(handler.current_strength_percentage, 0.7)
                        self.assertEqual(handler.active_source, SPS_PLUG)
                        await send(plug + 'PenOthers', 0.9)
                        self.assertEqual(handler.current_strength_percentage, 0.9)
                        await send(plug + 'PenOthers', 0)
                        self.assertEqual(handler.current_strength_percentage, 0.7)
                        await send(plug + 'PenSelf', 0)
                        self.assertAlmostEqual(handler.current_strength_percentage, 0.5)
                        await send(socket + 'PenSelfNewRoot', 0)
                        await send(socket + 'PenSelfNewTip', 0)
                        self.assertEqual(handler.current_strength_percentage, 0)
                        self.assertFalse(handler.is_active)
                    finally:
                        await handler.stop_background_jobs()

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

    async def test_pure_feeder_sends_current_ratio_without_modulation(self):
        connection = FakeDGConnection()
        settings = make_settings()
        settings['dglab3']['coyote']['channel_a']['depth']['waveform'] = '纯粹'
        handler = ShockHandler(settings, connection, 'A')
        handler.depth_update_time_window = 0.01
        task = asyncio.create_task(handler.depth_background_wave_feeder())
        try:
            await asyncio.sleep(0.03)
            self.assertFalse(connection.waves)  # No trigger never arms output.
            for ratio, expected in ((0.25, 25), (0.8, 80), (1, 100), (0, 0)):
                start = len(connection.waves)
                await handler.handler_depth(ratio)
                for _ in range(30):
                    if len(connection.waves) > start:
                        break
                    await asyncio.sleep(0.01)
                self.assertGreater(len(connection.waves), start)
                self.assertEqual(frame_amplitudes(connection.waves[-1][2][0]), (expected,) * 4)
            await handler._clear_output()
            self.assertEqual(handler.last_sent_strengths, (0, 0, 0, 0))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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

    async def test_shared_switch_blocks_extra_and_map_but_keeps_sps(self):
        settings = make_settings()
        settings['dglab3']['opossum']['channel_a']['extra_parameters']['enabled'] = False
        handler = ShockHandler(settings, FakeDGConnection(), 'A', device_kind='opossum')
        self.assertFalse(await handler.trigger_map_event(1))
        handler.osc_handler('/avatar/parameters/Shock/TouchAreaA', ('extra', 'value'), 1)
        await asyncio.sleep(0)
        self.assertEqual(handler.current_strength_percentage, 0)
        self.assertFalse(any(binding[1][0] == 'extra' for binding in handler.osc_bindings()))
        handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenOthers', (SPS_PLUG, 'PenOthers'), 0.4)
        await asyncio.sleep(0)
        self.assertEqual(handler.current_strength_percentage, 0.4)

    async def test_map_event_survives_extra_expiry_then_resumes_sps(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenOthers', (SPS_PLUG, 'PenOthers'), 0.4)
        handler.osc_handler('/avatar/parameters/Shock/TouchAreaA', ('extra', 'value'), 0.8)
        await asyncio.sleep(0)
        await handler.trigger_map_event(0.8)
        task = asyncio.create_task(handler.clear_check())
        try:
            await asyncio.sleep(0.6)
            self.assertEqual(handler.current_strength_percentage, 1)
            self.assertEqual(handler.active_source, 'map')
            await asyncio.sleep(0.3)
            self.assertEqual(handler.current_strength_percentage, 0.4)
            self.assertEqual(handler.active_source, SPS_PLUG)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_expired_map_event_clears_opossum_without_restarting_it(self):
        connection = FakeDGConnection()
        handler = ShockHandler(make_settings(), connection, 'A', device_kind='opossum')
        handler.start_background_jobs()
        try:
            self.assertTrue(await handler.trigger_map_event(0.25))
            await asyncio.sleep(0.4)
            self.assertTrue(connection.waves)
            self.assertIn(('opossum', 'A'), connection.cleared)
            self.assertFalse(handler.is_active)
            wave_count = len(connection.waves)
            await asyncio.sleep(0.2)
            self.assertEqual(len(connection.waves), wave_count)
            self.assertEqual(handler.last_sent_strengths, (0, 0, 0, 0))
        finally:
            await handler.stop_background_jobs()

    async def test_hot_disabling_shared_switch_cancels_active_map_event(self):
        connection = FakeDGConnection()
        handler = ShockHandler(make_settings(), connection, 'A', device_kind='opossum')
        await handler.trigger_map_event(5)
        config = copy.deepcopy(handler.shock_settings)
        config['extra_parameters']['enabled'] = False
        await handler.reconfigure(config)
        self.assertFalse(handler.is_active)
        self.assertEqual(handler.map_event_until, 0)
        self.assertEqual(connection.cleared, [('opossum', 'A')])
        self.assertFalse(await handler.trigger_map_event(1))


class SPSDepthTests(unittest.TestCase):
    def test_avatar_lists_only_complete_numeric_depth_zones(self):
        cases = (
            ('Orf', 'TouchOnly', 'TouchSelf', 'Float'),
            ('Orf', 'RootOnly', 'PenSelfNewRoot', 'Float'),
            ('Orf', 'MixedOwners', 'PenSelfNewRoot', 'Float'),
            ('Orf', 'MixedOwners', 'PenOthersNewTip', 'Float'),
            ('Orf', 'Complete', 'PenSelfNewRoot', 'Float'),
            ('Orf', 'Complete', 'PenSelfNewTip', 'Float'),
            ('Orf', 'Direct', 'PenOthers', 'Float'),
            ('Pen', 'FrotOnly', 'FrotOthers', 'Float'),
            ('Pen', 'StringDepth', 'PenSelf', 'String'),
            ('Pen', 'Readable', 'PenSelf', 'Float'),
        )
        parameters = [{'name': f'OGB/{kind}/{zone}/{signal}', 'output': {
            'address': f'/avatar/parameters/OGB/{kind}/{zone}/{signal}', 'type': type_,
        }} for kind, zone, signal, type_ in cases]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'avtr_test.json'
            path.write_text(json.dumps({'parameters': parameters}), encoding='utf-8')
            avatar = load_avatar_sps_config(path)
        self.assertEqual([zone.zone_id for zone in avatar.sockets], ['Complete', 'Direct'])
        self.assertEqual([zone.zone_id for zone in avatar.plugs], ['Readable'])

    def test_valid_socket_depth_requires_a_direct_depth_or_matching_pair(self):
        base = '/avatar/parameters/OGB/Orf/'
        for signals, valid in (
            ([], False),
            (['One/TouchSelf', 'One/FrotOthers'], False),
            (['One/PenSelfNewRoot'], False),
            (['One/PenSelfNewRoot', 'Two/PenSelfNewTip'], False),
            (['One/PenSelfNewRoot', 'One/PenOthersNewTip'], False),
            (['One/PenSelfNewRoot', 'One/PenSelfNewTip'], True),
            (['One/PenOthers'], True),
        ):
            with self.subTest(signals=signals):
                self.assertEqual(has_socket_depth_parameters([base + signal for signal in signals]), valid)
        self.assertFalse(has_socket_depth_parameters([base + 'One/PenSelf'], 'Two'))

    def test_plug_self_only_tracks_depth_and_withdrawal(self):
        calculator = SPSDepthCalculator(SPS_PLUG)
        self.assertEqual(calculator.update('PenSelf', 0.6), 0.6)
        self.assertEqual(calculator.update('PenSelf', 0), 0)

    def test_socket_self_root_tip_uses_the_same_depth_formula(self):
        calculator = SPSDepthCalculator(SPS_SOCKET)
        for _ in range(4):
            calculator.update('PenSelfNewRoot', 0.2)
            self.assertEqual(calculator.update('PenSelfNewTip', 0.6), 0)
        calculator.update('PenSelfNewRoot', 0.8)
        self.assertAlmostEqual(calculator.update('PenSelfNewTip', 1), 0.5)
        calculator.update('PenSelfNewRoot', 0)
        self.assertEqual(calculator.update('PenSelfNewTip', 0), 0)
        self.assertIsNone(calculator.detectors['Self'].length)

    def test_socket_self_and_others_keep_separate_length_estimates(self):
        calculator = SPSDepthCalculator(SPS_SOCKET)
        for owner, root, tip in (('Self', 0.2, 0.6), ('Others', 0.1, 0.9)):
            for _ in range(4):
                calculator.update(f'Pen{owner}NewRoot', root)
                calculator.update(f'Pen{owner}NewTip', tip)
        self.assertAlmostEqual(calculator.detectors['Self'].length, 0.4)
        self.assertAlmostEqual(calculator.detectors['Others'].length, 0.8)
        calculator.update('PenSelfNewRoot', 0.8)
        calculator.update('PenSelfNewTip', 1)
        calculator.update('PenOthersNewRoot', 0.5)
        self.assertAlmostEqual(calculator.update('PenOthersNewTip', 1), 0.5)
        calculator.update('PenSelfNewRoot', 0)
        self.assertAlmostEqual(calculator.update('PenSelfNewTip', 0), 0.375)

    def test_legacy_socket_self_signal_is_supported(self):
        calculator = SPSDepthCalculator(SPS_SOCKET)
        self.assertEqual(calculator.update('PenSelf', 0.6), 0.6)
        self.assertEqual(calculator.update('PenSelf', 0), 0)

    def test_avatar_json_exposes_socket_and_plug_dropdown_options(self):
        document = {
            'id': 'avtr_test',
            'name': 'Test Avatar',
            'parameters': [
                {
                    'name': 'OGB/Orf/Test Socket/PenOthersNewTip',
                    'output': {'address': '/avatar/parameters/OGB/Orf/Test_Socket/PenOthersNewTip', 'type': 'Float'},
                },
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
    def test_pure_coyote_waveform_has_no_time_envelope(self):
        self.assertEqual(srv.WAVEFORM_NAMES_BY_DEVICE['coyote'][0], '纯粹')
        frames = load_waveform_frames('纯粹', 'coyote')
        self.assertEqual(frames, ('0A0A0A0A64646464',))
        for depth, expected in ((0, 0), (0.25, 25), (0.7, 70), (1, 100)):
            self.assertEqual(frame_amplitudes(scale_coyote_frame(frames[0], depth)), (expected,) * 4)
        self.assertNotIn('纯粹', srv.WAVEFORM_NAMES_BY_DEVICE['opossum'])
        self.assertIn('纯粹', srv.WAVEFORMS)

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

    def test_local_vrchat_map_trigger_is_allowed_without_control_token(self):
        shocking_vrchat.SETTINGS['api']['control_enabled'] = False
        with patch(
            'shocking_vrchat.submit_to_async_loop',
            side_effect=lambda coroutine: coroutine.close(),
        ) as submit:
            response = shocking_vrchat.app.test_client().get(
                '/api/v1/shock/A/1',
                headers={'User-Agent': 'UnityPlayer/test'},
            )
        self.assertEqual(response.status_code, 200)
        submit.assert_called_once()

    def test_non_vrchat_map_trigger_is_rejected_without_control_token(self):
        shocking_vrchat.SETTINGS['api']['control_enabled'] = False
        response = shocking_vrchat.app.test_client().get('/api/v1/shock/A/1')
        self.assertEqual(response.status_code, 401)

    def test_public_vrchat_map_trigger_is_rejected_without_control_token(self):
        shocking_vrchat.SETTINGS['api']['control_enabled'] = False
        response = shocking_vrchat.app.test_client().get(
            '/api/v1/shock/A/1',
            headers={'User-Agent': 'UnityPlayer/test'},
            environ_base={'REMOTE_ADDR': '8.8.8.8'},
        )
        self.assertEqual(response.status_code, 401)

    def test_raw_wave_api_remains_disabled_by_default(self):
        shocking_vrchat.SETTINGS['api']['control_enabled'] = False
        response = shocking_vrchat.app.test_client().get(
            '/api/v1/sendwave/A/1/0A0A0A0A64646464',
            headers={'User-Agent': 'UnityPlayer/test'},
        )
        self.assertEqual(response.status_code, 401)

    def test_raw_wave_broadcast_remains_coyote_only(self):
        calls = []

        async def record_wave(**kwargs):
            calls.append(kwargs)

        with patch.object(shocking_vrchat.DGConnection, 'broadcast_wave', new=record_wave):
            asyncio.run(shocking_vrchat.broadcast_repeated_wave(
                ['A', 'B'], 2, '0A0A0A0A64646464',
            ))
        self.assertEqual([item['channel'] for item in calls], ['A', 'B'])
        self.assertTrue(all(item['device_kind'] == 'coyote' for item in calls))
        self.assertTrue(all(len(json.loads(item['wavestr'])) == 2 for item in calls))

    def test_map_request_respects_each_device_channel_switch(self):
        settings = shocking_vrchat.apply_basic_settings(
            copy.deepcopy(shocking_vrchat.DEFAULT_SETTINGS),
            copy.deepcopy(shocking_vrchat.DEFAULT_BASIC_SETTINGS),
        )
        handlers = [
            ShockHandler(settings, FakeDGConnection(), channel, device_kind=kind)
            for kind in ('coyote', 'opossum') for channel in ('A', 'B')
        ]
        controller = SimpleNamespace(runtime=SimpleNamespace(handlers=handlers))
        for switches in ((True, True, True, True), (True, False, False, True), (False,) * 4):
            for channels in (('A',), ('B',), ('A', 'B')):
                with self.subTest(switches=switches, channels=channels):
                    for handler, enabled in zip(handlers, switches):
                        handler.extra_config['enabled'] = enabled
                        handler.map_event_until = 0
                        handler.depth_current_strength = 0
                    with patch('shocking_vrchat.ACTIVE_CONTROLLER', controller):
                        asyncio.run(shocking_vrchat.trigger_map_event(channels, 1))
                    self.assertEqual(
                        [handler.depth_current_strength for handler in handlers],
                        [float(enabled and handler.channel in channels)
                         for handler, enabled in zip(handlers, switches)],
                    )

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

    def test_disconnected_chatbox_status_is_empty(self):
        manager = AdvancedChatboxManager({'chatbox': {'enable': False}})
        self.assertEqual(manager.format_device_status([]), '')
        self.assertEqual(manager.format_detailed_status([]), '')

    def test_multi_device_status_contains_only_device_lines(self):
        manager = AdvancedChatboxManager({'chatbox': {'enable': False}})
        connection = SimpleNamespace(get_device_states=lambda: [
            {
                'device_kind': 'coyote',
                'upper_strength': {'A': 80, 'B': 70},
            },
            {
                'device_kind': 'opossum',
                'upper_strength': {'A': 60, 'B': 50},
            },
        ])

        self.assertEqual(
            manager.format_device_status([connection]),
            '郊狼 MAX A:80 B:70\n负鼠 MAX A:60 B:50',
        )
        self.assertEqual(
            manager.format_detailed_status([connection]),
            '郊狼 MAX A:80 B:70\n负鼠 MAX A:60 B:50',
        )

    def test_connected_app_without_devices_is_empty(self):
        manager = AdvancedChatboxManager({'chatbox': {'enable': False}})
        connection = SimpleNamespace(get_device_states=lambda: [])
        self.assertEqual(manager.format_device_status([connection]), '')
        self.assertEqual(manager.format_detailed_status([connection]), '')

    def test_unbound_legacy_connection_is_not_a_connected_device(self):
        manager = AdvancedChatboxManager({'chatbox': {'enable': False}})
        connection = SimpleNamespace(bound=False, get_upper_strength=lambda _: 100)
        self.assertEqual(manager.format_device_status([connection]), '')
        self.assertEqual(manager.format_detailed_status([connection]), '')

    def test_legacy_coyote_uses_effective_upper_limits(self):
        manager = AdvancedChatboxManager({'chatbox': {'enable': False}})
        connection = SimpleNamespace(
            bound=True, uuid='legacy', strength={'A': 10, 'B': 20},
            get_upper_strength=lambda channel: {'A': 70, 'B': 60}[channel],
        )
        self.assertEqual(manager.format_device_status([connection]), '郊狼 MAX A:70 B:60')
        self.assertIn('A: 10/70', manager.format_detailed_status([connection]))


class ChatboxUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock()
        with patch('srv.advanced_chatbox_manager.SimpleUDPClient', return_value=self.client):
            self.manager = AdvancedChatboxManager({'chatbox': {
                'enable': True, 'set_avatar_parameter': False, 'update_interval': 3.0,
            }})
        self.addCleanup(self.manager.cleanup, notify=False)
        self.states = [
            {'slot_id': 'coyote', 'device_kind': 'coyote', 'upper_strength': {'A': 80, 'B': 70}},
            {'slot_id': 'opossum', 'device_kind': 'opossum', 'upper_strength': {'A': 60, 'B': 50}},
        ]

    async def test_waiting_for_connection_sends_nothing(self):
        await self.manager.update_chatbox([])
        await self.manager.update_chatbox([SimpleNamespace(get_device_states=lambda: [])])
        await self.manager.update_chatbox([], detailed=True)
        self.manager.cleanup()
        self.client.send_message.assert_not_called()

    async def test_disconnect_clears_once_without_waiting_for_interval(self):
        with patch('srv.advanced_chatbox_manager.time.monotonic', return_value=100):
            await self.manager.update_chatbox([], device_states=self.states)
        with patch('srv.advanced_chatbox_manager.time.monotonic', return_value=100.1):
            await self.manager.update_chatbox([])
            await self.manager.update_chatbox([])
        self.assertEqual(self.client.send_message.call_count, 2)
        self.client.send_message.assert_called_with('/chatbox/input', ['', True, False])

    async def test_reconnect_updates_immediately(self):
        with patch('srv.advanced_chatbox_manager.time.monotonic', return_value=100):
            await self.manager.update_chatbox([], device_states=self.states)
            await self.manager.update_chatbox([])
            await self.manager.update_chatbox([], device_states=self.states)
        self.assertEqual(self.client.send_message.call_count, 3)
        self.client.send_message.assert_called_with(
            '/chatbox/input', ['郊狼 MAX A:80 B:70\n负鼠 MAX A:60 B:50', True, False],
        )

    async def test_one_device_disconnects_removes_only_its_line_immediately(self):
        with patch('srv.advanced_chatbox_manager.time.monotonic', return_value=100):
            await self.manager.update_chatbox([], device_states=self.states)
            await self.manager.update_chatbox([], device_states=self.states[1:])
        self.assertEqual(self.client.send_message.call_count, 2)
        self.client.send_message.assert_called_with('/chatbox/input', ['负鼠 MAX A:60 B:50', True, False])

    async def test_connected_updates_still_respect_refresh_interval(self):
        with patch('srv.advanced_chatbox_manager.time.monotonic', return_value=100):
            await self.manager.update_chatbox([], device_states=self.states)
        with patch('srv.advanced_chatbox_manager.time.monotonic', return_value=102.9):
            await self.manager.update_chatbox([], device_states=self.states)
        self.assertEqual(self.client.send_message.call_count, 1)
        with patch('srv.advanced_chatbox_manager.time.monotonic', return_value=103):
            await self.manager.update_chatbox([], device_states=self.states)
        self.assertEqual(self.client.send_message.call_count, 2)

    async def test_detailed_multi_device_output_is_only_limits(self):
        await self.manager.update_chatbox([], detailed=True, device_states=iter(self.states))
        self.client.send_message.assert_called_once_with(
            '/chatbox/input', ['郊狼 MAX A:80 B:70\n负鼠 MAX A:60 B:50', True, False],
        )

    async def test_cleanup_clears_existing_display_and_closes_socket(self):
        await self.manager.update_chatbox([], device_states=self.states)
        self.manager.cleanup()
        self.client.send_message.assert_called_with('/chatbox/input', ['', True, False])
        self.client._sock.close.assert_called_once()

    async def test_failed_clear_is_retried(self):
        await self.manager.update_chatbox([], device_states=self.states)
        self.client.send_message.side_effect = OSError('send failed')
        await self.manager.update_chatbox([])
        self.assertTrue(self.manager.last_status_text)
        self.client.send_message.side_effect = None
        await self.manager.update_chatbox([])
        self.assertEqual(self.manager.last_status_text, '')

    async def test_disabled_chatbox_never_sends(self):
        self.manager.enabled = False
        await self.manager.update_chatbox([], device_states=self.states)
        self.client.send_message.assert_not_called()
        self.manager.enabled = True


if __name__ == '__main__':
    unittest.main()
