# srv/advanced_chatbox_manager.py
import time
from loguru import logger
from pythonosc.udp_client import SimpleUDPClient

class AdvancedChatboxManager:
    def __init__(self, settings):
        self.enabled = settings.get('chatbox', {}).get('enable', True)
        self.settings = settings.get('chatbox', {})
        self.osc_client = None
        self.update_interval = float(self.settings.get('update_interval', 3.0))
        self.last_update_time = 0.0

        # 即使禁用 Chatbox，也要保留状态容器；ShockHandler 仍可能上报状态。
        self.channel_modes = {
            device_kind: {
                channel: {
                    'mode': 'unknown',
                    'strength_percentage': 0.0,
                    'is_active': False,
                    'last_active_time': 0.0,
                    'duration': 0.0,
                }
                for channel in ('A', 'B')
            }
            for device_kind in ('coyote', 'opossum')
        }

        if self.enabled:
            self.osc_client = SimpleUDPClient(
                self.settings.get('osc_host', '127.0.0.1'),
                int(self.settings.get('osc_port', 9000)),
            )
            if self.settings.get('set_avatar_parameter', True):
                # 可选的自定义 Avatar 参数，不是 /chatbox/input 所必需。
                try:
                    self.osc_client.send_message('/avatar/parameters/ChatboxEnable', 1.0)
                    logger.info('Chatbox功能已启用')
                except Exception as exc:
                    logger.warning(f'启用Chatbox失败: {exc}')

    def update_channel_mode(
        self, channel, mode, strength_percentage, is_active=False, duration=0,
        device_kind='coyote',
    ):
        """更新通道模式信息。"""
        channel = channel.upper()
        if device_kind in self.channel_modes and channel in self.channel_modes[device_kind]:
            channel_info = self.channel_modes[device_kind][channel]
            channel_info['mode'] = mode
            channel_info['strength_percentage'] = strength_percentage
            channel_info['is_active'] = is_active
            channel_info['duration'] = duration
            if is_active:
                channel_info['last_active_time'] = time.monotonic()

    def get_mode_display_name(self, mode):
        """获取模式显示名称。"""
        mode_names = {
            'sps_socket': 'Socket 深度',
            'sps_plug': 'Plug 深度',
            'extra': '额外参数',
            'map': '地图事件',
            'none': '未触发',
            'unknown': '未知模式',
        }
        return mode_names.get(mode, mode)

    def get_activity_indicator(self, channel_info):
        """获取活动状态指示器。"""
        if channel_info['is_active']:
            return '🔴'
        if time.monotonic() - channel_info['last_active_time'] < 5:
            return '🟡'
        return '⚫'

    def _refresh_handler_status(self, shock_handlers):
        if not shock_handlers:
            return
        for handler in shock_handlers:
            if not hasattr(handler, 'get_mode_info'):
                continue
            mode_info = handler.get_mode_info()
            self.update_channel_mode(
                mode_info['channel'],
                mode_info['mode'],
                mode_info['strength_percentage'],
                mode_info['is_active'],
                device_kind=mode_info.get('device_kind', 'coyote'),
            )

    def format_device_status(self, connections, shock_handlers=None):
        """格式化精简设备状态。"""
        if not connections:
            return '设备未连接'

        self._refresh_handler_status(shock_handlers)
        status_lines = []
        for conn in connections:
            getter = getattr(conn, 'get_device_states', None)
            if getter is None:
                status_lines.append(
                    f"郊狼 MAX A:{conn.get_upper_strength('A')} B:{conn.get_upper_strength('B')}"
                )
                continue
            for state in getter():
                label = '负鼠' if state['device_kind'] == 'opossum' else '郊狼'
                upper = state['upper_strength']
                status_lines.append(
                    f"{label} MAX A:{upper.get('A', 0)} B:{upper.get('B', 0)}"
                )
        if not status_lines:
            return 'APP已连接，等待设备'
        if len(status_lines) > 1:
            return '设备状态 - 多设备:\n' + '\n'.join(status_lines)
        return status_lines[0]

    def format_detailed_status(self, connections, shock_handlers=None):
        """格式化详细状态信息。"""
        if not connections:
            return '设备未连接'

        self._refresh_handler_status(shock_handlers)
        detailed_lines = []
        for conn in connections:
            getter = getattr(conn, 'get_device_states', None)
            states = getter() if getter is not None else ({
                'slot_id': conn.uuid[:8],
                'device_kind': 'coyote',
                'strength': conn.strength,
                'upper_strength': {
                    'A': conn.get_upper_strength('A'),
                    'B': conn.get_upper_strength('B'),
                },
            },)
            for state in states:
                label = '负鼠' if state['device_kind'] == 'opossum' else '郊狼'
                channel_modes = self.channel_modes[state['device_kind']]
                mode_a = self.get_mode_display_name(channel_modes['A']['mode'])
                mode_b = self.get_mode_display_name(channel_modes['B']['mode'])
                strength_pct_a = int(channel_modes['A']['strength_percentage'] * 100)
                strength_pct_b = int(channel_modes['B']['strength_percentage'] * 100)
                activity_a = self.get_activity_indicator(channel_modes['A'])
                activity_b = self.get_activity_indicator(channel_modes['B'])
                strength = state['strength']
                upper = state['upper_strength']
                detailed_lines.append(
                    f"{label} {state['slot_id'][:8]}:\n"
                    f"A: {strength.get('A', 0)}/{upper.get('A', 0)} "
                    f"({strength_pct_a}%) {mode_a}{activity_a}\n"
                    f"B: {strength.get('B', 0)}/{upper.get('B', 0)} "
                    f"({strength_pct_b}%) {mode_b}{activity_b}"
                )
        if not detailed_lines:
            return 'APP已连接，等待设备'
        return '\n\n'.join(detailed_lines)

    async def update_chatbox(self, connections, shock_handlers=None, detailed=False):
        """按配置的间隔更新 Chatbox。"""
        if not self.enabled or self.osc_client is None:
            return
        current_time = time.monotonic()
        if current_time - self.last_update_time < self.update_interval:
            return
        self.last_update_time = current_time
        status_text = (
            self.format_detailed_status(connections, shock_handlers)
            if detailed
            else self.format_device_status(connections, shock_handlers)
        )
        try:
            self.osc_client.send_message('/chatbox/input', [status_text, True, False])
            logger.debug(f'Chatbox更新: {status_text}')
        except Exception as exc:
            logger.warning(f'Chatbox更新失败: {exc}')

    def send_custom_message(self, message):
        """发送自定义消息到 Chatbox。"""
        if not self.enabled or self.osc_client is None:
            return
        try:
            self.osc_client.send_message('/chatbox/input', [str(message), True, False])
            logger.debug(f'Chatbox自定义消息: {message}')
        except Exception as exc:
            logger.warning(f'Chatbox自定义消息发送失败: {exc}')

    def cleanup(self, notify=True):
        """清理 Chatbox 状态。"""
        if not self.enabled or self.osc_client is None:
            return
        try:
            if notify:
                self.send_custom_message('设备已断开')
            if notify and self.settings.get('set_avatar_parameter', True):
                self.osc_client.send_message('/avatar/parameters/ChatboxEnable', 0.0)
            logger.info('Chatbox功能已清理')
        except Exception as exc:
            logger.warning(f'Chatbox清理失败: {exc}')
        finally:
            # python-osc 1.9.x does not expose close(), but owns a UDP socket.
            osc_socket = getattr(self.osc_client, '_sock', None)
            if osc_socket is not None:
                osc_socket.close()
            self.osc_client = None
