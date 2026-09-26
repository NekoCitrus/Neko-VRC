import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, build_opener
from urllib.parse import unquote

from loguru import logger
from zeroconf import ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf

from .sps_depth import sps_osc_bindings


OSCQUERY_SERVICE_TYPE = '_oscjson._tcp.local.'


@dataclass(frozen=True)
class OSCQueryAvatarSnapshot:
    avatar_id: str
    parameter_paths: tuple[str, ...]
    endpoint: str = ''


def _node_member(node, name, default=None):
    if not isinstance(node, dict):
        return default
    return node.get(name, node.get(name.lower(), default))


def parse_vrchat_avatar_node(document, endpoint=''):
    """Extract the current avatar ID and parameter paths from VRChat's /avatar node."""
    contents = _node_member(document, 'CONTENTS', {})
    if not isinstance(contents, dict) or 'parameters' not in contents:
        return None
    change = contents.get('change', {})
    value = _node_member(change, 'VALUE', ())
    if not isinstance(value, (list, tuple)) or not value:
        return None
    avatar_id = str(value[0]).strip()
    if not avatar_id.startswith('avtr_'):
        return None

    paths = set()

    def walk(node):
        full_path = _node_member(node, 'FULL_PATH')
        if isinstance(full_path, str) and full_path.startswith('/avatar/parameters/'):
            paths.add(full_path)
        children = _node_member(node, 'CONTENTS', {})
        if isinstance(children, dict):
            for child in children.values():
                walk(child)

    walk(contents['parameters'])
    return OSCQueryAvatarSnapshot(
        avatar_id=avatar_id,
        parameter_paths=tuple(sorted(paths)),
        endpoint=endpoint,
    )


class VRChatOSCQueryClient:
    """Discover VRChat's OSCQuery server and keep its current avatar snapshot fresh."""

    def __init__(self, zeroconf, callback, poll_interval=3.0, request_timeout=1.5):
        self.zeroconf = zeroconf
        self.callback = callback
        self.poll_interval = float(poll_interval)
        self.request_timeout = float(request_timeout)
        self.browser = None
        self.thread = None
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.lock = threading.Lock()
        self.service_names = set()
        self.fallback_avatar_id = None
        self.last_fingerprint = None
        self.opener = build_opener(ProxyHandler({}))

    def start(self):
        self.stop_event.clear()
        self.browser = ServiceBrowser(
            self.zeroconf,
            OSCQUERY_SERVICE_TYPE,
            handlers=[self._service_changed],
        )
        self.thread = threading.Thread(
            target=self._poll,
            daemon=True,
            name='vrchat-oscquery-client',
        )
        self.thread.start()
        self.refresh_event.set()

    def _service_changed(self, _zeroconf, _service_type, name, state_change):
        with self.lock:
            if state_change is ServiceStateChange.Removed:
                self.service_names.discard(name)
            else:
                self.service_names.add(name)
        self.refresh_event.set()

    def request_refresh(self, fallback_avatar_id=None):
        if isinstance(fallback_avatar_id, str) and fallback_avatar_id.startswith('avtr_'):
            with self.lock:
                self.fallback_avatar_id = fallback_avatar_id
        self.refresh_event.set()

    def _fetch(self, address, port):
        host = f'[{address}]' if ':' in address and not address.startswith('[') else address
        endpoint = f'http://{host}:{port}'
        try:
            with self.opener.open(f'{endpoint}/avatar', timeout=self.request_timeout) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise ValueError('OSCQuery /avatar 响应超过 4 MiB。')
            return parse_vrchat_avatar_node(json.loads(payload.decode('utf-8')), endpoint)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.debug('OSCQuery 查询 {} 失败：{}', endpoint, exc)
            return None

    def _query_services(self):
        with self.lock:
            names = tuple(self.service_names)
        for name in names:
            try:
                info = self.zeroconf.get_service_info(
                    OSCQUERY_SERVICE_TYPE,
                    name,
                    timeout=max(100, int(self.request_timeout * 1000)),
                )
            except Exception as exc:
                logger.debug('读取 OSCQuery 服务 {} 失败：{}', name, exc)
                continue
            if info is None:
                continue
            for address in info.parsed_addresses():
                snapshot = self._fetch(address, info.port)
                if snapshot is not None:
                    return snapshot
        return None

    def _publish(self, snapshot):
        fingerprint = (snapshot.avatar_id, snapshot.parameter_paths)
        if fingerprint == self.last_fingerprint:
            return
        self.last_fingerprint = fingerprint
        try:
            self.callback(snapshot)
        except Exception:
            logger.exception('处理 VRChat OSCQuery Avatar 信息失败。')

    def _poll(self):
        while not self.stop_event.is_set():
            self.refresh_event.wait(self.poll_interval)
            self.refresh_event.clear()
            if self.stop_event.is_set():
                break
            snapshot = self._query_services()
            if snapshot is not None:
                with self.lock:
                    self.fallback_avatar_id = None
                self._publish(snapshot)
                continue
            with self.lock:
                fallback_avatar_id = self.fallback_avatar_id
                self.fallback_avatar_id = None
            if fallback_avatar_id:
                self._publish(OSCQueryAvatarSnapshot(fallback_avatar_id, ()))

    def stop(self):
        self.stop_event.set()
        self.refresh_event.set()
        if self.browser is not None:
            self.browser.cancel()
            self.browser = None
        if self.thread is not None:
            self.thread.join(timeout=self.request_timeout + 1)
            self.thread = None


