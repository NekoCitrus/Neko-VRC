import asyncio
import copy
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import srv
from srv.config_manager import ConfigManager, DEFAULT_BASIC_SETTINGS, DEFAULT_SETTINGS, apply_basic_settings, validate_config
from srv.connector.ble_protocol import (
    WRITE_UUID, V2_STRENGTH_UUID, V2_WAVE_UUID, ZERO_FRAME, coyote_limits,
    coyote_packet, opossum_display, opossum_packet, opossum_strength,
    v2_strength, v2_wave,
)
from srv.connector.bluetooth import BLEHost, scan_hosts
from srv.device_router import DeviceRouter


ACTIVE = '0A0A0A0A19324B64'


def settings():
    value = copy.deepcopy(DEFAULT_SETTINGS)
    value['ws']['master_uuid'] = '11111111-1111-1111-1111-111111111111'
    return apply_basic_settings(value, copy.deepcopy(DEFAULT_BASIC_SETTINGS))


class ProtocolTests(unittest.TestCase):
    def test_coyote_absolute_pair_matches_official_layout(self):
        packet = coyote_packet(ACTIVE, ZERO_FRAME, 40, 0)
        self.assertEqual(len(packet), 20)
        self.assertEqual(packet[:4], bytes.fromhex('B00F2800'))
        self.assertEqual(packet[4:12], bytes.fromhex(ACTIVE))
        self.assertEqual(packet[12:], bytes.fromhex(ZERO_FRAME))

    def test_opossum_never_sends_coyote_frequency_bytes(self):
        packet = opossum_packet(ACTIVE, ACTIVE)
        self.assertEqual(packet, bytes.fromhex('B00000000000000019324B640000000019324B64'))
        self.assertEqual(opossum_strength(160, 200), bytes.fromhex('B3A0C8'))

    def test_coyote_limits_and_opossum_screen_are_distinct(self):
        self.assertEqual(coyote_limits(50, 80), bytes.fromhex('BF325000000000'))
        self.assertEqual(opossum_display(10, 20)[-2:], bytes((10, 20)))
        self.assertGreater(len(opossum_display(0, 0)), 20)

    def test_reject_invalid_strength_and_amplitudes(self):
        for value in (-1, 201, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                opossum_strength(value, 0)
        for value in ('', '0A0A0A0A00000065', 'XX' * 8):
            with self.subTest(frame=value), self.assertRaises(ValueError):
                opossum_packet(value, ZERO_FRAME)
        with self.assertRaises(ValueError):
            coyote_packet('0000000000000000', ZERO_FRAME, 0, 0)

    def test_v2_strength_bit_order_and_zero_wave(self):
        self.assertEqual(int.from_bytes(v2_strength(10, 20), 'little'), (70 << 11) | 140)
        packed = int.from_bytes(v2_wave(ZERO_FRAME), 'little')
        self.assertEqual(packed >> 15 & 31, 0)
        self.assertLessEqual(int.from_bytes(v2_wave(ACTIVE), 'little') >> 15 & 31, 20)

    def test_connection_config_migrates_without_changing_existing_sources(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConfigManager(root, root)
            config, basic = manager.load()
            config['connections']['opossum'] = {'mode': 'bluetooth', 'address': '11:22', 'model': 'opossum'}
            basic['dglab3']['opossum']['channel_a']['socket_enabled'] = False
            manager.save(config, basic)
            loaded, loaded_basic = manager.load()
            self.assertEqual(loaded['connections'], config['connections'])
            self.assertFalse(loaded_basic['dglab3']['opossum']['channel_a']['socket_enabled'])

    def test_invalid_connection_settings_rejected(self):
        config = settings()
        for field, value in (('mode', 'both'), ('address', 123), ('model', 'opossum')):
            changed = copy.deepcopy(config)
            changed['connections']['coyote'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_config(changed, DEFAULT_BASIC_SETTINGS)


class FakeClient:
    def __init__(self, address, **kwargs):
        self.address = address
        self.disconnected_callback = kwargs['disconnected_callback']
        self.mtu_size = 64
        self.writes = []
        self.notifications = {}
        self.is_connected = False

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.disconnected_callback(self)

    async def write_gatt_char(self, uuid, data, response):
        self.writes.append((uuid, bytes(data), response))

    async def read_gatt_char(self, uuid):
        return bytes((80,))

    async def start_notify(self, uuid, callback):
        self.notifications[uuid] = callback


class BLETests(unittest.IsolatedAsyncioTestCase):
    async def make_host(self, model='opossum'):
        kind = 'opossum' if model == 'opossum' else 'coyote'
        host = BLEHost(kind, model, 'test-address', settings(), FakeClient)
        await host.connect()
        self.addAsyncCleanup(host.close)
        return host

    async def test_all_models_zero_before_any_active_frame(self):
        for model in ('coyote2', 'coyote3', 'opossum'):
            with self.subTest(model=model):
                host = await self.make_host(model)
                first = host.client.writes[0][1]
                if model == 'coyote2': self.assertEqual(first, bytes(3))
                elif model == 'coyote3': self.assertEqual(first, coyote_packet(ZERO_FRAME, ZERO_FRAME, 0, 0))
                else: self.assertEqual(first, bytes.fromhex('B30000'))
                self.assertEqual(host.battery, 80)
                self.assertEqual(host.commanded, {'A': 0, 'B': 0})
                await host.close()

    async def test_live_frame_keeps_fixed_cap_and_expires_to_zero(self):
        host = await self.make_host()
        with patch('srv.connector.bluetooth.time.monotonic', return_value=10):
            host.accept_frame('A', json.dumps([ACTIVE]))
        await host.tick(10.1)
        self.assertEqual(host.commanded, {'A': 100, 'B': 0})
        packet = host.client.writes[-1][1]
        self.assertEqual(packet[8:12], bytes.fromhex(ACTIVE)[4:])
        await host.tick(10.4)
        self.assertEqual(host.commanded, {'A': 0, 'B': 0})
        self.assertEqual(host.limits()['A'], 100)

    async def test_coyote_soft_limit_written_on_connect_and_hot_save(self):
        host = await self.make_host('coyote3')
        self.assertEqual(host.client.writes[1][1], coyote_limits(100, 100))
        updated = settings()
        updated['dglab3']['coyote']['channel_a']['strength_limit'] = 33
        original = host.client
        await host.apply_settings(updated)
        self.assertIs(host.client, original)
        self.assertTrue(host.connected)
        self.assertEqual(host.client.writes[-1][1], coyote_limits(33, 100))

    async def test_unavailable_screen_mtu_does_not_split_commands(self):
        host = await self.make_host()
        host.client.mtu_size = 23
        host.display_supported = False
        host.client.writes.clear()
        host._notify(None, bytes.fromhex('B31020'))
        await asyncio.sleep(0)
        self.assertEqual(host.feedback, {'A': 16, 'B': 32})
        self.assertFalse(any(packet[0] == 0xB2 for _, packet, _ in host.client.writes))

    async def test_close_zeroes_hardware_and_discards_frame(self):
        host = await self.make_host()
        host.accept_frame('B', json.dumps([ACTIVE]))
        await host.tick()
        await host.close()
        self.assertFalse(host.connected)
        self.assertEqual(host.client.writes[-2][1], bytes.fromhex('B30000'))
        self.assertEqual(host.frames['B'], (ZERO_FRAME, 0))

    async def test_bluetooth_write_failure_closes_connection(self):
        host = await self.make_host()
        host.client.write_gatt_char = AsyncMock(side_effect=OSError('lost'))
        await asyncio.sleep(0.15)
        self.assertFalse(host.connected)
        self.assertIn('lost', host.error)

    async def test_notify_in_range_and_malformed_packets(self):
        host = await self.make_host('coyote3')
        host._notify(None, bytes.fromhex('B1011428'))
        self.assertEqual(host.feedback, {'A': 20, 'B': 40})
        host._notify(None, bytes.fromhex('B101FFFF'))
        self.assertEqual(host.feedback, {'A': 20, 'B': 40})

    async def test_scan_filters_names_and_preserves_hardware_id(self):
        from types import SimpleNamespace
        discovered = {'a': (SimpleNamespace(address='addr', name='old-name'), SimpleNamespace(local_name='47L127000', rssi=-40)),
                      'b': (SimpleNamespace(address='other', name='unrelated'), SimpleNamespace(local_name='', rssi=-20))}
        with patch('bleak.BleakScanner.discover', new=AsyncMock(return_value=discovered)):
            hosts = await scan_hosts(0.01)
        self.assertEqual(hosts, [{'address': 'addr', 'name': '47L127000', 'model': 'opossum', 'device_kind': 'opossum', 'rssi': -40}])

    async def test_unknown_or_wrong_host_never_receives_commands(self):
        host = BLEHost('coyote', 'coyote3', 'unknown-address', settings())
        with patch('bleak.BleakScanner.find_device_by_filter', new=AsyncMock(return_value=None)):
            with self.assertRaises(RuntimeError):
                await host.connect()
        self.assertIsNone(host.client)
        self.assertFalse(host.connected)

    async def test_write_mode_uses_actual_characteristic_capabilities(self):
        from types import SimpleNamespace
        host = await self.make_host()
        host.client.services = SimpleNamespace(get_characteristic=lambda _: SimpleNamespace(
            properties=['write-without-response'], max_write_without_response_size=20))
        await host._write(opossum_strength(0, 0))
        self.assertFalse(host.client.writes[-1][2])
        with self.assertRaises(ValueError):
            await host._write(opossum_display(0, 0))

    async def test_failed_notify_subscription_disarms_connection(self):
        host = BLEHost('opossum', 'opossum', 'test-address', settings(), FakeClient)
        original = FakeClient.start_notify
        with patch.object(FakeClient, 'start_notify', new=AsyncMock(side_effect=OSError('notify unavailable'))):
            with self.assertRaises(OSError):
                await host.connect()
        self.assertFalse(host.connected)
        self.assertFalse(host.client.is_connected)

    async def test_single_channel_clear_preserves_other_live_channel(self):
        host = await self.make_host()
        host.accept_frame('A', json.dumps([ACTIVE]))
        host.accept_frame('B', json.dumps([ACTIVE]))
        await host.clear('A')
        self.assertEqual(host.commanded, {'A': 0, 'B': 100})
        self.assertEqual(host.client.writes[-1][1][8:12], bytes(4))
        self.assertEqual(host.client.writes[-1][1][16:20], bytes.fromhex(ACTIVE)[4:])


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_routes_and_hot_switch_do_not_duplicate(self):
        config = settings()
        config['connections']['coyote']['mode'] = 'app'
        config['connections']['opossum']['mode'] = 'bluetooth'
        router = DeviceRouter(config)
        host = Mock(connected=True, address='opossum')
        host.clear, host.close, host.apply_settings = AsyncMock(), AsyncMock(), AsyncMock()
        router.hosts['opossum'] = host
        app = Mock(spec=['send_wave', 'clear_wave', 'uuid'], send_wave=AsyncMock(), clear_wave=AsyncMock())
        with patch('srv.get_ws_connections', return_value=(app,)):
            await router.broadcast_wave('A', json.dumps([ACTIVE]), 'coyote')
            await router.broadcast_wave('B', json.dumps([ACTIVE]), 'opossum')
            self.assertEqual(app.send_wave.await_count, 1)
            host.accept_frame.assert_called_once_with('B', json.dumps([ACTIVE]))
            updated = copy.deepcopy(config)
            updated['connections']['opossum']['mode'] = 'app'
            await router.apply_settings(updated)
            host.close.assert_awaited_once()
            self.assertNotIn('opossum', router.hosts)

    async def test_stop_latches_and_never_accepts_future_samples(self):
        config = settings()
        for connection in config['connections'].values():
            connection['mode'] = 'app'
        router = DeviceRouter(config)
        app = Mock(spec=['send_wave', 'clear_wave', 'uuid'], send_wave=AsyncMock(), clear_wave=AsyncMock())
        with patch('srv.get_ws_connections', return_value=(app,)):
            await router.emergency_stop()
            await router.broadcast_wave('A', json.dumps([ACTIVE]), 'coyote')
        self.assertTrue(router.paused)
        self.assertEqual(app.clear_wave.await_count, 4)
        app.send_wave.assert_not_awaited()

    async def test_only_one_transport_in_device_snapshot(self):
        config = settings()
        config['connections']['coyote']['mode'] = 'app'
        config['connections']['opossum']['mode'] = 'bluetooth'
        router = DeviceRouter(config)
        host = Mock(connected=True)
        host.snapshot.return_value = {'device_kind': 'opossum', 'slot_id': 'BLE', 'transport': 'bluetooth'}
        router.hosts['opossum'] = host
        app = Mock()
        app.get_device_states.return_value = ({'device_kind': 'opossum', 'slot_id': 'APP'}, {'device_kind': 'coyote', 'slot_id': 'C'})
        with patch('srv.get_ws_connections', return_value=(app,)):
            self.assertEqual([state['slot_id'] for state in router.device_states()], ['C', 'BLE'])


if __name__ == '__main__':
    unittest.main()
