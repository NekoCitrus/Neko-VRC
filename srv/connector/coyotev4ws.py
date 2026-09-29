"""DG-LAB Socket V4 adapter for all supported slots reported by one App.

The desktop application acts as the V4 controller and relay endpoint.  One
App connection can expose multiple Coyote and Opossum hosts; every host keeps
independent strength state and receives operations addressed to its slot ID.
"""

import asyncio
import json
import secrets
import time

from loguru import logger

from srv import add_ws_connection, remove_ws_connection
from srv.waveform import normalize_socket_frame


COYOTE_DEVICE_TYPES = {'COYOTE_020', 'COYOTE_030'}
OPOSSUM_DEVICE_TYPES = {'OVC_1'}
SUPPORTED_DEVICE_TYPES = COYOTE_DEVICE_TYPES | OPOSSUM_DEVICE_TYPES


def _device_kind(device_type):
    if device_type in COYOTE_DEVICE_TYPES:
        return 'coyote'
    if device_type in OPOSSUM_DEVICE_TYPES:
        return 'opossum'
    return None


def _merge_dict(current, patch):
    if not isinstance(current, dict) or not isinstance(patch, dict):
        return patch
    merged = dict(current)
    for key, value in patch.items():
        merged[key] = _merge_dict(current.get(key), value)
    return merged


