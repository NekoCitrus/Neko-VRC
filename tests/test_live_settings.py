import asyncio
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from loguru import logger
import shocking_vrchat as core
from srv.config_manager import DEFAULT_SETTINGS, DEFAULT_BASIC_SETTINGS, apply_basic_settings
from srv.sps_depth import SPS_PLUG, SPS_SOCKET, SPSDepthCalculator
from srv.oscquery import VRChatOSCQueryClient, OSCQueryAvatarSnapshot


class LiveSettingsTests(unittest.IsolatedAsyncioTestCase):
    def make_runtime(self):
        settings = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS))
        settings['chatbox']['enable'] = False
        runtime = core.DeviceRuntime(settings)
        runtime.dispatcher = runtime._build_dispatcher()
        self.addCleanup(runtime.chatbox_manager.cleanup, notify=False)
        async def cleanup():
            await asyncio.gather(*(h.stop_background_jobs() for h in runtime.handlers))
        self.addAsyncCleanup(cleanup)
        return runtime

    async def test_cap_and_waveform_edits_preserve_inputs_and_other_channels(self):
        runtime = self.make_runtime()
        first, second = runtime.handlers[:2]
        for handler, value in ((first, 0.6), (second, 0.3)):
            handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf', (SPS_PLUG, 'PenSelf'), value)
        await asyncio.sleep(0)
        runtime.router.broadcast_clear_wave = AsyncMock()
        for cap in range(20, 40):
            updated = copy.deepcopy(runtime.settings)
            updated['dglab3']['coyote']['channel_a']['strength_limit'] = cap
            updated['dglab3']['coyote']['channel_a']['depth']['waveform'] = '气泡'
            await runtime._apply_settings(updated)
        self.assertEqual(first.current_strength_percentage, 0.6)
        self.assertEqual(second.current_strength_percentage, 0.3)
        self.assertEqual(first.waveform, '气泡')
        runtime.router.broadcast_clear_wave.assert_not_awaited()
        for lms in (True, False):
            updated = copy.deepcopy(runtime.settings)
            updated['dglab3']['coyote']['channel_a']['lms_enabled'] = lms
            await runtime._apply_settings(updated)
            self.assertEqual(first.current_strength_percentage, 0.6)
            runtime.router.broadcast_clear_wave.assert_not_awaited()
        updated = copy.deepcopy(runtime.settings)
        updated['dglab3']['coyote']['channel_a']['plug_enabled'] = False
        await runtime._apply_settings(updated)
        self.assertEqual(first.current_strength_percentage, 0)
        self.assertEqual(second.current_strength_percentage, 0.3)
        runtime.router.broadcast_clear_wave.assert_awaited_once_with('A', device_kind='coyote')

    async def test_computed_strength_uses_ratio_and_each_host_cap_not_samples(self):
        runtime = self.make_runtime()
        handler = runtime.handlers[0]
        handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf', (SPS_PLUG, 'PenSelf'), 0.6)
        await asyncio.sleep(0)
        runtime.settings['connections']['coyote']['mode'] = 'app'
        runtime.router.device_states = lambda: tuple({
            'slot_id': slot, 'device_kind': 'coyote', 'device_name': slot,
            'strength': {'A': 200}, 'upper_strength': {'A': cap},
        } for slot, cap in (('first', 70), ('second', 40)))
        handler.last_sent_strengths = (100, 0, 100, 0)
        info = runtime.snapshot()['device_channels']['coyote']['A']
        self.assertEqual(info['calculated_strength'], 42)
        self.assertEqual([s['calculated_strength'] for s in info['device_strengths']], [42, 24])
        runtime.router.paused = True
        self.assertEqual(runtime.snapshot()['device_channels']['coyote']['A']['calculated_strength'], 0)
        runtime.router.paused = False
        runtime.router.device_states = lambda: ()
        self.assertEqual(runtime.snapshot()['device_channels']['coyote']['A']['calculated_strength'], 0)

    async def test_diagnostics_logs_computed_value_and_not_modulated_samples(self):
        runtime = self.make_runtime()
        records = []
        sink = logger.add(lambda message: records.append(str(message)), level='INFO')
        task = asyncio.create_task(runtime._diagnostics_task())
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.remove(sink)
        self.assertEqual(sum('计算强度 device=' in line for line in records), 4)
        self.assertTrue(all('calculated=0.00' in line for line in records))

    async def test_unchanged_strength_and_feedback_are_not_periodically_reprinted(self):
        runtime = self.make_runtime()
        records = []
        sink = logger.add(lambda message: records.append(message.record), level='INFO')
        task = asyncio.create_task(runtime._diagnostics_task())
        try:
            await asyncio.sleep(0.05)
            await asyncio.sleep(5.15)  # Beyond the removed five-second heartbeat.
            self.assertEqual(sum(record['extra'].get('strength_event', False) for record in records), 4)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            logger.remove(sink)

    async def test_expired_close_gate_cannot_reactivate_legacy_depth(self):
        runtime = self.make_runtime()
        handler = runtime.handlers[0]
        depth = '/avatar/parameters/OGB/Orf/Test/PenOthers'
        close = '/avatar/parameters/OGB/Orf/Test/PenOthersClose'
        handler.set_avatar_parameters((depth, close))
        handler.start_background_jobs()
        handler.osc_handler(depth, (SPS_SOCKET, 'PenOthers'), 0.8)
        await asyncio.sleep(0)
        self.assertEqual(handler.current_strength_percentage, 0)
        handler.receive_query_value(close, (SPS_SOCKET, 'PenOthersClose'), 1, time.perf_counter())
        await asyncio.sleep(0)
        self.assertEqual(handler.current_strength_percentage, 0.8)
        await asyncio.sleep(0.58)
        self.assertEqual(handler.current_strength_percentage, 0)