class _OSCQueryHandler(BaseHTTPRequestHandler):
    service = None

    def log_message(self, format, *args):
        return

    def do_GET(self):
        path, _, query = self.path.partition('?')
        path = unquote(path)
        if query == 'HOST_INFO':
            payload = {
                'NAME': self.service.name,
                'OSC_IP': self.service.osc_host,
                'OSC_PORT': self.service.osc_port,
                'OSC_TRANSPORT': 'UDP',
            }
        elif path in ('', '/'):
            payload = self.service.root_node()
        else:
            payload = self.service.node_for(path)
            if payload is None:
                self.send_error(404)
                return
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OSCQueryService:
    def __init__(self, settings, avatar_callback=None):
        self.settings = settings
        self.name = 'Neko-VRC'
        self.osc_host = settings['osc']['listen_host']
        self.osc_port = int(settings['osc']['listen_port'])
        self.http_host = settings.get('oscquery', {}).get('listen_host', '127.0.0.1')
        self.http_port = int(settings.get('oscquery', {}).get('listen_port', 8801))
        self.http = None
        self.http_thread = None
        self.zeroconf = None
        self.service_info = None
        self.register_thread = None
        self.service_registered = False
        self.stopping = False
        self.avatar_callback = avatar_callback
        self.vrchat_client = None

    def _paths(self):
        paths = set()
        for device_kind in ('coyote', 'opossum'):
            for channel in ('channel_a', 'channel_b'):
                config = self.settings['dglab3'][device_kind][channel]
                paths.update(
                    address
                    for address, _signal in sps_osc_bindings(
                        config['trigger_type'],
                        config['zone'],
                    )
                )
        return sorted(path for path in paths if path.startswith('/avatar/'))

    def root_node(self):
        contents = {'avatar': {'FULL_PATH': '/avatar', 'CONTENTS': {}}}
        for path in self._paths():
            node = contents['avatar']['CONTENTS']
            parts = path.strip('/').split('/')[1:]
            current = '/avatar'
            for index, part in enumerate(parts):
                current += '/' + part
                child = node.setdefault(part, {'FULL_PATH': current})
                if index == len(parts) - 1:
                    child.update({'TYPE': 'f', 'ACCESS': 2})
                else:
                    node = child.setdefault('CONTENTS', {})
        return {'FULL_PATH': '/', 'CONTENTS': contents}

    def node_for(self, path):
        path = '/' + path.strip('/')
        root = self.root_node()
        if path == '/':
            return root

        node = root
        for part in path.strip('/').split('/'):
            node = node.get('CONTENTS', {}).get(part)
            if node is None:
                break
        if node is not None:
            return node

        requested_parts = path.strip('/').split('/')
        for pattern in self._paths():
            pattern_parts = pattern.strip('/').split('/')
            if len(pattern_parts) == len(requested_parts) and all(
                expected == '*' or expected == actual
                for expected, actual in zip(pattern_parts, requested_parts)
            ):
                return {'FULL_PATH': path, 'TYPE': 'f', 'ACCESS': 2}
        return None

    def start(self):
        self.stopping = False
        self.service_registered = False
        handler = type('OSCQueryRequestHandler', (_OSCQueryHandler,), {})
        handler.service = self
        self.http = ThreadingHTTPServer((self.http_host, self.http_port), handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        address = self.http_host if self.http_host not in ('0.0.0.0', '::') else '127.0.0.1'
        self.zeroconf = Zeroconf()
        self.service_info = ServiceInfo(
            OSCQUERY_SERVICE_TYPE,
            f'{self.name}.{OSCQUERY_SERVICE_TYPE}',
            addresses=[__import__('ipaddress').ip_address(address).packed],
            port=self.http_port,
            properties={b'txtvers': b'1'},
        )
        zeroconf = self.zeroconf
        service_info = self.service_info

        def register():
            try:
                zeroconf.register_service(service_info, allow_name_change=True)
                if self.zeroconf is zeroconf:
                    self.service_registered = True
            except Exception as exc:
                if not self.stopping:
                    logger.warning(f'OSCQuery mDNS 广播失败：{exc!r}')
        self.register_thread = threading.Thread(
            target=register,
            daemon=True,
            name='oscquery-mdns-register',
        )
        self.register_thread.start()
        if self.avatar_callback is not None:
            self.vrchat_client = VRChatOSCQueryClient(self.zeroconf, self.avatar_callback)
            self.vrchat_client.start()
        logger.info(f'OSCQuery 已启动：HTTP {address}:{self.http_port}，OSC {self.osc_host}:{self.osc_port}')

    def request_avatar_refresh(self, fallback_avatar_id=None):
        if self.vrchat_client is not None:
            self.vrchat_client.request_refresh(fallback_avatar_id)

    def stop(self):
        self.stopping = True
        if self.register_thread is not None:
            self.register_thread.join(timeout=2)
            self.register_thread = None
        if self.vrchat_client is not None:
            self.vrchat_client.stop()
            self.vrchat_client = None
        if self.zeroconf is not None:
            if self.service_info is not None and self.service_registered:
                try:
                    self.zeroconf.unregister_service(self.service_info)
                except Exception as exc:
                    logger.debug(f'OSCQuery mDNS 取消广播失败：{exc!r}')
            self.zeroconf.close()
            self.zeroconf = None
            self.service_info = None
            self.service_registered = False
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
            self.http = None
        if self.http_thread is not None:
            self.http_thread.join(timeout=2)
            self.http_thread = None
