import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loguru import logger
from zeroconf import ServiceInfo, Zeroconf


class _OSCQueryHandler(BaseHTTPRequestHandler):
    service = None

    def log_message(self, format, *args):
        return

    def do_GET(self):
        path, _, query = self.path.partition('?')
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
    def __init__(self, settings):
        self.settings = settings
        self.name = 'ShockingVRChat'
        self.osc_host = settings['osc']['listen_host']
        self.osc_port = int(settings['osc']['listen_port'])
        self.http_host = settings.get('oscquery', {}).get('listen_host', '127.0.0.1')
        self.http_port = int(settings.get('oscquery', {}).get('listen_port', 8801))
        self.http = None
        self.zeroconf = None
        self.service_info = None

    def _paths(self):
        paths = set()
        for channel in ('channel_a', 'channel_b'):
            paths.update(self.settings['dglab3'][channel].get('avatar_params', []))
        return sorted(path for path in paths if path.startswith('/avatar/'))

    def root_node(self):
        contents = {'avatar': {'FULL_PATH': '/avatar', 'CONTENTS': {}}}
        for path in self._paths():
            node = contents['avatar']['CONTENTS']
            parts = path.strip('/').split('/')[1:]
            current = '/avatar'
            for index, part in enumerate(parts):
                current += '/' + part
                node = node.setdefault(part, {'FULL_PATH': current, 'TYPE': 'f', 'ACCESS': 2})
        return {'FULL_PATH': '/', 'CONTENTS': contents}

    def node_for(self, path):
        if path == '/avatar':
            return self.root_node()['CONTENTS']['avatar']
        for item in self._paths():
            if item == path:
                return {'FULL_PATH': item, 'TYPE': 'f', 'ACCESS': 2}
        return None

    def start(self):
        handler = type('OSCQueryRequestHandler', (_OSCQueryHandler,), {})
        handler.service = self
        self.http = ThreadingHTTPServer((self.http_host, self.http_port), handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        address = self.http_host if self.http_host not in ('0.0.0.0', '::') else '127.0.0.1'
        self.zeroconf = Zeroconf()
        self.service_info = ServiceInfo(
            '_oscjson._tcp.local.',
            f'{self.name}._oscjson._tcp.local.',
            addresses=[__import__('ipaddress').ip_address(address).packed],
            port=self.http_port,
            properties={b'txtvers': b'1'},
        )
        def register():
            try:
                self.zeroconf.register_service(self.service_info)
            except Exception as exc:
                logger.warning(f'OSCQuery mDNS 广播失败：{exc}')
        threading.Thread(target=register, daemon=True, name='oscquery-mdns-register').start()
        logger.info(f'OSCQuery 已启动：HTTP {address}:{self.http_port}，OSC {self.osc_host}:{self.osc_port}')

    def stop(self):
        if self.zeroconf is not None and self.service_info is not None:
            self.zeroconf.close()
            self.zeroconf = None
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
            self.http = None
