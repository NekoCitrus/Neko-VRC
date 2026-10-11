"""Private newline-JSON IPC endpoint owned by the WPF frontend.

WPF creates a CurrentUserOnly named pipe and starts this process with its
random pipe name. No network port or extra public control API is introduced.
"""

import argparse
import copy
import json
import queue
import re
import threading
import time
from collections import deque

from loguru import logger

import shocking_vrchat as core
from srv import WAVEFORM_NAMES_BY_DEVICE
from srv.config_manager import validate_config
from srv.steamvr_autostart import configure_steamvr_autostart


class BackendAPI:
    def __init__(self, config_dir=None, launch_executable=None):
        core.config_init(config_dir)
        self.manager = core.CONFIG_MANAGER
        self.settings, self.basic = copy.deepcopy(core.SETTINGS), copy.deepcopy(core.SETTINGS_BASIC)
        self.controller = core.ServiceController(event_callback=self.on_event)
        self.avatar = {}
        self.logs = deque(maxlen=100)
        self.log_lock = threading.Lock()
        self.launch_executable = launch_executable
        self.log_sink = logger.add(self._append_log, level='INFO')

    def _append_log(self, message):
        with self.log_lock:
            self.logs.append(str(message).strip())

    def on_event(self, event):
        if event.get('type') == 'avatar':
            self.avatar = event

    def state(self):
        with self.log_lock:
            logs = list(self.logs)
        return {'service': self.controller.state, 'runtime': self.controller.snapshot(),
                'avatar': self.avatar, 'logs': logs}

    def initial(self):
        return {'settings': copy.deepcopy(self.settings), 'basic': copy.deepcopy(self.basic),
                'waveforms': WAVEFORM_NAMES_BY_DEVICE, 'config_path': str(self.manager.path),
                **self.state()}

    def save(self, settings, basic):
        validate_config(settings, basic)
        # Failed persistence rolls live settings back and clears old triggers.
        previous_settings, previous_basic = self.settings, self.basic
        self.controller.apply_settings(settings, basic)
        try:
            if (settings['general']['steamvr_auto_start']
                    or settings['general']['steamvr_auto_start'] != previous_settings['general']['steamvr_auto_start']):
                configure_steamvr_autostart(
                    self.manager.config_dir, settings['general']['steamvr_auto_start'],
                    executable=self.launch_executable,
                )
            self.manager.save(settings, basic)
        except Exception:
            self.controller.apply_settings(previous_settings, previous_basic)
            raise
        self.settings, self.basic = copy.deepcopy(settings), copy.deepcopy(basic)
        return self.initial()

    def dispatch(self, method, data=None):
        data = data or {}
        if method == 'init':
            return self.initial()
        if method == 'snapshot':
            return self.state()
        if method == 'bluetooth_check':
            # Import Windows backends without scanning or connecting. This
            # also catches missing WinRT binaries in a frozen distribution.
            from bleak.backends.winrt.client import BleakClientWinRT
            from bleak.backends.winrt.scanner import BleakScannerWinRT
            return {'client': BleakClientWinRT.__name__, 'scanner': BleakScannerWinRT.__name__}
        if method == 'save':
            return self.save(data['settings'], data['basic'])
        if method == 'start':
            if self.settings['general']['steamvr_auto_start']:
                try:
                    result = configure_steamvr_autostart(self.manager.config_dir, True,
                                                        executable=self.launch_executable)
                    logger.info(result.message)
                except Exception as exc:
                    logger.warning('SteamVR 自动启动尚未确认：{}', exc)
            self.controller.start(self.settings, self.basic)
            return self.state()
        if method == 'shutdown':
            self.controller.stop()
            return self.state()
        if method == 'qr':
            import qrcode
            # Return rectangles, not a bitmap dependency or a temporary file.
            code = qrcode.QRCode(border=4)
            content = core.build_qr_content(self.settings)
            code.add_data(content)
            code.make(fit=True)
            return {'matrix': code.get_matrix(), 'content': content,
                    'url': core.build_websocket_url(self.settings)}
        if method in ('scan', 'connect', 'disconnect', 'stop', 'resume'):
            if self.controller.runtime is None:
                raise RuntimeError('先启动服务。')
            result = self.controller.runtime.device_action(method, data.get('kind'))
            return result if method == 'scan' else self.state()
        raise ValueError('未知操作。')

    def close(self):
        self.controller.stop()
        logger.remove(self.log_sink)


