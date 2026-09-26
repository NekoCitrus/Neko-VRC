import asyncio
import json
import time

from loguru import logger

from srv import WAVEFORM_NAMES

from .base_handler import BaseHandler
from ..connector.coyotev3ws import DGConnection
from ..sps_depth import SPSDepthCalculator, sps_osc_bindings
from ..waveform import load_waveform_frames, scale_coyote_frame


class ShockHandler(BaseHandler):
    """Convert one device-specific SPS depth into one device channel output."""

    def __init__(
        self,
        SETTINGS: dict,
        DG_CONN: DGConnection,
        channel_name: str,
        device_kind: str = 'coyote',
        event_callback=None,
    ) -> None:
        self.SETTINGS = SETTINGS
        self.DG_CONN = DG_CONN
        self.channel = channel_name.upper()
        if device_kind not in ('coyote', 'opossum'):
            raise ValueError(f'Unsupported device kind: {device_kind}')
        self.device_kind = device_kind
        self.shock_settings = SETTINGS['dglab3'][device_kind][f'channel_{channel_name.lower()}']
        self.trigger_type = self.shock_settings['trigger_type']
        self.zone = self.shock_settings['zone']
        self.depth_config = self.shock_settings['depth']
        self.waveform = self.depth_config.get('waveform', WAVEFORM_NAMES[0])
        self.wave_frames = load_waveform_frames(self.waveform)
        self.wave_frame_index = 0
        self.calculators = {}

        self.current_mode = self.trigger_type
        self.is_active = False
        self.current_strength_percentage = 0.0
        self.depth_update_time_window = 0.1
        self.depth_current_strength = 0.0
        self.to_clear_time = 0.0
        self.is_cleared = True
        self.chatbox_manager = None
        self._background_tasks = set()
        self.event_callback = event_callback
        self.last_parameter = ''
        self.last_raw_value = 0.0
        self.active_zone = ''

    def osc_bindings(self):
        return sps_osc_bindings(self.trigger_type, self.zone)

    def set_chatbox_manager(self, chatbox_manager):
        self.chatbox_manager = chatbox_manager

    def get_mode_info(self):
        return {
            'device_kind': self.device_kind,
            'channel': self.channel,
            'mode': self.current_mode,
            'trigger_type': self.trigger_type,
            'zone': self.zone,
            'active_zone': self.active_zone,
            'waveform': self.waveform,
            'is_active': self.is_active,
            'strength_percentage': self.current_strength_percentage,
            'parameter': self.last_parameter,
            'raw_value': self.last_raw_value,
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

    def osc_handler(self, address, signal, *args):
        if isinstance(signal, (list, tuple)):
            signal = signal[0]
        logger.debug(f'VRCOSC: CHANN {self.channel}: {address} [{signal}]: {args}')
        try:
            value = self.param_sanitizer(args)
        except ValueError as exc:
            logger.warning(f'通道 {self.channel} 忽略无效 OSC 参数 {address}: {exc}')
            return None
        self.last_parameter = address
        self.last_raw_value = float(value)
        parts = address.split('/')
        if len(parts) < 7:
            logger.warning(f'Channel {self.channel} ignored malformed SPS address: {address}')
            return None
        zone = parts[5]
        calculator = self.calculators.setdefault(zone, SPSDepthCalculator(self.trigger_type))
        calculator.update(signal, value)
        self.active_zone, depth = max(
            ((zone_id, item.depth) for zone_id, item in self.calculators.items()),
            key=lambda item: item[1],
        )
        if depth <= 0:
            self.active_zone = ''
        self._track_task(self.handler_depth(depth))
        return None

    def reset_for_avatar_change(self):
        """Discard values belonging to the previously active avatar."""
        self.calculators.clear()
        self.active_zone = ''
        self.last_parameter = ''
        self.last_raw_value = 0.0
        self._track_task(self.handler_depth(0.0))

    def _emit_debug_event(self):
        if self.event_callback is None:
            return
        self.event_callback({
            'type': 'channel',
            'device_kind': self.device_kind,
            'channel': self.channel,
            'parameter': self.last_parameter,
            'raw_value': self.last_raw_value,
            'strength_percentage': self.current_strength_percentage,
            'active': self.is_active,
            'active_zone': self.active_zone,
        })

    async def clear_check(self):
        while True:
            await asyncio.sleep(0.05)
            if not self.is_cleared and time.monotonic() > self.to_clear_time:
                self.is_cleared = True
                self.depth_current_strength = 0.0
                self.is_active = False
                self.current_strength_percentage = 0.0
                self._emit_debug_event()
                await self.DG_CONN.broadcast_clear_wave(
                    self.channel, device_kind=self.device_kind,
                )
                logger.info(
                    '{} channel {}, wave cleared after timeout.',
                    self.device_kind,
                    self.channel,
                )

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
            # VRChat emits OSC parameter changes, not a guaranteed heartbeat.
            # Keep a steady insertion active until an explicit zero arrives.
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
                self.channel,
                self.trigger_type,
                strength,
                is_active=self.is_active,
                device_kind=self.device_kind,
            )

    async def depth_background_wave_feeder(self):
        last_strength = 0.0
        while True:
            await asyncio.sleep(self.depth_update_time_window)
            current_strength = self.depth_current_strength
            if current_strength == last_strength == 0:
                continue
            source_frame = self.wave_frames[self.wave_frame_index]
            self.wave_frame_index = (self.wave_frame_index + 1) % len(self.wave_frames)
            wave = json.dumps(
                [scale_coyote_frame(source_frame, current_strength)],
                separators=(',', ':'),
            )
            logger.debug(
                '{} channel {}, SPS depth {:.3f} to {:.3f}, Sending {}',
                self.device_kind,
                self.channel,
                last_strength,
                current_strength,
                wave,
            )
            last_strength = current_strength
            await self.DG_CONN.broadcast_wave(
                self.channel,
                wavestr=wave,
                device_kind=self.device_kind,
            )
