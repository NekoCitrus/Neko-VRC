"""Route each device kind to exactly one transport; retain legacy App APIs."""

import asyncio

import srv
from loguru import logger
from srv.config_manager import DEFAULT_SETTINGS
from srv.connector.bluetooth import BLEHost, scan_hosts


class DeviceRouter:
    def __init__(self, settings):
        self.settings = settings
        self.hosts = {}
        self.paused = False

    def config(self, kind):
        return self.settings.get('connections', DEFAULT_SETTINGS['connections'])[kind]

    async def _app_send(self, method, **kwargs):
        sent = False
        for connection in srv.get_ws_connections():
            if not getattr(connection, 'is_device_ready', lambda *args: True)(kwargs.get('device_kind')):
                continue
            try:
                await getattr(connection, method)(**kwargs)
                sent = True
            except Exception as exc:
                logger.warning('App 输出失败：{}', exc)
        return sent

    async def broadcast_wave(self, channel='A', wavestr='[]', device_kind='coyote'):
        if self.paused:
            return False
        if self.config(device_kind)['mode'] == 'bluetooth':
            host = self.hosts.get(device_kind)
            if host and host.connected:
                host.accept_frame(channel, wavestr)
                return True
            return False
        else:
            return await self._app_send('send_wave', channel=channel, wavestr=wavestr, device_kind=device_kind)

    async def broadcast_clear_wave(self, channel='A', device_kind='coyote'):
        if self.config(device_kind)['mode'] == 'bluetooth':
            host = self.hosts.get(device_kind)
            if host:
                await host.clear(channel)
        else:
            await self._app_send('clear_wave', channel=channel, device_kind=device_kind)

    async def connect(self, kind):
        config = self.config(kind)
        if config['mode'] != 'bluetooth' or not config['address']:
            raise ValueError('先选择蓝牙设备并保存配置。')
        await self.disconnect(kind)
        if any(host.address == config['address'] and host.connected for host in self.hosts.values()):
            raise ValueError('此蓝牙设备已被另一个通道占用。')
        host = BLEHost(kind, config['model'], config['address'], self.settings)
        self.hosts[kind] = host
        try:
            await host.connect()
        except Exception as exc:
            host.error = str(exc)
            raise

    async def disconnect(self, kind):
        host = self.hosts.get(kind)
        if host:
            await host.close()

    async def apply_settings(self, settings):
        previous = self.settings
        for kind, host in tuple(self.hosts.items()):
            old = previous.get('connections', DEFAULT_SETTINGS['connections'])[kind]
            new = settings.get('connections', DEFAULT_SETTINGS['connections'])[kind]
            if old != new:
                await host.close()
                self.hosts.pop(kind, None)
            else:
                await host.apply_settings(settings)
        self.settings = settings

    async def emergency_stop(self):
        self.paused = True
        errors = []
        for kind in ('coyote', 'opossum'):
            for channel in ('A', 'B'):
                try:
                    await self.broadcast_clear_wave(channel, kind)
                except Exception as exc:
                    errors.append(str(exc))
        for connection in srv.get_ws_connections():
            getter = getattr(connection, 'get_device_states', None)
            if getter:
                for state in getter():
                    if self.config(state['device_kind'])['mode'] != 'app':
                        continue
                    for channel in ('A', 'B'):
                        try:
                            await connection.set_strength(channel, value=0, slot_id=state['slot_id'])
                        except Exception as exc:
                            errors.append(str(exc))
            elif getattr(connection, 'bound', False) and self.config('coyote')['mode'] == 'app':
                for channel in ('A', 'B'):
                    try:
                        await connection.set_strength(channel, value=0)
                    except Exception as exc:
                        errors.append(str(exc))
        if errors:
            raise RuntimeError('停止发送；部分设备归零未确认：' + '; '.join(errors))

    async def close(self):
        self.paused = True
        await asyncio.gather(*(host.close() for host in self.hosts.values()), return_exceptions=True)

    def device_states(self):
        result = []
        for connection in srv.get_ws_connections():
            getter = getattr(connection, 'get_device_states', None)
            if getter:
                states = getter()
            elif getattr(connection, 'bound', False):
                from srv.diagnostics import websocket_latency
                states = ({'slot_id': connection.uuid, 'device_kind': 'coyote',
                           'device_type': 'COYOTE_030', 'device_name': '郊狼',
                           'strength': dict(connection.strength), 'strength_max': dict(connection.strength_max),
                           'reported_strength': dict(getattr(connection, 'reported_strength', connection.strength)),
                           'upper_strength': {c: connection.get_upper_strength(c) for c in ('A', 'B')},
                           'communication_latency': websocket_latency(getattr(connection, 'ws_conn', None))},)
            else:
                states = ()
            result.extend({**state, 'transport': 'app', 'connection_uuid': connection.uuid} for state in states
                          if self.config(state['device_kind'])['mode'] == 'app')
        result.extend(host.snapshot() for kind, host in self.hosts.items()
                      if host.connected and self.config(kind)['mode'] == 'bluetooth')
        return tuple(result)

    scan = staticmethod(scan_hosts)