class DGV4Connection:
    protocol_version = 'v4'

    def __init__(self, ws_connection, settings):
        self.ws_conn = ws_connection
        self.SETTINGS = settings
        self.master_uuid = str(settings['ws']['master_uuid'])
        self.uuid = secrets.token_hex(4)
        self.strength = {'A': 0, 'B': 0}
        self.strength_max = {'A': 200, 'B': 200}
        self.strength_limits = {
            'coyote': {
                'A': settings['dglab3']['coyote']['channel_a']['strength_limit'],
                'B': settings['dglab3']['coyote']['channel_b']['strength_limit'],
            },
            'opossum': {
                'A': settings['dglab3']['opossum']['channel_a']['strength_limit'],
                'B': settings['dglab3']['opossum']['channel_b']['strength_limit'],
            },
        }
        # Compatibility for the legacy V3-facing API: this always means Coyote.
        self.strength_limit = self.strength_limits['coyote']
        self.devices = {}
        self.device_states = {}
        self.slot_id = None
        self.device_type = None
        self.device_name = None
        self._request_counter = 0
        self._send_lock = asyncio.Lock()
        add_ws_connection(self)

    @staticmethod
    def _validate_channel(channel):
        channel = str(channel).upper()
        if channel not in ('A', 'B'):
            raise ValueError(f'Invalid channel: {channel}')
        return channel

    def is_device_ready(self, device_kind=None):
        if device_kind is None:
            return bool(self.device_states)
        return any(state['device_kind'] == device_kind for state in self.device_states.values())

    def get_upper_strength(self, channel='A', slot_id=None, device_kind=None):
        channel = self._validate_channel(channel)
        state = self.device_states.get(slot_id) if slot_id else None
        if state is None:
            state = next(
                (
                    item for item in self.device_states.values()
                    if device_kind is None or item['device_kind'] == device_kind
                ),
                None,
            )
        if state is None:
            return 0
        limit = self.strength_limits[state['device_kind']][channel]
        return min(state['strength_max'][channel], limit)

    def get_device_states(self):
        result = []
        for state in self.device_states.values():
            result.append({
                **state,
                'strength': dict(state['strength']),
                'strength_max': dict(state['strength_max']),
                'upper_strength': {
                    channel: self.get_upper_strength(channel, slot_id=state['slot_id'])
                    for channel in ('A', 'B')
                },
            })
        return tuple(result)

    async def _send_frame(self, frame):
        message = json.dumps(frame, ensure_ascii=False, separators=(',', ':'))
        logger.debug('V4 ID {}, SENDING {}', self.uuid, message)
        async with self._send_lock:
            await self.ws_conn.send(message)

    async def _send_request(self, method, data=None):
        if method == 'device.op' and (
            not isinstance(data, dict) or data.get('s') not in self.device_states
        ):
            return False
        if method == 'device.op.clear' and isinstance(data, dict) and (
            data.get('s') not in self.device_states
        ):
            return False
        self._request_counter += 1
        request = {
            't': 'req',
            'reqId': f'{self.uuid}-{self._request_counter}',
            'm': method,
        }
        if data is not None:
            request['data'] = data
        await self._send_frame({'type': 'message', 'data': request})
        return True

    def _replace_devices(self, devices):
        self.devices = {
            device['slotId']: dict(device)
            for device in devices
            if isinstance(device, dict) and isinstance(device.get('slotId'), str)
        }
        self._refresh_device_states(reset_all=True)

    def _patch_devices(self, added, removed):
        reset_slots = set()
        for device in added:
            if isinstance(device, dict) and isinstance(device.get('slotId'), str):
                self.devices[device['slotId']] = dict(device)
                reset_slots.add(device['slotId'])
        for slot_id in removed:
            if isinstance(slot_id, str):
                self.devices.pop(slot_id, None)
        self._refresh_device_states(reset_slots=reset_slots)

    def _patch_slots(self, slots):
        for patch in slots:
            if not isinstance(patch, dict):
                continue
            slot_id = patch.get('slotId')
            if slot_id not in self.devices:
                continue
            self.devices[slot_id] = _merge_dict(self.devices[slot_id], patch)
        self._refresh_device_states()

    def _refresh_device_states(self, reset_all=False, reset_slots=None):
        reset_slots = set(reset_slots or ())
        previous = self.device_states
        current = {}
        for slot_id, device in self.devices.items():
            device_type = device.get('type')
            device_kind = _device_kind(device_type)
            if device_kind is None or not (device.get('slotState') or {}).get('hasDevice', True):
                continue
            old_state = previous.get(slot_id)
            if (
                reset_all
                or slot_id in reset_slots
                or old_state is None
                or old_state['device_type'] != device_type
            ):
                state = {
                    'slot_id': slot_id,
                    'device_type': device_type,
                    'device_kind': device_kind,
                    'device_name': device.get('name'),
                    'strength': {'A': 0, 'B': 0},
                    'strength_max': {'A': 200, 'B': 200},
                }
            else:
                state = old_state
                state['device_name'] = device.get('name')
            self._update_device_state(state, device)
            current[slot_id] = state

        previous_ids = {(slot, state['device_type']) for slot, state in previous.items()}
        current_ids = {(slot, state['device_type']) for slot, state in current.items()}
        self.device_states = current
        self._sync_primary_device()

        for slot_id, device_type in current_ids - previous_ids:
            state = current[slot_id]
            logger.info(
                'V4 设备已就绪：{} ({}, {})',
                slot_id,
                device_type,
                state['device_name'] or '未命名',
            )
        for slot_id, _device_type in previous_ids - current_ids:
            logger.warning('V4 设备已断开：{}', slot_id)

    def _sync_primary_device(self):
        primary = next(
            (state for state in self.device_states.values() if state['device_kind'] == 'coyote'),
            next(iter(self.device_states.values()), None),
        )
        if primary is None:
            self.slot_id = None
            self.device_type = None
            self.device_name = None
            self.strength = {'A': 0, 'B': 0}
            self.strength_max = {'A': 200, 'B': 200}
            return
        self.slot_id = primary['slot_id']
        self.device_type = primary['device_type']
        self.device_name = primary['device_name']
        self.strength = primary['strength']
        self.strength_max = primary['strength_max']

    def _update_device_state(self, state, device):
        props = device.get('props') or {}
        slot_state = device.get('slotState') or {}
        for channel in ('A', 'B'):
            strength = props.get(f'intensity{channel}')
            if isinstance(strength, (int, float)):
                state['strength'][channel] = max(0, min(200, int(strength)))
            channel_state = slot_state.get(f'channel{channel}') or {}
            maximum = channel_state.get('intensityMax')
            if state['device_kind'] == 'coyote' and isinstance(maximum, (int, float)):
                state['strength_max'][channel] = max(0, min(200, int(maximum)))

    def _handle_data(self, data):
        if not isinstance(data, dict):
            return False
        if data.get('t') == 'ev':
            event = data.get('ev')
            if event == 'devices.snapshot':
                self._replace_devices(data.get('devices') or [])
                return True
            elif event == 'devices.patch':
                self._patch_devices(data.get('added') or [], data.get('removed') or [])
                return True
            elif event == 'slots.patch':
                self._patch_slots(data.get('slots') or [])
                return True
        elif data.get('t') == 'resp':
            result = data.get('result')
            if isinstance(result, dict) and isinstance(result.get('devices'), list):
                self._replace_devices(result['devices'])
                return True
        return False

    async def _sync_strength_limits(self):
        for slot_id, state in tuple(self.device_states.items()):
            # Opossum intensity is controlled by the trigger lifecycle in
            # send_wave()/clear_wave(). Synchronizing it on every device or
            # slot patch would start an idle vibrator or cancel an active one.
            if state['device_kind'] != 'coyote':
                continue
            for channel in ('A', 'B'):
                await self.set_strength(
                    channel,
                    mode='2',
                    value=self.get_upper_strength(channel, slot_id=slot_id),
                    slot_id=slot_id,
                )

    async def _handle_message(self, raw_message):
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode('utf-8')
        try:
            frame = json.loads(raw_message)
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning('V4 忽略无法解析的 WebSocket 消息。')
            return
        if not isinstance(frame, dict):
            return
        frame_type = frame.get('type')
        if frame_type == 'message':
            if self._handle_data(frame.get('data')):
                await self._sync_strength_limits()
        elif frame_type == 'ping':
            await self._send_frame({'type': 'pong', 'ts': int(time.time() * 1000)})
        elif frame_type not in ('pong', 'heartbeat'):
            logger.debug('V4 忽略未知帧类型：{}', frame_type)

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(30)
            await self._send_frame({'type': 'heartbeat'})

    async def serve(self):
        logger.info('V4 App 已连接，连接 ID：{}', self.uuid)
        heartbeat_task = None
        try:
            await self._send_frame({'type': 'hello', 'clientId': self.uuid})
            await self._send_frame({'type': 'controller_attached', 'clientId': self.master_uuid})
            await self._send_request('devices.get')
            heartbeat_task = asyncio.create_task(self._heartbeat())
            async for message in self.ws_conn:
                await self._handle_message(message)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            remove_ws_connection(self)
            logger.info('V4 App 已断开，连接 ID：{}', self.uuid)

    async def set_strength(self, channel='A', mode='2', value=0, force=False, slot_id=None):
        channel = self._validate_channel(channel)
        if slot_id is None:
            for target_slot in tuple(self.device_states):
                await self.set_strength(
                    channel,
                    mode=mode,
                    value=value,
                    force=force,
                    slot_id=target_slot,
                )
            return
        state = self.device_states.get(slot_id)
        if state is None:
            return
        value = int(value)
        if not force and not 0 <= value <= 200:
            raise ValueError('Strength must be between 0 and 200.')
        upper = self.get_upper_strength(channel, slot_id=slot_id)
        channel_index = 0 if channel == 'A' else 1
        if mode == '2':
            target = max(0, min(value, upper))
            delta = target - state['strength'][channel]
        elif mode == '1':
            delta = abs(value)
            target = min(upper, state['strength'][channel] + delta)
            delta = target - state['strength'][channel]
        elif mode == '0':
            delta = -abs(value)
            target = max(0, state['strength'][channel] + delta)
            delta = target - state['strength'][channel]
        else:
            raise ValueError(f'Invalid strength mode: {mode}')
        if delta == 0:
            return
        state['strength'][channel] = target
        if target == 0 and mode == '2':
            operation = {'s': slot_id, 't': 7, 'c': channel_index, 'p': 1, 'v': 0}
        else:
            operation = {'s': slot_id, 't': 3, 'c': channel_index, 'p': 1, 'v': delta}
        await self._send_request('device.op', operation)

    async def set_strength_0_to_1(self, channel='A', value=0):
        value = float(value)
        if not 0 <= value <= 1:
            raise ValueError('Normalized strength must be between 0 and 1.')
        channel = self._validate_channel(channel)
        for slot_id in tuple(self.device_states):
            await self.set_strength(
                channel,
                mode='2',
                value=int(self.get_upper_strength(channel, slot_id=slot_id) * value),
                slot_id=slot_id,
            )

    async def apply_settings(self, settings):
        """Update per-device caps while preserving the Socket connection."""
        self.SETTINGS = settings
        self.strength_limits = {
            device_kind: {
                'A': settings['dglab3'][device_kind]['channel_a']['strength_limit'],
                'B': settings['dglab3'][device_kind]['channel_b']['strength_limit'],
            }
            for device_kind in ('coyote', 'opossum')
        }
        self.strength_limit = self.strength_limits['coyote']
        for slot_id, state in tuple(self.device_states.items()):
            for channel in ('A', 'B'):
                if state['strength'][channel] != 0:
                    await self.set_strength(
                        channel,
                        value=self.get_upper_strength(channel, slot_id=slot_id),
                        slot_id=slot_id,
                    )

    async def send_wave(self, channel='A', wavestr='[]', device_kind=None):
        channel = self._validate_channel(channel)
        try:
            frames = json.loads(wavestr)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError('Invalid V4 waveform JSON.') from exc
        if not frames or not all(isinstance(frame, str) for frame in frames):
            raise ValueError('V4 waveform must contain hexadecimal string frames.')
        for state in tuple(self.device_states.values()):
            if device_kind is not None and state['device_kind'] != device_kind:
                continue
            if state['device_kind'] == 'opossum':
                await self.set_strength(
                    channel,
                    mode='2',
                    value=self.get_upper_strength(channel, slot_id=state['slot_id']),
                    slot_id=state['slot_id'],
                )
            device_frames = [
                normalize_socket_frame(frame, state['device_type'])
                for frame in frames
            ]
            operation = {
                's': state['slot_id'],
                't': 0,
                'c': 0 if channel == 'A' else 1,
                'p': 1,
                'd': len(device_frames) * 100,
                'im': True,
                'v': device_frames,
            }
            await self._send_request('device.op', operation)

    async def clear_wave(self, channel='A', device_kind=None):
        channel = self._validate_channel(channel)
        for slot_id, state in tuple(self.device_states.items()):
            if device_kind is not None and state['device_kind'] != device_kind:
                continue
            await self._send_request(
                'device.op.clear',
                {'s': slot_id, 'c': 0 if channel == 'A' else 1},
            )
            if state['device_kind'] == 'opossum':
                await self.set_strength(
                    channel,
                    mode='2',
                    value=0,
                    slot_id=slot_id,
                )
