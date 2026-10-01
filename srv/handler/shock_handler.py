import asyncio
import json
import time

from loguru import logger

from srv import WAVEFORM_NAMES_BY_DEVICE

from .base_handler import BaseHandler
from ..connector.coyotev3ws import DGConnection
from ..sps_depth import SPSDepthCalculator, SPS_PLUG, SPS_SOCKET, sps_osc_bindings
from ..waveform import frame_amplitudes, load_waveform_frames, scale_coyote_frame


SOURCE_EXTRA = 'extra'
SOURCE_MAP = 'map'
EXTRA_PARAMETER_TIMEOUT = 0.5


class ShockHandler(BaseHandler):
    """Drive one output from the maximum Socket, Plug, or extra-parameter depth."""

    def __init__(
        self, SETTINGS: dict, DG_CONN: DGConnection, channel_name: str,
        device_kind: str = 'coyote', event_callback=None,
    ) -> None:
        self.SETTINGS = SETTINGS
        self.DG_CONN = DG_CONN
        self.channel = channel_name.upper()
        if device_kind not in ('coyote', 'opossum'):
            raise ValueError(f'Unsupported device kind: {device_kind}')
        self.device_kind = device_kind
        self.event_callback = event_callback
        self.chatbox_manager = None
        self._background_tasks = set()
        self.calculators = {}
        self.calculator_inputs = {}
        self.extra_values = {}
        self.map_event_until = 0.0
        self.current_mode = 'none'
        self.active_source = ''
        self.active_zone = ''
        self.last_parameter = ''
        self.last_raw_value = 0.0
        self.is_active = False
        self.current_strength_percentage = 0.0
        self.depth_update_time_window = 0.1
        self.depth_current_strength = 0.0
        self.to_clear_time = 0.0
        self.is_cleared = True
        self.last_sent_frame = ''
        self.last_sent_strengths = (0, 0, 0, 0)
        self._load_settings(SETTINGS['dglab3'][device_kind][f'channel_{channel_name.lower()}'])

    def _load_settings(self, channel_settings):
        self.shock_settings = channel_settings
        self.socket_zone = channel_settings['socket_zone']
        self.plug_zone = channel_settings['plug_zone']
        self.extra_config = channel_settings['extra_parameters']
        self.depth_config = channel_settings['depth']
        default_waveform = WAVEFORM_NAMES_BY_DEVICE[self.device_kind][0]
        self.waveform = self.depth_config.get('waveform', default_waveform)
        self.wave_frames = load_waveform_frames(self.waveform, self.device_kind)
        self.wave_frame_index = 0

    def osc_bindings(self):
        bindings = []
        for trigger_type, zone in (
            (SPS_SOCKET, self.socket_zone), (SPS_PLUG, self.plug_zone),
        ):
            bindings.extend(
                (address, (trigger_type, signal))
                for address, signal in sps_osc_bindings(trigger_type, zone)
            )
        if self.extra_config['enabled']:
            bindings.extend(
                (path, (SOURCE_EXTRA, 'value')) for path in self.extra_config['paths']
            )
        return tuple(bindings)

    def set_chatbox_manager(self, chatbox_manager):
        self.chatbox_manager = chatbox_manager

    def get_mode_info(self):
        return {
            'device_kind': self.device_kind,
            'channel': self.channel,
            'mode': self.current_mode,
            'trigger_type': self.active_source,
            'socket_zone': self.socket_zone,
            'plug_zone': self.plug_zone,
            'active_zone': self.active_zone,
            'waveform': self.waveform,
            'is_active': self.is_active,
            'strength_percentage': self.current_strength_percentage,
            'parameter': self.last_parameter,
            'raw_value': self.last_raw_value,
            'sent_frame': self.last_sent_frame,
            'sent_strengths': self.last_sent_strengths,
        }

    def start_background_jobs(self):
        self._track_task(self.clear_check())
        self._track_task(self.depth_background_wave_feeder())

    def _track_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._task_finished)
        return task

    def _task_finished(self, task):
        self._background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(f'Channel {self.channel} background task failed: {task.exception()}')

    async def stop_background_jobs(self):
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _unwrap_binding(binding):
        if isinstance(binding, list) and len(binding) == 1:
            binding = binding[0]
        if not isinstance(binding, (list, tuple)) or len(binding) != 2:
            raise ValueError('Invalid OSC binding metadata')
        return binding

    def osc_handler(self, address, binding, *args):
        try:
            source, signal = self._unwrap_binding(binding)
            value = self.param_sanitizer(args)
        except ValueError as exc:
            logger.warning(f'通道 {self.channel} 忽略无效 OSC 参数 {address}: {exc}')
            return None
        raw_value = float(value)
        if source == SOURCE_EXTRA:
            if not self.extra_config['enabled']:
                return None
            bottom = float(self.extra_config['bottom'])
            top = float(self.extra_config['top'])
            depth = min(max((raw_value - bottom) / (top - bottom), 0.0), 1.0)
            self.extra_values[address] = (
                depth, time.monotonic() + EXTRA_PARAMETER_TIMEOUT, raw_value,
            )
        else:
            parts = address.split('/')
            if len(parts) < 7:
                logger.warning(f'Channel {self.channel} ignored malformed SPS address: {address}')
                return None
            zone = parts[5]
            calculator = self.calculators.setdefault(
                (source, zone), SPSDepthCalculator(source),
            )
            calculator.update(signal, raw_value)
            self.calculator_inputs[(source, zone)] = (address, raw_value)
        winner = self._winner()
        self._set_winner(winner)
        self._track_task(self.handler_depth(winner[0]))
        return None

    def _winner(self, now=None):
        now = time.monotonic() if now is None else now
        candidates = []
        if self.extra_config['enabled'] and self.map_event_until > now:
            candidates.append((1.0, SOURCE_MAP, '', '地图事件', 1.0))
        for (trigger_type, zone), calculator in self.calculators.items():
            address, raw_value = self.calculator_inputs.get(
                (trigger_type, zone), ('', 0.0),
            )
            candidates.append((calculator.depth, trigger_type, zone, address, raw_value))
        candidates.extend(
            (depth, SOURCE_EXTRA, '', address, raw_value)
            for address, (depth, expiry, raw_value) in self.extra_values.items()
            if expiry > now
        )
        return max(candidates, key=lambda item: item[0]) if candidates else (0.0, '', '', '', 0.0)

    async def trigger_map_event(self, seconds):
        """Use the channel's shared extra/map switch and normal output feeder."""
        if not self.extra_config['enabled']:
            return False
        self.map_event_until = max(self.map_event_until, time.monotonic() + seconds)
        winner = self._winner()
        self._set_winner(winner)
        await self.handler_depth(winner[0])
        return True

    def _set_winner(self, winner):
        depth, source, zone, parameter, raw_value = winner
        if depth <= 0:
            source = zone = parameter = ''
            raw_value = 0.0
        self.active_source = source
        self.current_mode = source or 'none'
        self.active_zone = zone
        self.last_parameter = parameter or (f'{source}:{zone}' if source and zone else '')
        self.last_raw_value = raw_value

    async def reconfigure(self, channel_settings):
        """Apply channel settings without replacing the live APP connection."""
        self.calculators.clear()
        self.calculator_inputs.clear()
        self.extra_values.clear()
        self.map_event_until = 0.0
        self._load_settings(channel_settings)
        self._set_winner((0.0, '', '', '', 0.0))
        await self._clear_output()

    def reset_for_avatar_change(self):
        self.calculators.clear()
        self.calculator_inputs.clear()
        self.extra_values.clear()
        self.map_event_until = 0.0
        self._set_winner((0.0, '', '', '', 0.0))
        self._track_task(self.handler_depth(0.0))

    def _emit_debug_event(self):
        if self.event_callback is None:
            return
        self.event_callback({
            'type': 'channel', 'device_kind': self.device_kind,
            'channel': self.channel, 'parameter': self.last_parameter,
            'raw_value': self.last_raw_value, 'source': self.active_source,
            'strength_percentage': self.current_strength_percentage,
            'active': self.is_active, 'active_zone': self.active_zone,
            'sent_frame': self.last_sent_frame,
            'sent_strengths': self.last_sent_strengths,
        })

    async def _clear_output(self):
        self.is_cleared = True
        self.depth_current_strength = 0.0
        self.is_active = False
        self.current_strength_percentage = 0.0
        await self.DG_CONN.broadcast_clear_wave(self.channel, device_kind=self.device_kind)
        self.last_sent_frame = ''
        self.last_sent_strengths = (0, 0, 0, 0)
        self._emit_debug_event()
        if self.chatbox_manager:
            self.chatbox_manager.update_channel_mode(
                self.channel, self.current_mode, 0.0,
                is_active=False, device_kind=self.device_kind,
            )

    async def clear_check(self):
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            expired = [
                address for address, (_depth, expiry, _raw) in self.extra_values.items()
                if expiry <= now
            ]
            map_expired = bool(self.map_event_until and self.map_event_until <= now)
            if map_expired:
                self.map_event_until = 0.0
            if expired or map_expired:
                for address in expired:
                    self.extra_values.pop(address, None)
                winner = self._winner(now)
                self._set_winner(winner)
                if winner[0] > 0:
                    await self.handler_depth(winner[0])
                else:
                    await self._clear_output()
            elif not self.is_cleared and now > self.to_clear_time:
                await self._clear_output()

    async def set_clear_after(self, seconds):
        self.is_cleared = False
        self.to_clear_time = time.monotonic() + seconds

    @staticmethod
    def generate_wave_100ms(freq, from_, to_):
        if not isinstance(freq, int) or not 0 <= freq <= 255:
            raise ValueError('波形频率必须是 0~255 之间的整数。')
        if not 0 <= from_ <= 1 or not 0 <= to_ <= 1:
            raise ValueError('波形强度必须位于 0~1。')
        from_value = int(100 * from_)
        to_value = int(100 * to_)
        result = [f'{freq:02X}'] * 4
        delta = (to_value - from_value) // 4
        result += [f'{min(max(from_value + delta * index, 0), 100):02X}' for index in range(1, 5)]
        return json.dumps([''.join(result)], separators=(',', ':'))

    async def handler_depth(self, depth):
        strength = min(max(float(depth), 0.0), 1.0)
        if strength > 0:
            self.is_cleared = False
            self.to_clear_time = float('inf')
        else:
            await self.set_clear_after(0.5)
        self.depth_current_strength = strength
        self.current_strength_percentage = strength
        self.is_active = strength > 0
        self._emit_debug_event()
        if self.chatbox_manager:
            self.chatbox_manager.update_channel_mode(
                self.channel, self.current_mode, strength,
                is_active=self.is_active, device_kind=self.device_kind,
            )

    async def depth_background_wave_feeder(self):
        last_strength = 0.0
        while True:
            await asyncio.sleep(self.depth_update_time_window)
            current_strength = self.depth_current_strength
            if current_strength == 0 and self.is_cleared:
                last_strength = 0.0
                continue
            if current_strength == last_strength == 0:
                continue
            source_frame = self.wave_frames[self.wave_frame_index]
            self.wave_frame_index = (self.wave_frame_index + 1) % len(self.wave_frames)
            sent_frame = scale_coyote_frame(source_frame, current_strength)
            wave = json.dumps([sent_frame], separators=(',', ':'))
            last_strength = current_strength
            await self.DG_CONN.broadcast_wave(
                self.channel, wavestr=wave, device_kind=self.device_kind,
            )
            self.last_sent_frame = sent_frame
            self.last_sent_strengths = frame_amplitudes(sent_frame)
            self._emit_debug_event()
