"""Async BLE lifecycle with per-host scheduling, zero-on-connect and expiry."""

import asyncio
import json
import time

from loguru import logger

from .ble_protocol import (
    BATTERY_UUID, MODELS, NOTIFY_UUID, V2_STRENGTH_UUID, V2_WAVE_UUID,
    WRITE_UUID, ZERO_FRAME, coyote_limits, coyote_packet, frame_bytes,
    opossum_display, opossum_packet, opossum_strength, v2_strength, v2_uuid, v2_wave,
)


class BLEHost:
    """One physical host; only its scheduler writes paired A/B samples."""

    def __init__(self, kind, model, address, settings, client_factory=None):
        self.kind, self.model, self.address = kind, model, address
        self.settings = settings
        self.client_factory = client_factory
        self.client = None
        self.task = None
        self.lock = asyncio.Lock()
        self.connected = False
        self.error = ''
        self.battery = None
        self.feedback = {'A': 0, 'B': 0}
        self.commanded = {'A': 0, 'B': 0}
        self.sent_frames = {'A': ZERO_FRAME, 'B': ZERO_FRAME}
        self.frames = {'A': (ZERO_FRAME, 0), 'B': (ZERO_FRAME, 0)}
        self.display_supported = True
        self._closing = False

    def limits(self):
        return {channel: self.settings['dglab3'][self.kind][f'channel_{channel.lower()}']['strength_limit']
                for channel in ('A', 'B')}

    def _disconnected(self, _client):
        self.connected = False
        self.frames = {'A': (ZERO_FRAME, 0), 'B': (ZERO_FRAME, 0)}
        self.commanded = {'A': 0, 'B': 0}
        self.sent_frames = {'A': ZERO_FRAME, 'B': ZERO_FRAME}
        if not self._closing:
            self.error = '蓝牙已断开，请重新连接。'

    def _battery(self, _sender, data):
        if len(data) == 1 and data[0] <= 100:
            self.battery = int(data[0])

    def _notify(self, _sender, data):
        if self.model == 'coyote3' and len(data) == 4 and data[0] == 0xB1:
            a, b = data[2:]
        elif self.model == 'opossum' and len(data) == 3 and data[0] == 0xB3:
            a, b = data[1:]
        elif self.model == 'coyote2' and len(data) == 3:
            value = int.from_bytes(data, 'little')
            a, b = (value >> 11 & 2047) // 7, (value & 2047) // 7
        else:
            return
        if a <= 200 and b <= 200:
            self.feedback = {'A': a, 'B': b}
            if self.model == 'opossum' and self.display_supported and self.connected:
                asyncio.create_task(self._update_display())

    async def _write(self, data, characteristic=WRITE_UUID):
        # Write-with-response gives bounded flow control on Windows.
        response = True
        services = getattr(self.client, 'services', None)
        if services is not None:
            char = services.get_characteristic(characteristic)
            if char is None:
                raise ValueError('蓝牙设备缺少所需特征。')
            response = 'write' in char.properties
            if not response and ('write-without-response' not in char.properties
                                 or len(data) > char.max_write_without_response_size):
                raise ValueError('设备不支持此蓝牙写入长度。')
        await asyncio.wait_for(self.client.write_gatt_char(characteristic, data, response=response), 2)

    async def _update_display(self):
        try:
            async with self.lock:
                if self.connected and self.display_supported:
                    await self._write(opossum_display(self.feedback['A'], self.feedback['B']))
        except Exception as exc:
            self.display_supported = False
            logger.warning('负鼠屏幕更新不可用：{}', exc)

    async def connect(self):
        if self.model not in ('coyote2', 'coyote3', 'opossum'):
            raise ValueError('蓝牙型号无效。')
        expected_kind = 'opossum' if self.model == 'opossum' else 'coyote'
        if self.kind != expected_kind:
            raise ValueError('蓝牙型号与通道不匹配。')
        if self.client_factory is None:
            from bleak import BleakClient, BleakScanner
            device = await BleakScanner.find_device_by_filter(
                lambda d, a: d.address.casefold() == self.address.casefold()
                and MODELS.get(a.local_name or d.name or '') == self.model,
                timeout=8,
            )
            if device is None:
                raise RuntimeError('设备未广播或型号不匹配，请释放手机连接后重新扫描。')
            self.client_factory = BleakClient
            target = device
        else:
            target = self.address
        self.client = self.client_factory(target, disconnected_callback=self._disconnected, timeout=12)
        try:
            await self.client.connect()
            self.connected = True
            async with self.lock:
                # Explicit zero BEFORE arming any waveform, including stale
                # hardware strength left behind by a previous application.
                await self._write_output(ZERO_FRAME, ZERO_FRAME, {'A': 0, 'B': 0}, force=True)
                if self.model == 'coyote3':
                    limits = self.limits()
                    await self._write(coyote_limits(limits['A'], limits['B']))
            notify = V2_STRENGTH_UUID if self.model == 'coyote2' else NOTIFY_UUID
            await self.client.start_notify(notify, self._notify)
            battery = v2_uuid(0x1500) if self.model == 'coyote2' else BATTERY_UUID
            try:
                self._battery(None, await self.client.read_gatt_char(battery))
                await self.client.start_notify(battery, self._battery)
            except Exception as exc:
                logger.debug('读取蓝牙电量失败：{}', exc)
            if self.model == 'opossum':
                self.display_supported = self.client.mtu_size >= len(opossum_display(0, 0)) + 3
                if self.display_supported:
                    await self._update_display()
            self.task = asyncio.create_task(self._run())
        except BaseException:
            await self.close()
            raise

    async def _write_output(self, a, b, target, force=False):
        if self.model == 'coyote3':
            await self._write(coyote_packet(a, b, target['A'], target['B']))
        elif self.model == 'opossum':
            if force or target != self.commanded or self.feedback != target:
                await self._write(opossum_strength(target['A'], target['B']))
            await self._write(opossum_packet(a, b))
        else:
            await self._write(v2_strength(target['A'], target['B']), V2_STRENGTH_UUID)
            await self._write(v2_wave(a), V2_WAVE_UUID['A'])
            await self._write(v2_wave(b), V2_WAVE_UUID['B'])
        self.commanded = dict(target)
        self.sent_frames = {'A': a, 'B': b}

    def accept_frame(self, channel, wavestr):
        frames = json.loads(wavestr)
        if channel not in ('A', 'B') or not isinstance(frames, list) or len(frames) != 1:
            raise ValueError('蓝牙输出需要单个 100ms 波形帧。')
        frame_bytes(frames[0])
        self.frames[channel] = (frames[0], time.monotonic())

    async def tick(self, now=None):
        now = time.monotonic() if now is None else now
        async with self.lock:
            if not self.connected:
                return
            frames = {c: f if 0 <= now - stamp <= 0.35 else ZERO_FRAME
                      for c, (f, stamp) in self.frames.items()}
            limits = self.limits()
            target = {c: limits[c] if any(frame_bytes(frames[c])[4:]) else 0 for c in ('A', 'B')}
            await self._write_output(frames['A'], frames['B'], target)

    async def _run(self):
        try:
            while self.connected:
                await asyncio.sleep(0.1)
                await self.tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc)
            logger.warning('蓝牙 {} 输出失败：{}', self.kind, exc)
            await self.close()

    async def clear(self, channel=None):
        channels = (channel,) if channel else ('A', 'B')
        for c in channels:
            self.frames[c] = (ZERO_FRAME, 0)
        if self.connected:
            await self.tick()

    async def apply_settings(self, settings):
        self.settings = settings
        await self.clear()
        if self.connected and self.model == 'coyote3':
            limits = self.limits()
            async with self.lock:
                await self._write(coyote_limits(limits['A'], limits['B']))

    async def close(self):
        self._closing = True
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        try:
            if self.connected:
                await self.clear()
        except Exception as exc:
            logger.warning('蓝牙归零失败：{}', exc)
        finally:
            self.connected = False
            if self.client:
                try:
                    await asyncio.wait_for(self.client.disconnect(), 3)
                except Exception as exc:
                    logger.warning('蓝牙断开失败：{}', exc)
            self.frames = {'A': (ZERO_FRAME, 0), 'B': (ZERO_FRAME, 0)}
            self.commanded = {'A': 0, 'B': 0}

    def snapshot(self):
        return {
            'slot_id': self.address, 'device_kind': self.kind,
            'device_type': {'coyote2': 'COYOTE_020', 'coyote3': 'COYOTE_030', 'opossum': 'OVC_1'}[self.model],
            'device_name': self.model, 'transport': 'bluetooth', 'connected': self.connected,
            'strength': dict(self.feedback), 'sent_strength': dict(self.commanded),
            'sent_frames': dict(self.sent_frames),
            'strength_max': self.limits(), 'upper_strength': self.limits(),
            'battery': self.battery, 'error': self.error, 'display_supported': self.display_supported,
        }


async def scan_hosts(timeout=5):
    from bleak import BleakScanner
    discoveries = await BleakScanner.discover(timeout=timeout, return_adv=True)
    hosts = []
    for device, advertisement in discoveries.values():
        name = advertisement.local_name or device.name or ''
        model = MODELS.get(name)
        if model:
            hosts.append({'address': device.address, 'name': name, 'model': model,
                          'device_kind': 'opossum' if model == 'opossum' else 'coyote',
                          'rssi': advertisement.rssi})
    return hosts
