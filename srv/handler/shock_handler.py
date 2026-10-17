import asyncio
import json
import math
import time

from loguru import logger

from srv import WAVEFORM_NAMES_BY_DEVICE
from ..config_manager import default_trigger_switches

from .base_handler import BaseHandler
from ..connector.coyotev3ws import DGConnection
from ..sps_depth import (
    LMS_PROXIMITY_ADDRESS, PCS_PROXIMITY_ADDRESS, OGB_PARAMETER_PREFIX,
    SPSDepthCalculator, SPS_PLUG, SPS_SOCKET, has_socket_depth_parameters,
    has_plug_depth_parameters, sps_osc_bindings,
)
from ..waveform import frame_amplitudes, load_waveform_frames, scale_coyote_frame


SOURCE_EXTRA = 'extra'
SOURCE_MAP = 'map'
SOURCE_LMS = 'lms'
SOURCE_PCS = 'pcs'
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
        self.lms_value = None
        self.pcs_value = None
        self.avatar_parameter_paths = None
        self.query_input_expiries = {}
        self.udp_input_times = {}
        self.input_reset_time = 0.0
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
        for field, default in default_trigger_switches(self.device_kind).items():
            setattr(self, field, channel_settings.get(field, default))
        self.depth_config = channel_settings['depth']
        default_waveform = WAVEFORM_NAMES_BY_DEVICE[self.device_kind][0]
        self.waveform = self.depth_config.get('waveform', default_waveform)
        self.wave_frames = load_waveform_frames(self.waveform, self.device_kind, self.SETTINGS)
        self.wave_frame_index = 0

    def osc_bindings(self):
        bindings = []
        for trigger_type, zone, needed in (
            (SPS_SOCKET, self.socket_zone, self.socket_enabled or self.pcs_enabled),
            (SPS_PLUG, self.plug_zone, self.plug_enabled or self.lms_enabled),
        ):
            if not needed:
                continue
            bindings.extend(
                (address, (trigger_type, signal))
                for address, signal in sps_osc_bindings(trigger_type, zone)
            )
        if self.extra_config['enabled']:
            bindings.extend(
                (path, (SOURCE_EXTRA, 'value')) for path in self.extra_config['paths']
                if path not in (LMS_PROXIMITY_ADDRESS, PCS_PROXIMITY_ADDRESS)
            )
        if self.pcs_enabled:
            bindings.append((PCS_PROXIMITY_ADDRESS, (SOURCE_PCS, 'value')))
        if self.lms_enabled:
            bindings.append((LMS_PROXIMITY_ADDRESS, (SOURCE_LMS, 'value')))
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
            'socket_depth_available': self.socket_depth_available,
            'parameter_hint': self.parameter_hint,
            'parameter_availability': self.parameter_availability,
            'enabled_sources': {
                'socket': self.socket_enabled, 'plug': self.plug_enabled,
                'extra_map': self.extra_config['enabled'],
                'pcs': self.pcs_enabled, 'lms': self.lms_enabled,
            },
        }

    @property
    def socket_depth_available(self):
        paths = set()
        # Actual validated OSC/OSCQuery values are evidence, even before discovery
        # completes or if an avatar unlocks between OSCQuery polls.
        for (trigger_type, zone), calculator in tuple(self.calculators.items()):
            if trigger_type == SPS_SOCKET:
                paths.update(f'{OGB_PARAMETER_PREFIX}Orf/{zone}/{signal}'
                             for signal in tuple(calculator.values))
        return has_socket_depth_parameters(paths, self.socket_zone)

    @property
    def parameter_availability(self):
        paths = self.avatar_parameter_paths or ()
        return {
            'known': self.avatar_parameter_paths is not None,
            'socket': has_socket_depth_parameters(paths, self.socket_zone),
            'plug': has_plug_depth_parameters(paths, self.plug_zone),
            'pcs': PCS_PROXIMITY_ADDRESS in paths,
            'lms': LMS_PROXIMITY_ADDRESS in paths,
        }

    @property
    def parameter_hint(self):
        available = self.parameter_availability
        if not available['known']:
            return '等待读取模型 OSC 参数'
        if not any(available[name] for name in ('socket', 'plug', 'pcs', 'lms')):
            return '无可用的 Socket、Plug、PCS、LMS 参数'
        missing = []
        if self.socket_enabled and not available['socket']:
            missing.append('Socket')
        if self.plug_enabled and not available['plug']:
            missing.append('Plug')
        if not missing:
            return ''
        fallbacks = []
        for name, enabled in (('pcs', self.pcs_enabled), ('lms', self.lms_enabled)):
            if available[name]:
                fallbacks.append(f"{name.upper()} 回退已启用" if enabled else f"可启用 {name.upper()} 回退")
        return '无 ' + '/'.join(missing) + ' 深度；' + ('，'.join(fallbacks) or '无 PCS/LMS 回退参数')

    def set_avatar_parameters(self, parameter_paths):
        paths = None if parameter_paths is None else frozenset(parameter_paths)
        if paths == self.avatar_parameter_paths:
            return
        self.avatar_parameter_paths = paths
        # A capability change must not resurrect a previously gated fallback.
        # Fresh PCS/LMS readings can re-arm only their own source.
        self.pcs_value = None
        self.lms_value = None
        if paths is not None:
            self.query_input_expiries = {path: expiry for path, expiry in self.query_input_expiries.items() if path in paths}
            self.udp_input_times = {path: timestamp for path, timestamp in self.udp_input_times.items() if path in paths}
            for key, calculator in tuple(self.calculators.items()):
                trigger_type, zone = key
                ogb_kind = 'Orf' if trigger_type == SPS_SOCKET else 'Pen'
                for signal in tuple(calculator.values):
                    if f'{OGB_PARAMETER_PREFIX}{ogb_kind}/{zone}/{signal}' not in paths:
                        calculator.forget(signal)
                if trigger_type == SPS_SOCKET:
                    calculator.has_close_gate = f'{OGB_PARAMETER_PREFIX}Orf/{zone}/PenOthersClose' in paths
                if not calculator.values:
                    self.calculators.pop(key)
                    self.calculator_inputs.pop(key, None)
                elif self.calculator_inputs.get(key, ('', 0))[0] not in paths:
                    signal = next(reversed(calculator.values))
                    self.calculator_inputs[key] = (
                        f'{OGB_PARAMETER_PREFIX}{ogb_kind}/{zone}/{signal}', calculator.values[signal],
                    )
            self.extra_values = {path: value for path, value in self.extra_values.items() if path in paths}
        winner = self._winner()
        self._set_winner(winner)
        self._track_task(self._apply_current_winner())

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
        return self._receive_value(address, binding, args)

    def receive_query_value(self, address, binding, value, observed_at):
        # Python 3.10's Windows monotonic clock is too coarse to order a fast
        # hot-save against an in-flight HTTP request. Use the high-resolution
        # performance clock for input ordering, keeping output timers separate.
        now = time.perf_counter()
        if (not math.isfinite(observed_at) or observed_at < self.input_reset_time
                or not 0 <= now - observed_at < EXTRA_PARAMETER_TIMEOUT
                or self.udp_input_times.get(address, -1) >= observed_at):
            return
        self._receive_value(address, binding, (value,), observed_at)

    def _receive_value(self, address, binding, args, query_observed_at=None):
        started = time.perf_counter()
        try:
            source, signal = self._unwrap_binding(binding)
            value = self.param_sanitizer(args)
        except ValueError as exc:
            logger.warning(f'通道 {self.channel} 忽略无效 OSC 参数 {address}: {exc}')
            return None
        raw_value = float(value)
        if not math.isfinite(raw_value):
            return None
        now = time.monotonic()
        expiry = now + EXTRA_PARAMETER_TIMEOUT
        if query_observed_at is not None:
            expiry -= time.perf_counter() - query_observed_at
        if address == PCS_PROXIMITY_ADDRESS:
            if not self.pcs_enabled:
                return None
            self.pcs_value = (
                self._distance_depth(raw_value),
                expiry, raw_value,
            )
        elif address == LMS_PROXIMITY_ADDRESS:
            if not self.lms_enabled:
                return None
            # Route even overlapping user wildcard bindings through this gate.
            self.lms_value = (
                self._distance_depth(raw_value),
                expiry, raw_value,
            )
        elif source in (SOURCE_LMS, SOURCE_PCS):
            return None
        elif source == SOURCE_EXTRA:
            if not self.extra_config['enabled']:
                return None
            if ((address.startswith(f'{OGB_PARAMETER_PREFIX}Orf/') and not self.socket_enabled)
                    or (address.startswith(f'{OGB_PARAMETER_PREFIX}Pen/') and not self.plug_enabled)):
                return None
            self.extra_values[address] = (
                self._distance_depth(raw_value), expiry, raw_value,
            )
        else:
            if ((source == SPS_SOCKET and not (self.socket_enabled or self.pcs_enabled))
                    or (source == SPS_PLUG and not (self.plug_enabled or self.lms_enabled))):
                return None
            parts = address.split('/')
            if len(parts) < 7:
                logger.warning(f'Channel {self.channel} ignored malformed SPS address: {address}')
                return None
            zone = parts[5]
            calculator = self.calculators.setdefault(
                (source, zone), SPSDepthCalculator(source),
            )
            if source == SPS_SOCKET and self.avatar_parameter_paths is not None:
                calculator.has_close_gate = f'{OGB_PARAMETER_PREFIX}Orf/{zone}/PenOthersClose' in self.avatar_parameter_paths
            calculator.update(signal, raw_value)
            self.calculator_inputs[(source, zone)] = (address, raw_value)
        if query_observed_at is None:
            self.udp_input_times[address] = time.perf_counter()
            self.query_input_expiries.pop(address, None)
        else:
            self.query_input_expiries[address] = expiry
        winner = self._winner()
        self._set_winner(winner)
        self._track_task(self._apply_current_winner())
        from srv.diagnostics import record_latency
        record_latency('osc.compute_winner', started)
        return None

    def _distance_depth(self, raw_value):
        bottom = float(self.extra_config['bottom'])
        top = float(self.extra_config['top'])
        return min(max((raw_value - bottom) / (top - bottom), 0.0), 1.0)

    def _winner(self, now=None):
        now = time.monotonic() if now is None else now
        candidates = []
        if self.extra_config['enabled'] and self.map_event_until > now:
            candidates.append((1.0, SOURCE_MAP, '', '地图事件', 1.0))
        for (trigger_type, zone), calculator in self.calculators.items():
            if ((trigger_type == SPS_SOCKET and not self.socket_enabled)
                    or (trigger_type == SPS_PLUG and not self.plug_enabled)):
                continue
            address, raw_value = self.calculator_inputs.get(
                (trigger_type, zone), ('', 0.0),
            )
            candidates.append((calculator.depth, trigger_type, zone, address, raw_value))
        candidates.extend(
            (depth, SOURCE_EXTRA, '', address, raw_value)
            for address, (depth, expiry, raw_value) in self.extra_values.items()
            if self.extra_config['enabled'] and expiry > now
        )
        plug_active = any(
            calculator.depth > 0
            for (trigger_type, _zone), calculator in self.calculators.items()
            if trigger_type == SPS_PLUG
        )
        if self.lms_enabled and not plug_active and self.lms_value is not None:
            depth, expiry, raw_value = self.lms_value
            if expiry > now:
                candidates.append((depth, SOURCE_LMS, '', LMS_PROXIMITY_ADDRESS, raw_value))
        # PCS is an independent distance input, not gated by socket availability.
        if self.pcs_enabled and self.pcs_value is not None:
            depth, expiry, raw_value = self.pcs_value
            if expiry > now:
                candidates.append((depth, SOURCE_PCS, '', PCS_PROXIMITY_ADDRESS, raw_value))
        return max(candidates, key=lambda item: item[0]) if candidates else (0.0, '', '', '', 0.0)

    async def _apply_current_winner(self):
        # A queued OSC update can run after hot reconfiguration. Never replay
        # its captured strength; evaluate the current switches/inputs instead.
        winner = self._winner()
        self._set_winner(winner)
        await self.handler_depth(winner[0])

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
        self.lms_value = None
        self.pcs_value = None
        self.query_input_expiries.clear()
        self.udp_input_times.clear()
        self.input_reset_time = time.perf_counter()
        self.map_event_until = 0.0
        self._load_settings(channel_settings)
        self._set_winner((0.0, '', '', '', 0.0))
        await self._clear_output()

    async def hot_update(self, channel_settings):
        """Invalidate only the inputs whose meaning/routing actually changed."""
        old = self.shock_settings
        index, waveform = self.wave_frame_index, self.waveform
        old_extra = self.extra_config
        self._load_settings(channel_settings)
        if waveform == self.waveform:
            self.wave_frame_index = index
        for kind, zone_key, needed in (
            (SPS_SOCKET, 'socket_zone', self.socket_enabled or self.pcs_enabled),
            (SPS_PLUG, 'plug_zone', self.plug_enabled or self.lms_enabled),
        ):
            if old[zone_key] != channel_settings[zone_key] or not needed:
                for key in tuple(self.calculators):
                    if key[0] == kind:
                        self.calculators.pop(key, None)
                        self.calculator_inputs.pop(key, None)
        range_changed = any(old_extra[key] != self.extra_config[key] for key in ('bottom', 'top'))
        if old_extra != self.extra_config:
            self.extra_values.clear()
        if not self.extra_config['enabled']:
            self.map_event_until = 0
        if range_changed or old.get('pcs_enabled') != channel_settings.get('pcs_enabled'):
            self.pcs_value = None
        if range_changed or old.get('lms_enabled') != channel_settings.get('lms_enabled'):
            self.lms_value = None
        winner = self._winner()
        self._set_winner(winner)
        if winner[0] > 0:
            await self.handler_depth(winner[0])
        else:
            await self._clear_output()

    def reset_for_avatar_change(self, observed_at=None):
        self.calculators.clear()
        self.calculator_inputs.clear()
        self.extra_values.clear()
        self.lms_value = None
        self.pcs_value = None
        self.query_input_expiries.clear()
        self.udp_input_times.clear()
        # A coherent new-avatar snapshot may seed its own values, but cannot
        # replay a request predating a newer hot-save/resume/reset boundary.
        self.input_reset_time = (time.perf_counter() if observed_at is None
                                 else max(self.input_reset_time, observed_at))
        self.avatar_parameter_paths = None
        self.map_event_until = 0.0
        self._set_winner((0.0, '', '', '', 0.0))
        self._track_task(self._apply_current_winner())

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
            query_expired = [path for path, expiry in self.query_input_expiries.items() if expiry <= now]
            for path in query_expired:
                self.query_input_expiries.pop(path, None)
                parts = path.strip('/').split('/')
                if len(parts) == 6 and parts[:3] == ['avatar', 'parameters', 'OGB']:
                    kind = SPS_SOCKET if parts[3] == 'Orf' else SPS_PLUG
                    calculator = self.calculators.get((kind, parts[4]))
                    if calculator is not None:
                        calculator.forget(parts[5])
            expired = [
                address for address, (_depth, expiry, _raw) in self.extra_values.items()
                if expiry <= now
            ]
            map_expired = bool(self.map_event_until and self.map_event_until <= now)
            lms_expired = self.lms_value is not None and self.lms_value[1] <= now
            if lms_expired:
                self.lms_value = None
            pcs_expired = self.pcs_value is not None and self.pcs_value[1] <= now
            if pcs_expired:
                self.pcs_value = None
            if map_expired:
                self.map_event_until = 0.0
            if expired or map_expired or lms_expired or pcs_expired or query_expired:
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
            if self.active_source == SOURCE_MAP:
                # Legacy map events are a steady envelope, independent of the
                # selected SPS waveform. Keep the preset phase for resumption.
                source_frame = '0A0A0A0A64646464'
            else:
                source_frame = self.wave_frames[self.wave_frame_index]
                self.wave_frame_index = (self.wave_frame_index + 1) % len(self.wave_frames)
            sent_frame = scale_coyote_frame(source_frame, current_strength)
            wave = json.dumps([sent_frame], separators=(',', ':'))
            last_strength = current_strength
            started = time.perf_counter()
            delivered = await self.DG_CONN.broadcast_wave(
                self.channel, wavestr=wave, device_kind=self.device_kind,
            )
            from srv.diagnostics import record_latency
            record_latency(f'output.{self.device_kind}.enqueue_or_send', started)
            if delivered is False:
                continue
            self.last_sent_frame = sent_frame
            self.last_sent_strengths = frame_amplitudes(sent_frame)
            self._emit_debug_event()