def serve_stream(stream, api, heartbeat_timeout=60):
    """Serve one parent; EOF or a missing heartbeat disarms and closes it."""
    requests = queue.Queue(maxsize=32)

    def read_loop():
        try:
            while True:
                line = stream.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    break
                requests.put(line, timeout=2)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            try:
                requests.put(None, timeout=1)
            except queue.Full:
                pass

    threading.Thread(target=read_loop, daemon=True, name='wpf-pipe-read').start()
    last_request = time.monotonic()
    try:
        while True:
            try:
                line = requests.get(timeout=0.5)
            except queue.Empty:
                if time.monotonic() - last_request > heartbeat_timeout:
                    break
                continue
            if line is None:
                break
            last_request = time.monotonic()
            request = {}
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or not isinstance(request.get('id'), int):
                    raise ValueError('IPC 请求格式无效。')
                result = api.dispatch(request.get('method'), request.get('data'))
                response = {'id': request['id'], 'ok': True, 'data': result}
            except Exception as exc:
                logger.exception('前端操作失败')
                response = {'id': request.get('id') if isinstance(request, dict) else None,
                            'ok': False, 'error': str(exc)}
            stream.write((json.dumps(response, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
            stream.flush()
            if isinstance(request, dict) and request.get('method') == 'shutdown':
                break
    finally:
        api.close()


class PipeStream:
    """Use Win32 pipe I/O, not CRT FileIO's disk-file update semantics."""
    def __init__(self, name):
        import _winapi
        self.api = _winapi
        self.handle = _winapi.CreateFile('\\\\.\\pipe\\' + name,
                                        _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                                        0, 0, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, 0)
        self.buffer = bytearray()

    def _complete(self, overlapped, error):
        # Synchronous reads on a duplex handle serialize against writes and
        # can deadlock while the frontend is waiting for its response.
        if error == self.api.ERROR_IO_PENDING:
            self.api.WaitForSingleObject(overlapped.event, self.api.INFINITE)
        transferred, error = overlapped.GetOverlappedResult(True)
        if error:
            raise OSError(error, '私有管道已断开。')
        return transferred

    def readline(self, limit):
        while b'\n' not in self.buffer and len(self.buffer) < limit:
            overlapped, error = self.api.ReadFile(self.handle, 65536, overlapped=True)
            self._complete(overlapped, error)
            data = overlapped.getbuffer()
            if not data:
                break
            self.buffer.extend(data)
        end = self.buffer.find(b'\n')
        length = min(end + 1 if end >= 0 else len(self.buffer), limit)
        line = bytes(self.buffer[:length])
        del self.buffer[:length]
        return line

    def write(self, data):
        while data:
            overlapped, error = self.api.WriteFile(self.handle, data, overlapped=True)
            written = self._complete(overlapped, error)
            if written <= 0:
                raise OSError('私有管道写入失败。')
            data = data[written:]

    def flush(self):
        pass

    def close(self):
        self.api.CloseHandle(self.handle)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pipe', required=True)
    parser.add_argument('--config-dir')
    parser.add_argument('--frontend')
    parser.add_argument('--stop-event')
    args = parser.parse_args()
    if not re.fullmatch(r'NekoVRC-[a-f0-9]{32}', args.pipe):
        raise ValueError('私有管道名称无效。')
    api = BackendAPI(args.config_dir, args.frontend)
    stop_monitor = threading.Event()
    monitor = None
    if args.stop_event:
        if args.stop_event != 'Local\\' + args.pipe + '-Stop':
            api.close()
            raise ValueError('急停事件名称无效。')
        monitor = threading.Thread(target=watch_stop_event, args=(args.stop_event, api, stop_monitor),
                                   daemon=True, name='wpf-emergency-stop')
        monitor.start()
    deadline = time.monotonic() + 15
    while True:
        try:
            stream = PipeStream(args.pipe)
            break
        except OSError:
            if time.monotonic() >= deadline:
                api.close()
                raise
            time.sleep(0.1)
    try:
        serve_stream(stream, api)
    finally:
        stream.close()
        stop_monitor.set()
        if monitor:
            monitor.join(timeout=1)


def watch_stop_event(name, api, done):
    """Emergency output stop must not queue behind scanning or IPC saves."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenEventW(0x00100000, False, name)  # SYNCHRONIZE only
    if not handle:
        logger.error('无法打开前端急停事件。')
        return
    try:
        while not done.is_set():
            if kernel.WaitForSingleObject(handle, 500) == 0:
                runtime = api.controller.runtime
                if runtime is not None:
                    try:
                        runtime.device_action('stop', timeout=10)
                    except Exception as exc:
                        logger.warning('急停归零未确认：{}', exc)
    finally:
        kernel.CloseHandle(handle)


if __name__ == '__main__':
    main()