class OGBParityAndRunLogTests(unittest.TestCase):
    def test_lost_query_connection_invalidates_old_avatar_once(self):
        events = []
        client = VRChatOSCQueryClient(None, events.append)
        client._publish(OSCQueryAvatarSnapshot('avtr_old', (), 'http://127.0.0.1:1'))
        client.last_query_success = 10
        client._publish_disconnection_if_stale(14.9)
        self.assertEqual(len(events), 1)
        client._publish_disconnection_if_stale(15.1)
        self.assertEqual(events[-1].avatar_id, '')
        self.assertEqual(events[-1].endpoint, '')
        self.assertEqual(len(events), 2)
        client._publish_disconnection_if_stale(30)
        self.assertEqual(len(events), 2)

    def test_legacy_close_gate_matches_ogb_without_overriding_new_depth(self):
        calculator = SPSDepthCalculator(SPS_SOCKET)
        self.assertEqual(calculator.update('PenOthers', 0.8), 0.8)
        self.assertEqual(calculator.update('PenOthersClose', 0), 0)
        self.assertEqual(calculator.update('PenOthersClose', 1), 0.8)
        calculator.update('PenOthersClose', 0)
        calculator.forget('PenOthersClose')
        self.assertEqual(calculator.depth, 0)  # Expired gate must not revive legacy depth.
        for _ in range(4):
            calculator.update('PenOthersNewRoot', 0.2)
            calculator.update('PenOthersNewTip', 0.6)
        calculator.update('PenOthersNewRoot', 0.8)
        self.assertAlmostEqual(calculator.update('PenOthersNewTip', 1), 0.5)

    def test_each_launch_has_a_separate_writable_log_file(self):
        keys = ('SETTINGS', 'SETTINGS_BASIC', 'CONFIG_MANAGER', 'CONFIG_FILENAME', 'CONFIG_FILENAME_BASIC', 'SERVER_IP')
        previous = {key: getattr(core, key) for key in keys}
        try:
            with tempfile.TemporaryDirectory() as root:
                with patch.object(core, 'APP_DIR', root):
                    for _ in range(2):
                        core.config_init(root)
                        logger.info('run-log-marker')
                logger.remove()
                logs = list((Path(root) / 'log').glob('*.log'))
                self.assertEqual(len(logs), 2)
                self.assertTrue(all('run-log-marker' in file.read_text(encoding='utf-8') for file in logs))
        finally:
            logger.remove()
            for key, value in previous.items():
                setattr(core, key, value)
