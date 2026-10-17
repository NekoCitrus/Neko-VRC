"""Firmware-like Coyote V3 fake: no App session or pre-initialized host."""
import asyncio
import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import shocking_vrchat as core
from srv.config_manager import DEFAULT_SETTINGS, DEFAULT_BASIC_SETTINGS, apply_basic_settings
from srv.connector.bluetooth import BLEHost
from srv.connector.ble_protocol import NOTIFY_UUID, ZERO_FRAME
from srv.sps_depth import SPS_PLUG, SPS_SOCKET, PCS_PROXIMITY_ADDRESS
from test_bluetooth import FakeClient


class CoyoteFirmware(FakeClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.intensity = [0, 0]
        self.caps = [0, 0]
        self.active = False

    async def write_gatt_char(self, uuid, data, response):
        await super().write_gatt_char(uuid, data, response)
        if data[0] == 0xBF:
            self.caps = list(data[1:3])
        if data[0] == 0xB0:
            for index, shift in ((0, 2), (1, 0)):
                mode = data[1] >> shift & 3
                if mode == 3:
                    self.intensity[index] = min(data[index + 2], self.caps[index])
            self.active = bool(self.intensity[0] and any(data[8:12]))
            sequence = data[1] >> 4
            if sequence:
                self.notifications[NOTIFY_UUID](None, bytes((0xB1, sequence, *self.intensity)))


class StandaloneCoyoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = apply_basic_settings(copy.deepcopy(DEFAULT_SETTINGS), copy.deepcopy(DEFAULT_BASIC_SETTINGS))
        # Explicit PCS opt-in for the standalone routing regression below.
        self.settings['dglab3']['coyote']['channel_a']['pcs_enabled'] = True
        self.settings['chatbox']['enable'] = False
        self.runtime = core.DeviceRuntime(self.settings)
        self.runtime.dispatcher = self.runtime._build_dispatcher()
        self.host = BLEHost('coyote', 'coyote3', 'fake-device', self.settings, CoyoteFirmware)
        self.runtime.router.hosts['coyote'] = self.host
        self.addAsyncCleanup(self.host.close)
        self.addCleanup(self.runtime.chatbox_manager.cleanup, notify=False)
        for handler in self.runtime.handlers:
            handler.start_background_jobs()
            self.addAsyncCleanup(handler.stop_background_jobs)
        self.no_app = patch('srv.get_ws_connections', return_value=())
        self.no_app.start()
        self.addCleanup(self.no_app.stop)
        await self.host.connect()

    async def wait_for_output(self):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not self.host.client.active:
            await asyncio.sleep(0.02)
        self.assertTrue(self.host.client.active)
        state = self.runtime.snapshot()
        self.assertFalse(state['app_connected'])
        self.assertGreater(state['device_channels']['coyote']['A']['calculated_strength'], 0)
        self.assertEqual(state['device_channels']['coyote']['A']['actual_strength'], 100)

    async def test_fresh_plug_trigger_outputs_without_app(self):
        self.runtime.handlers[0].osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf', (SPS_PLUG, 'PenSelf'), 0.6)
        await self.wait_for_output()
        self.assertEqual(self.runtime.snapshot()['device_channels']['coyote']['A']['calculated_strength'], 60)

    async def test_pcs_fallback_outputs_without_app(self):
        handler = self.runtime.handlers[0]
        handler.set_avatar_parameters((PCS_PROXIMITY_ADDRESS,))
        handler.osc_handler(PCS_PROXIMITY_ADDRESS, ('pcs', 'value'), 0.6)
        await self.wait_for_output()

    async def test_socket_root_tip_depth_outputs_without_app(self):
        handler = self.runtime.handlers[0]
        for _ in range(4):
            handler.osc_handler('/avatar/parameters/OGB/Orf/Test/PenSelfNewRoot', (SPS_SOCKET, 'PenSelfNewRoot'), 0.2)
            handler.osc_handler('/avatar/parameters/OGB/Orf/Test/PenSelfNewTip', (SPS_SOCKET, 'PenSelfNewTip'), 0.6)
        handler.osc_handler('/avatar/parameters/OGB/Orf/Test/PenSelfNewRoot', (SPS_SOCKET, 'PenSelfNewRoot'), 0.8)
        handler.osc_handler('/avatar/parameters/OGB/Orf/Test/PenSelfNewTip', (SPS_SOCKET, 'PenSelfNewTip'), 1.0)
        await self.wait_for_output()
        self.assertEqual(self.runtime.snapshot()['device_channels']['coyote']['A']['calculated_strength'], 50)

    async def test_map_event_outputs_without_app(self):
        await self.runtime.handlers[0].trigger_map_event(1)
        await self.wait_for_output()

    async def test_legacy_sendwave_uses_ble_router_without_app(self):
        with patch.object(core, 'ACTIVE_CONTROLLER', SimpleNamespace(runtime=self.runtime)):
            await core.broadcast_repeated_wave(['A'], 2, '0A0A0A0A64646464')
        self.assertTrue(self.host.client.active)
        self.assertEqual(self.host.client.intensity[0], 100)

    async def test_command_does_not_fake_a_reported_strength(self):
        self.host.reported_strength = {'A': None, 'B': None}
        self.assertIsNone(self.runtime.snapshot()['device_channels']['coyote']['A']['actual_strength'])

    async def test_zero_overrides_an_unacknowledged_positive_request(self):
        self.host._coyote_pending = (3, time.monotonic())
        self.host.commanded = {'A': 100, 'B': 0}
        await self.host.clear()
        self.assertEqual(self.host.client.intensity, [0, 0])
        self.assertFalse(self.host.client.active)
