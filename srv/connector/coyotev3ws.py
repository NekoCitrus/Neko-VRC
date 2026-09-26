import asyncio
import json
import traceback

from loguru import logger
from websockets.asyncio.server import ServerConnection

from srv import DEFAULT_WAVE, add_ws_connection, get_ws_connections, remove_ws_connection


class DGWSMessage:
    HEARTBEAT = json.dumps({'type': 'heartbeat', 'clientId': '', 'targetId': '', 'message': '200'})

    def __init__(self, type, clientId='', targetId='', message='') -> None:
        self.type = type
        self.clientId = clientId
        self.targetId = targetId
        self.message = message

    def __str__(self) -> str:
        return json.dumps({
            'type': self.type,
            'clientId': self.clientId,
            'targetId': self.targetId,
            'message': str(self.message),
        })

    async def send(self, conn):
        await conn.send_text(str(self))


class DGConnection:
    protocol_version = 'v3'

    def __init__(self, ws_connection: ServerConnection, client_uuid=None, SETTINGS: dict = None) -> None:
        if SETTINGS is None:
            raise ValueError('DGConnection SETTINGS not provided.')

        self.ws_conn = ws_connection
        self.uuid = str(client_uuid if client_uuid is not None else ws_connection.id)
        self.SETTINGS = SETTINGS
        self.master_uuid = SETTINGS['ws']['master_uuid']
        self.strength = {'A': 0, 'B': 0}
        self.strength_max = {'A': 0, 'B': 0}
        self.strength_limit = {
            'A': SETTINGS['dglab3']['coyote']['channel_a']['strength_limit'],
            'B': SETTINGS['dglab3']['coyote']['channel_b']['strength_limit'],
        }
        self.bound = False
        # websockets 不允许并发 send；所有波形、心跳和强度更新共用一把锁。
        self._send_lock = asyncio.Lock()
        add_ws_connection(self)

    def __str__(self):
        return f'<DGConnection (id:{self.uuid}, {self.strength}, max {self.strength_max})>'

    def is_device_ready(self, device_kind=None):
        return self.bound and device_kind in (None, 'coyote')

    async def send_text(self, message):
        logger.debug('ID {}, SENDING {}', self.uuid, message)
        async with self._send_lock:
            await self.ws_conn.send(message)

    async def msg_handler(self, msg: DGWSMessage):
        if msg.type == 'bind':
            if msg.targetId != self.uuid:
                raise ValueError('UUID mismatch.')
            if msg.clientId != self.master_uuid:
                raise ValueError('Binding to unknown uuid.')
            await DGWSMessage('bind', clientId=msg.clientId, targetId=msg.targetId, message='200').send(self)
            self.bound = True
            return

        if msg.type == 'msg':
            if msg.message.startswith('strength-'):
                raw_values = msg.message[len('strength-'):].split('+')
                if len(raw_values) != 4:
                    raise ValueError('Invalid strength message.')
                values = tuple(map(int, raw_values))
                if any(value < 0 or value > 200 for value in values):
                    raise ValueError('Strength values must be between 0 and 200.')
                self.strength['A'], self.strength['B'], self.strength_max['A'], self.strength_max['B'] = values
                for channel in ('A', 'B'):
                    limit = self.get_upper_strength(channel)
                    if self.strength[channel] not in (0, limit):
                        await self.set_strength(channel, value=limit)
            elif msg.message.startswith('feedback-'):
                logger.success(f'ID {self.uuid}, {msg.message}')
            else:
                logger.error(f'ID {self.uuid}, unknown msg {msg.message}')
            return

        if msg.type == 'heartbeat':
            logger.info(f'ID {self.uuid}, RECV HB')
            return
        raise ValueError(f'Unknown message type: {msg.type}')

    @staticmethod
    def _validate_channel(channel):
        channel = str(channel).upper()
        if channel not in ('A', 'B'):
            raise ValueError(f'Invalid channel: {channel}')
        return channel

    def get_upper_strength(self, channel='A'):
        channel = self._validate_channel(channel)
        return min(self.strength_max[channel], self.strength_limit[channel])

    async def set_strength(self, channel='A', mode='2', value=0, force=False):
        channel = self._validate_channel(channel)
        value = int(value)
        if not force:
            if value < 0 or value > 200:
                raise ValueError('Strength must be between 0 and 200.')
            limit = self.get_upper_strength(channel)
            if value > limit and mode == '2':
                logger.warning(f'ID {self.uuid}, strength {value} exceeds limit {limit}; clamped.')
                value = limit
        if mode == '2':
            self.strength[channel] = value
        logger.info(f'Channel {channel}, set strength, mode {mode}, value {value}.')
        await DGWSMessage(
            'msg', self.master_uuid, self.uuid,
            f"strength-{'1' if channel == 'A' else '2'}+{mode}+{value}",
        ).send(self)

    async def set_strength_0_to_1(self, channel='A', value=0):
        value = float(value)
        if value < 0 or value > 1:
            raise ValueError('Normalized strength must be between 0 and 1.')
        channel = self._validate_channel(channel)
        await self.set_strength(channel=channel, mode='2', value=int(self.get_upper_strength(channel) * value))

    async def send_wave(self, channel='A', wavestr=DEFAULT_WAVE, device_kind='coyote'):
        if device_kind != 'coyote':
            return
        channel = self._validate_channel(channel)
        await DGWSMessage('msg', self.master_uuid, self.uuid, f'pulse-{channel}:{wavestr}').send(self)

    async def clear_wave(self, channel='A', device_kind='coyote'):
        if device_kind != 'coyote':
            return
        channel = self._validate_channel(channel)
        device_channel = '1' if channel == 'A' else '2'
        await DGWSMessage('msg', self.master_uuid, self.uuid, f'clear-{device_channel}').send(self)

    async def send_err(self, error_type='error', message='500'):
        await DGWSMessage(error_type, self.master_uuid, self.uuid, message).send(self)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(60)
            logger.info(f'ID {self.uuid}, Sending HB.')
            await self.send_text(DGWSMessage.HEARTBEAT)

    async def connection_init(self):
        await asyncio.sleep(2)
        await self.set_strength('A', value=1, force=True)
        await self.set_strength('B', value=1, force=True)

    async def serve(self):
        logger.info(f'New WS conn, id {self.uuid}.')
        heartbeat_task = None
        init_task = None
        try:
            await DGWSMessage('bind', clientId=self.uuid, targetId='', message='targetId').send(self)
            init_task = asyncio.create_task(self.connection_init())
            heartbeat_task = asyncio.create_task(self.heartbeat())
            async for message in self.ws_conn:
                logger.debug(f'WSID {self.uuid}, RECVMSG {message}.')
                try:
                    event = json.loads(message)
                    if not isinstance(event, dict):
                        raise ValueError('WebSocket message must be a JSON object.')
                    await self.msg_handler(DGWSMessage(**event))
                except Exception:
                    logger.error(traceback.format_exc())
                    try:
                        await self.send_err()
                    except Exception:
                        logger.debug('Failed to return WebSocket error response.')
        finally:
            logger.warning(f'ID {self.uuid} CLOSED.')
            for task in (heartbeat_task, init_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (heartbeat_task, init_task) if task is not None),
                return_exceptions=True,
            )
            remove_ws_connection(self)

    @staticmethod
    async def _broadcast(method_name, *args, **kwargs):
        connections = get_ws_connections()
        if not connections:
            return
        if len(connections) == 1:
            connection = connections[0]
            try:
                await getattr(connection, method_name)(*args, **kwargs)
            except Exception as exc:
                logger.warning(f'Broadcast to {connection.uuid} failed: {exc}')
            return
        results = await asyncio.gather(
            *(getattr(conn, method_name)(*args, **kwargs) for conn in connections),
            return_exceptions=True,
        )
        for conn, result in zip(connections, results):
            if isinstance(result, Exception):
                logger.warning(f'Broadcast to {conn.uuid} failed: {result}')

    @classmethod
    async def broadcast_wave(cls, channel='A', wavestr=DEFAULT_WAVE, device_kind='coyote'):
        await cls._broadcast(
            'send_wave', channel=channel, wavestr=wavestr, device_kind=device_kind,
        )

    @classmethod
    async def broadcast_clear_wave(cls, channel='A', device_kind='coyote'):
        await cls._broadcast('clear_wave', channel=channel, device_kind=device_kind)

    @classmethod
    async def broadcast_strength_0_to_1(cls, channel='A', value=0):
        await cls._broadcast('set_strength_0_to_1', channel=channel, value=value)
