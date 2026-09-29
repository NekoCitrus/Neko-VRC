"""Small dependency-free Win32 desktop shell for the service.

The only UI-specific third-party package is qrcode; its matrix is painted
directly with GDI, so no image toolkit or browser process is required.
"""

import copy
import ctypes
import queue
import sys
import threading
import traceback
from ctypes import wintypes
from pathlib import Path

import qrcode
from loguru import logger

from srv import WAVEFORM_NAMES_BY_DEVICE
from srv.config_manager import validate_config
from srv.sps_depth import SPS_PLUG, SPS_SOCKET, SPSZone
from srv.steamvr_autostart import SteamVRAutoStartError, configure_steamvr_autostart


if sys.platform != 'win32':
    raise RuntimeError('桌面界面仅支持 Windows。')


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32
comctl32 = ctypes.windll.comctl32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

# ctypes otherwise assumes 32-bit integer return values and truncates handles
# in a 64-bit build. Declare the pointer-sized results used by this module.
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.restype = LRESULT
user32.LoadIconW.restype = wintypes.HICON
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadCursorW.restype = wintypes.HANDLE
user32.SendMessageW.restype = LRESULT
user32.CreatePopupMenu.restype = wintypes.HMENU
gdi32.CreateFontW.restype = wintypes.HANDLE
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
user32.BeginPaint.restype = wintypes.HDC
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE,
    wintypes.LPCWSTR,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.DestroyIcon.argtypes = [wintypes.HICON]

WM_CREATE = 0x0001
WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_SETFONT = 0x0030
WM_CTLCOLORBTN = 0x0135
WM_CTLCOLORSTATIC = 0x0138
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_EVENTS = WM_APP + 2
WM_AUTOSTART = WM_APP + 3
WM_SHOW_EXISTING = WM_APP + 4

WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_TABSTOP = 0x00010000
WS_VSCROLL = 0x00200000
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_EX_CLIENTEDGE = 0x00000200
BS_PUSHBUTTON = 0x00000000
BS_AUTOCHECKBOX = 0x00000003
BS_GROUPBOX = 0x00000007
ES_LEFT = 0x0000
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
ES_READONLY = 0x0800
SS_LEFT = 0x0000
SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9
BM_GETCHECK = 0x00F0
BM_SETCHECK = 0x00F1
BST_CHECKED = 1
CBS_DROPDOWNLIST = 0x0003
CB_ADDSTRING = 0x0143
CB_SETCURSEL = 0x014E
CB_GETCURSEL = 0x0147
CB_RESETCONTENT = 0x014B
PBM_SETRANGE32 = 0x0406
PBM_SETPOS = 0x0402
MF_STRING = 0x0000
TPM_RIGHTBUTTON = 0x0002
NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
COLOR_WINDOW = 5
TRANSPARENT = 1
COLOR_WHITE = 0x00FFFFFF
COLOR_TEXT = 0x00202020
IDC_ARROW = 32512
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXICON = 11
SM_CYICON = 12
SM_CXSMICON = 49
SM_CYSMICON = 50
CW_USEDEFAULT = -2147483648

BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
ICON_PATH = BUNDLE_DIR / 'assets' / 'shocking_vrchat.ico'

ID_NAV_GENERAL = 101
ID_NAV_COYOTE = 102
ID_NAV_OPOSSUM = 103
ID_NAV_DEBUG = 104
ID_NAV_ABOUT = 105
ID_START = 110
ID_STOP = 111
ID_SAVE_RESTART = 112
ID_EXIT = 113
ID_TRAY_SHOW = 201
ID_TRAY_START = 202
ID_TRAY_STOP = 203
ID_TRAY_EXIT = 204

COPYRIGHT_ENTRIES = (
    ('DG-LAB', 'https://github.com/dungeonlab-open', '设备协议与官方波形'),
    ('Shocking-VRChat', 'https://github.com/VRChatNext/Shocking-VRChat', '原始项目与代码来源'),
    ('DG-LAB-VRCOSC', 'https://github.com/ccvrc/DG-LAB-VRCOSC', 'Chatbox 发送部分来源'),
    ('OscGoesBrrr / OSC Toys', 'https://osc.toys', 'SPS/OGB 深度算法与参数协议参考'),
)
FRONTEND_CONTRIBUTORS = ('WenX1ang', '猫橘Citrus', 'ChatGPT')


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.UINT),
        ('style', wintypes.UINT),
        ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', wintypes.HINSTANCE),
        ('hIcon', wintypes.HICON),
        ('hCursor', wintypes.HANDLE),
        ('hbrBackground', wintypes.HBRUSH),
        ('lpszMenuName', wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
        ('hIconSm', wintypes.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ('hdc', wintypes.HDC),
        ('fErase', wintypes.BOOL),
        ('rcPaint', wintypes.RECT),
        ('fRestore', wintypes.BOOL),
        ('fIncUpdate', wintypes.BOOL),
        ('rgbReserved', ctypes.c_byte * 32),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', wintypes.DWORD),
        ('Data2', wintypes.WORD),
        ('Data3', wintypes.WORD),
        ('Data4', ctypes.c_byte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('hWnd', wintypes.HWND),
        ('uID', wintypes.UINT),
        ('uFlags', wintypes.UINT),
        ('uCallbackMessage', wintypes.UINT),
        ('hIcon', wintypes.HICON),
        ('szTip', wintypes.WCHAR * 128),
        ('dwState', wintypes.DWORD),
        ('dwStateMask', wintypes.DWORD),
        ('szInfo', wintypes.WCHAR * 256),
        ('uTimeoutOrVersion', wintypes.UINT),
        ('szInfoTitle', wintypes.WCHAR * 64),
        ('dwInfoFlags', wintypes.DWORD),
        ('guidItem', GUID),
        ('hBalloonIcon', wintypes.HICON),
    ]


kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.BOOL]
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]


_application = None


@WNDPROC
def _window_proc(hwnd, message, wparam, lparam):
    if _application is not None:
        return _application.window_proc(hwnd, message, wparam, lparam)
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


class DesktopApplication:
    WIDTH = 1120
    HEIGHT = 760

    def __init__(self, config_manager, settings, basic_settings, controller):
        self.config_manager = config_manager
        self.settings = copy.deepcopy(settings)
        self.basic_settings = copy.deepcopy(basic_settings)
        self.controller = controller
        self.hwnd = None
        self.font = None
        self.background_brush = gdi32.CreateSolidBrush(COLOR_WHITE)
        self.controls = {}
        self.panels = {
            'general': [],
            'coyote': [],
            'opossum': [],
            'debug': [],
            'about': [],
        }
        self.events = queue.Queue()
        self.steamvr_lock = threading.Lock()
        self.action_running = False
        self.exiting = False
        self.pending_exit = False
        self.tray_data = None
        self.large_icon = None
        self.small_icon = None
        self.loaded_icons = []
        self._combo_buffers = []
        self.qr_matrix = []
        self._last_snapshot = None
        self.sps_zone_options = {
            SPS_SOCKET: [SPSZone(SPS_SOCKET, '*', '任何正在触发的 Socket')],
            SPS_PLUG: [SPSZone(SPS_PLUG, '*', '任何正在触发的 Plug')],
        }
        self.channel_zone_selections = {}
        for device_kind in ('coyote', 'opossum'):
            for channel in ('A', 'B'):
                key = (device_kind, channel)
                config = self.basic_settings['dglab3'][device_kind][f'channel_{channel.lower()}']
                self.channel_zone_selections[key] = {
                    SPS_SOCKET: self._initial_zone(SPS_SOCKET, config['socket_zone']),
                    SPS_PLUG: self._initial_zone(SPS_PLUG, config['plug_zone']),
                }

    def _initial_zone(self, trigger_type, configured):
        options = self.sps_zone_options[trigger_type]
        if configured and all(option.zone_id != configured for option in options):
            options.append(SPSZone(trigger_type, configured, configured))
        if configured:
            return configured
        return options[0].zone_id if options else ''

    def post_event(self, event):
        self.events.put(event)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_EVENTS, 0, 0)

    def _create(self, class_name, text, style, x, y, width, height, control_id=0, ex_style=0, panel=None):
        handle = user32.CreateWindowExW(
            ex_style,
            class_name,
            text,
            style | WS_CHILD | WS_VISIBLE,
            x,
            y,
            width,
            height,
            self.hwnd,
            control_id,
            kernel32.GetModuleHandleW(None),
            None,
        )
        if not handle:
            raise ctypes.WinError()
        if self.font:
            user32.SendMessageW(handle, WM_SETFONT, self.font, True)
        if panel:
            self.panels[panel].append(handle)
        return handle

    def _label(self, text, x, y, width, height=24, panel=None):
        return self._create('STATIC', text, SS_LEFT, x, y, width, height, panel=panel)

    def _edit(self, key, value, x, y, width, height=25, multiline=False, readonly=False, panel=None):
        style = WS_TABSTOP | ES_LEFT
        if multiline:
            style |= ES_MULTILINE | ES_AUTOVSCROLL | WS_VSCROLL
        if readonly:
            style |= ES_READONLY
        handle = self._create('EDIT', str(value), style, x, y, width, height, ex_style=WS_EX_CLIENTEDGE, panel=panel)
        self.controls[key] = handle
        return handle

    def _check(self, key, text, checked, x, y, width, panel=None):
        handle = self._create('BUTTON', text, WS_TABSTOP | BS_AUTOCHECKBOX, x, y, width, 28, panel=panel)
        user32.SendMessageW(handle, BM_SETCHECK, BST_CHECKED if checked else 0, 0)
        self.controls[key] = handle
        return handle

    def _combo(self, key, values, selected, x, y, width, panel=None):
        handle = self._create('COMBOBOX', '', WS_TABSTOP | CBS_DROPDOWNLIST, x, y, width, 180, panel=panel)
        for value in values:
            buffer = ctypes.create_unicode_buffer(value)
            self._combo_buffers.append(buffer)
            text_ptr = ctypes.cast(buffer, ctypes.c_void_p).value
            user32.SendMessageW(handle, CB_ADDSTRING, 0, text_ptr)
        index = values.index(selected) if selected in values else 0
        user32.SendMessageW(handle, CB_SETCURSEL, index, 0)
        self.controls[key] = handle
        return handle

    def _zone_id_from_combo(self, device_kind, channel, trigger_type):
        options = self.sps_zone_options[trigger_type]
        index = int(user32.SendMessageW(
            self.controls[f'zone_{device_kind}_{channel.lower()}_{trigger_type}'],
            CB_GETCURSEL, 0, 0,
        ))
        return options[index].zone_id if 0 <= index < len(options) else ''

    def _populate_zone_combo(self, device_kind, channel, trigger_type):
        control_key = f'zone_{device_kind}_{channel.lower()}_{trigger_type}'
        handle = self.controls[control_key]
        user32.SendMessageW(handle, CB_RESETCONTENT, 0, 0)
        options = self.sps_zone_options[trigger_type]
        for option in options:
            buffer = ctypes.create_unicode_buffer(option.label)
            self._combo_buffers.append(buffer)
            user32.SendMessageW(handle, CB_ADDSTRING, 0, ctypes.cast(buffer, ctypes.c_void_p).value)
        selected = self.channel_zone_selections[(device_kind, channel)].get(trigger_type, '')
        index = next((i for i, option in enumerate(options) if option.zone_id == selected), 0)
        user32.SendMessageW(handle, CB_SETCURSEL, index if options else -1, 0)

    def _combo_value(self, key, values):
        index = int(user32.SendMessageW(self.controls[key], CB_GETCURSEL, 0, 0))
        if not 0 <= index < len(values):
            raise ValueError(f'{key} 没有有效选项。')
        return values[index]

    def _apply_avatar_event(self, event):
        """Refresh choices from the current Avatar resolved through OSCQuery."""
        for device_kind in ('coyote', 'opossum'):
            for channel in ('A', 'B'):
                key = (device_kind, channel)
                for trigger_type in (SPS_SOCKET, SPS_PLUG):
                    control_key = f'zone_{device_kind}_{channel.lower()}_{trigger_type}'
                    if control_key not in self.controls:
                        continue
                    selected = self._zone_id_from_combo(device_kind, channel, trigger_type)
                    if selected:
                        self.channel_zone_selections[key][trigger_type] = selected

        sources = {
            SPS_SOCKET: ('sockets', '任何正在触发的 Socket'),
            SPS_PLUG: ('plugs', '任何正在触发的 Plug'),
        }
        for trigger_type, (key, any_label) in sources.items():
            options = [SPSZone(trigger_type, '*', any_label)]
            options.extend(
                SPSZone(trigger_type, str(item['zone_id']), str(item['label']))
                for item in event.get(key, ())
                if item.get('zone_id')
            )
            for device_kind in ('coyote', 'opossum'):
                for channel in ('A', 'B'):
                    selected = self.channel_zone_selections[(device_kind, channel)].get(
                        trigger_type, '*',
                    )
                    if selected and all(option.zone_id != selected for option in options):
                        options.append(SPSZone(trigger_type, selected, selected))
            self.sps_zone_options[trigger_type] = options

        for device_kind in ('coyote', 'opossum'):
            for channel in ('A', 'B'):
                for trigger_type in (SPS_SOCKET, SPS_PLUG):
                    self._populate_zone_combo(device_kind, channel, trigger_type)

        avatar_name = event.get('avatar_name') or event.get('avatar_id') or '未知'
        source = event.get('source') or 'OSC'
        if event.get('found'):
            status = (
                f'当前 Avatar（{source}）：{avatar_name}　'
                f'Socket {len(event.get("sockets", ()))} 个 / '
                f'Plug {len(event.get("plugs", ()))} 个'
            )
        else:
            status = f'当前 Avatar（{source}）：{avatar_name}（未找到参数信息）'
        for device_kind in ('coyote', 'opossum'):
            key = f'avatar_status_{device_kind}'
            if key in self.controls:
                self._set_text(key, status)

    def _button(self, text, control_id, x, y, width, height=30, panel=None):
        return self._create('BUTTON', text, WS_TABSTOP | BS_PUSHBUTTON, x, y, width, height, control_id, panel=panel)

    def _build_controls(self):
        self.font = gdi32.CreateFontW(
            -17, 0, 0, 0, 400, False, False, False, 1, 0, 0, 5, 0, 'Segoe UI'
        )
        self._label('服务状态', 20, 22, 100, 28)
        self.controls['service_status'] = self._label('正在启动…', 125, 22, 180, 28)
        self._button('启动服务', ID_START, 330, 14, 100, 36)
        self._button('停止服务', ID_STOP, 442, 14, 100, 36)
        self._button('保存并应用', ID_SAVE_RESTART, 554, 14, 150, 36)

        self._button('基本设置', ID_NAV_GENERAL, 20, 78, 110, 40)
        self._button('郊狼 A/B', ID_NAV_COYOTE, 20, 130, 110, 40)
        self._button('负鼠 A/B', ID_NAV_OPOSSUM, 20, 182, 110, 40)
        self._button('运行调试', ID_NAV_DEBUG, 20, 234, 110, 40)
        self._button('版权信息', ID_NAV_ABOUT, 20, 286, 110, 40)

        self._build_general_panel()
        self._build_device_panel('coyote')
        self._build_device_panel('opossum')
        self._build_debug_panel()
        self._build_about_panel()

        self.controls['connection_title'] = self._label('手机连接二维码', 770, 76, 310, 28)
        self.controls['device_status'] = self._label('APP：未连接', 770, 405, 310, 30)
        self._label('连接地址', 770, 447, 310, 26)
        self._edit('qr_content', '', 770, 475, 310, 72, multiline=True, readonly=True)
        self._label('配置文件', 770, 566, 310, 26)
        self._edit('config_path', str(self.config_manager.path), 770, 594, 310, 105, multiline=True, readonly=True)

        self._show_panel('general')
        self._refresh_qr()

    def _build_general_panel(self):
        panel = 'general'
        self._create('BUTTON', '设备连接', BS_GROUPBOX, 150, 72, 575, 170, panel=panel)
        self._label(
            'DG-LAB 4 APP / Socket V4（自动识别郊狼或负鼠）',
            172, 105, 525, 38, panel=panel,
        )
        self._label(
            '本软件使用 OSCQuery，VRChat 可自动发现服务，不需要额外的 OSC 分流软件。',
            172, 154, 525, 60, panel=panel,
        )

        self._create('BUTTON', '安全与启动', BS_GROUPBOX, 150, 255, 575, 205, panel=panel)
        self._check('chatbox', '启用 VRChat Chatbox 状态消息', self.settings['chatbox']['enable'], 172, 290, 360, panel=panel)
        self._check(
            'background',
            '关闭主窗口后继续在系统托盘运行',
            self.settings['general']['run_in_background'],
            172,
            330,
            400,
            panel=panel,
        )
        self._check(
            'steamvr_auto_start',
            '跟随 SteamVR 启动',
            self.settings['general']['steamvr_auto_start'],
            172,
            368,
            300,
            panel=panel,
        )
        steamvr_status = (
            'SteamVR：已配置，启动时将自动校验'
            if self.settings['general']['steamvr_auto_start']
            else 'SteamVR：未启用'
        )
        self.controls['steamvr_status'] = self._label(steamvr_status, 195, 406, 500, 38, panel=panel)

    def _build_device_panel(self, device_kind):
        is_coyote = device_kind == 'coyote'
        panel = device_kind
        title = '郊狼通道' if is_coyote else '负鼠通道'
        self._label(title, 150, 76, 575, 30, panel=panel)
        self.controls[f'avatar_status_{device_kind}'] = self._label(
            '正在读取 Avatar…', 150, 108, 575, 28, panel=panel,
        )
        self._build_device_channel(device_kind, 'A', 150, 140, panel)
        self._build_device_channel(device_kind, 'B', 150, 392, panel)


    def _build_device_channel(self, device_kind, channel, x, y, panel):
        lower = channel.lower()
        self._create('BUTTON', f'{channel} 通道', BS_GROUPBOX, x, y, 575, 240, panel=panel)
        self._label('Socket', x + 18, y + 28, 60, 28, panel=panel)
        self._combo(
            f'zone_{device_kind}_{lower}_{SPS_SOCKET}', [''], '',
            x + 80, y + 24, 185, panel=panel,
        )
        self._populate_zone_combo(device_kind, channel, SPS_SOCKET)
        self._label('Plug', x + 282, y + 28, 50, 28, panel=panel)
        self._combo(
            f'zone_{device_kind}_{lower}_{SPS_PLUG}', [''], '',
            x + 332, y + 24, 225, panel=panel,
        )
        self._populate_zone_combo(device_kind, channel, SPS_PLUG)
        config = self.settings['dglab3'][device_kind][f'channel_{lower}']
        selected_waveform = config['depth']['waveform']
        waveforms = WAVEFORM_NAMES_BY_DEVICE[device_kind]
        self._label('波形', x + 18, y + 70, 50, 28, panel=panel)
        self._combo(
            f'waveform_{device_kind}_{lower}', waveforms, selected_waveform,
            x + 68, y + 66, 180, panel=panel,
        )
        self._label('上限', x + 265, y + 70, 45, 28, panel=panel)
        self._edit(
            f'{device_kind}_strength_{lower}',
            self.basic_settings['dglab3'][device_kind][f'channel_{lower}']['strength_limit'],
            x + 310, y + 66, 60, 28, panel=panel,
        )
        extra = self.basic_settings['dglab3'][device_kind][f'channel_{lower}']['extra_parameters']
        self._check(
            f'extra_enabled_{device_kind}_{lower}', '额外参数', extra['enabled'],
            x + 392, y + 64, 150, panel=panel,
        )
        self._edit(
            f'extra_paths_{device_kind}_{lower}', '\r\n'.join(extra['paths']),
            x + 18, y + 108, 539, 112, multiline=True, panel=panel,
        )

    def _build_debug_panel(self):
        panel = 'debug'
        self._label('当前设备实时状态（只读）', 150, 76, 575, 30, panel=panel)
        self._build_device_debug('coyote', 150, 112, panel)
        self._build_device_debug('opossum', 150, 362, panel)

    def _build_device_debug(self, device_kind, x, y, panel):
        title = '郊狼' if device_kind == 'coyote' else '负鼠'
        self._create('BUTTON', f'{title} A/B 通道', BS_GROUPBOX, x, y, 575, 225, panel=panel)
        self._label('通道', x + 18, y + 28, 45, 24, panel=panel)
        self._label('来源', x + 68, y + 28, 100, 24, panel=panel)
        self._label('当前部位 / 参数', x + 173, y + 28, 205, 24, panel=panel)
        self._label('深度', x + 383, y + 28, 65, 24, panel=panel)
        self._label('发送强度', x + 438, y + 28, 105, 24, panel=panel)
        for index, channel in enumerate(('A', 'B')):
            row_y = y + 58 + index * 78
            prefix = f'debug_{device_kind}_{channel}'
            self._label(channel, x + 18, row_y, 45, 28, panel=panel)
            self.controls[f'{prefix}_mode'] = self._label('—', x + 68, row_y, 100, 28, panel=panel)
            self.controls[f'{prefix}_zone'] = self._label('—', x + 173, row_y, 205, 28, panel=panel)
            self.controls[f'{prefix}_depth'] = self._label('0.0%', x + 383, row_y, 65, 28, panel=panel)
            self.controls[f'{prefix}_strength'] = self._label('未连接', x + 438, row_y, 105, 28, panel=panel)
            progress = self._create(
                'msctls_progress32', '', 0, x + 68, row_y + 31, 475, 14, panel=panel,
            )
            user32.SendMessageW(progress, PBM_SETRANGE32, 0, 1000)
            self.controls[f'{prefix}_progress'] = progress

    def _build_about_panel(self):
        panel = 'about'
        self._label('版权与来源', 150, 78, 575, 36, panel=panel)
        self._create('BUTTON', '项目与代码来源', BS_GROUPBOX, 150, 120, 575, 280, panel=panel)
        y = 145
        for name, homepage, contribution in COPYRIGHT_ENTRIES:
            self._label(name, 172, y, 155, 28, panel=panel)
            self._label(contribution, 335, y, 370, 28, panel=panel)
            self._label(homepage, 195, y + 27, 510, 26, panel=panel)
            y += 60

        self._create('BUTTON', '前端部分贡献', BS_GROUPBOX, 150, 415, 575, 105, panel=panel)
        for index, contributor in enumerate(FRONTEND_CONTRIBUTORS):
            self._label(contributor, 172 + index * 158, 447, 145, 28, panel=panel)
        self._label('以上三者的贡献均为前端界面部分。', 172, 480, 520, 28, panel=panel)

        self._create('BUTTON', '开源许可', BS_GROUPBOX, 150, 535, 575, 105, panel=panel)
        self._label('本程序依照 GNU Affero General Public License v3.0 发布。', 172, 565, 530, 30, panel=panel)
        self._label('DG-LAB、VRChat、SteamVR 等名称及商标归各自权利人所有。', 172, 596, 530, 30, panel=panel)

    def _show_panel(self, selected):
        for name, handles in self.panels.items():
            command = SW_SHOW if name == selected else SW_HIDE
            for handle in handles:
                user32.ShowWindow(handle, command)
        user32.InvalidateRect(self.hwnd, None, True)

    def _get_text(self, key):
        handle = self.controls[key]
        length = user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    def _set_text(self, key, value):
        user32.SetWindowTextW(self.controls[key], str(value))

    def _is_checked(self, key):
        return user32.SendMessageW(self.controls[key], BM_GETCHECK, 0, 0) == BST_CHECKED

    def _set_checked(self, key, checked):
        user32.SendMessageW(self.controls[key], BM_SETCHECK, BST_CHECKED if checked else 0, 0)

    def _read_form(self):
        settings = copy.deepcopy(self.settings)
        basic = copy.deepcopy(self.basic_settings)
        settings['chatbox']['enable'] = self._is_checked('chatbox')
        settings['general']['run_in_background'] = self._is_checked('background')
        settings['general']['steamvr_auto_start'] = self._is_checked('steamvr_auto_start')
        for device_kind in ('coyote', 'opossum'):
            for channel in ('A', 'B'):
                lower = channel.lower()
                basic_channel = basic['dglab3'][device_kind][f'channel_{lower}']
                basic_channel['socket_zone'] = self._zone_id_from_combo(
                    device_kind, channel, SPS_SOCKET,
                )
                basic_channel['plug_zone'] = self._zone_id_from_combo(
                    device_kind, channel, SPS_PLUG,
                )
                paths = []
                for item in self._get_text(f'extra_paths_{device_kind}_{lower}').splitlines():
                    path = item.strip()
                    if path and path not in paths:
                        paths.append(path)
                basic_channel['extra_parameters']['enabled'] = self._is_checked(
                    f'extra_enabled_{device_kind}_{lower}',
                )
                basic_channel['extra_parameters']['paths'] = paths
                basic_channel['strength_limit'] = int(
                    self._get_text(f'{device_kind}_strength_{lower}')
                )
                waveforms = WAVEFORM_NAMES_BY_DEVICE[device_kind]
                settings['dglab3'][device_kind][f'channel_{lower}']['depth']['waveform'] = (
                    self._combo_value(
                        f'waveform_{device_kind}_{lower}', waveforms,
                    )
                )
        validate_config(settings, basic)
        return settings, basic

    def _apply_steamvr_autostart(self, enabled):
        with self.steamvr_lock:
            return configure_steamvr_autostart(self.config_manager.config_dir, enabled)

    def _sync_steamvr_on_start(self):
        if not self.settings['general']['steamvr_auto_start']:
            return
        self._set_text('steamvr_status', 'SteamVR：正在校验自动启动…')

        def worker():
            try:
                result = self._apply_steamvr_autostart(True)
                self.post_event({'type': 'steamvr', 'result': result})
            except SteamVRAutoStartError as exc:
                logger.warning('SteamVR 自动启动同步失败：{}', exc)
                self.post_event({'type': 'steamvr', 'error': str(exc)})

        threading.Thread(target=worker, daemon=True, name='ui-steamvr-sync').start()

    def _start_action(self, action, settings=None, basic=None):
        if self.action_running:
            self._message('当前操作尚未完成，请稍候。', error=False)
            return
        self.action_running = True

        def worker():
            error = None
            steamvr_result = None
            try:
                if action == 'start':
                    self.controller.start(self.settings, self.basic_settings)
                elif action == 'stop':
                    self.controller.stop()
                elif action == 'restart':
                    self.controller.restart(settings, basic)
                elif action == 'save_apply':
                    desired = settings['general']['steamvr_auto_start']
                    previous = self.settings['general']['steamvr_auto_start']
                    if desired or desired != previous:
                        steamvr_result = self._apply_steamvr_autostart(desired)
                    self.controller.apply_settings(settings, basic)
                    try:
                        self.config_manager.save(settings, basic)
                    except Exception:
                        self.controller.apply_settings(self.settings, self.basic_settings)
                        raise
                elif action == 'exit':
                    self.controller.stop()
            except Exception as exc:
                logger.error(traceback.format_exc())
                error = str(exc)
            self.post_event({
                'type': 'action_done',
                'action': action,
                'error': error,
                'settings': settings,
                'basic': basic,
                'steamvr_result': steamvr_result,
            })

        threading.Thread(target=worker, daemon=True, name=f'ui-{action}').start()

    def _save_and_apply(self):
        if self.action_running:
            self._message('当前操作尚未完成，请稍候再试。', error=True)
            return
        try:
            settings, basic = self._read_form()
            desired = settings['general']['steamvr_auto_start']
            previous = self.settings['general']['steamvr_auto_start']
            if desired or desired != previous:
                self._set_text('steamvr_status', 'SteamVR：正在应用设置…')
            self._start_action('save_apply', settings, basic)
        except (ValueError, OSError) as exc:
            self._message(str(exc), error=True)

    def _process_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            event_type = event.get('type')
            if event_type == 'service':
                state = event.get('state', 'stopped')
                labels = {
                    'starting': '正在启动…',
                    'running': '运行中',
                    'stopped': '已停止',
                    'error': '启动失败',
                }
                self._set_text('service_status', labels.get(state, state))
            elif event_type == 'action_done':
                self.action_running = False
                if event.get('error'):
                    if event.get('action') == 'save_apply':
                        self._set_checked(
                            'steamvr_auto_start',
                            self.settings['general']['steamvr_auto_start'],
                        )
                        current = '已启用' if self.settings['general']['steamvr_auto_start'] else '未启用'
                        self._set_text('steamvr_status', f'SteamVR：{current}（本次更改失败）')
                    self._message(f'操作失败：{event["error"]}', error=True)
                elif event.get('action') == 'save_apply':
                    self.settings = event['settings']
                    self.basic_settings = event['basic']
                    self._refresh_qr()
                    result = event.get('steamvr_result')
                    if result is not None:
                        self._set_text('steamvr_status', f'SteamVR：{result.message}')
                        if result.pending_restart:
                            self._message(result.message)
                if event.get('action') == 'exit':
                    user32.DestroyWindow(self.hwnd)
                elif self.pending_exit:
                    self.pending_exit = False
                    self._start_action('exit')
            elif event_type == 'steamvr':
                if event.get('error'):
                    self._set_text('steamvr_status', f'SteamVR：待同步（{event["error"]}）')
                else:
                    self._set_text('steamvr_status', f'SteamVR：{event["result"].message}')
            elif event_type == 'avatar':
                self._apply_avatar_event(event)
            elif event_type == 'device':
                if event.get('app_connected'):
                    version = str(event.get('protocol', '')).upper()
                    device = f'APP：已连接（{version}，等待设备）'
                else:
                    device = 'APP：未连接'
                self._set_text('device_status', device)

    def _refresh_snapshot(self):
        snapshot = self.controller.snapshot()
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot
        protocol = str(snapshot.get('protocol', '')).upper()
        if snapshot['connected']:
            devices = snapshot.get('devices', ())
            coyote_count = sum(item.get('device_kind') == 'coyote' for item in devices)
            opossum_count = sum(item.get('device_kind') == 'opossum' for item in devices)
            device = f'APP：已连接（{protocol}，郊狼 {coyote_count} / 负鼠 {opossum_count}）'
        elif snapshot.get('app_connected'):
            device = f'APP：已连接（{protocol}，等待设备）'
        else:
            device = 'APP：未连接'
        self._set_text('device_status', device)
        device_channels = snapshot.get('device_channels', {})
        for device_kind in ('coyote', 'opossum'):
            for channel in ('A', 'B'):
                info = device_channels.get(device_kind, {}).get(channel, {})
                percentage = float(info.get('strength_percentage', 0.0))
                trigger_type = info.get('trigger_type')
                mode = {
                    SPS_SOCKET: 'Socket', SPS_PLUG: 'Plug', 'extra': '额外参数',
                }.get(trigger_type, '—')
                detail = info.get('active_zone') or info.get('parameter') or '—'
                prefix = f'debug_{device_kind}_{channel}'
                self._set_text(f'{prefix}_mode', mode)
                self._set_text(f'{prefix}_zone', detail)
                self._set_text(f'{prefix}_depth', f'{percentage * 100:.1f}%')
                items = info.get('device_strengths', ())
                if not items:
                    text = '未连接'
                else:
                    strengths = info.get('sent_strengths', (0, 0, 0, 0))
                    text = '/'.join(str(int(value)) for value in strengths)
                self._set_text(f'{prefix}_strength', text)
                user32.SendMessageW(
                    self.controls[f'{prefix}_progress'], PBM_SETPOS,
                    int(percentage * 1000), 0,
                )

    def _refresh_qr(self):
        if 'connection_title' in self.controls:
            self._set_text('connection_title', '手机连接二维码')
        server_ip = self.settings.get('SERVER_IP')
        if not server_ip:
            from shocking_vrchat import detect_current_ip

            server_ip = detect_current_ip(self.settings)
        from shocking_vrchat import build_qr_content, build_websocket_url

        content = build_qr_content(self.settings, server_ip=server_ip)
        websocket_url = build_websocket_url(self.settings, server_ip=server_ip)
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2, box_size=1)
        qr.add_data(content)
        qr.make(fit=True)
        self.qr_matrix = qr.get_matrix()
        if 'qr_content' in self.controls:
            self._set_text('qr_content', websocket_url)
        if self.hwnd:
            user32.InvalidateRect(self.hwnd, None, False)

    def _paint_qr(self):
        paint = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(paint))
        try:
            if not self.qr_matrix:
                return
            left, top, size = 785, 112, 280
            white = gdi32.CreateSolidBrush(0x00FFFFFF)
            black = gdi32.CreateSolidBrush(0x00000000)
            rect = wintypes.RECT(left, top, left + size, top + size)
            user32.FillRect(hdc, ctypes.byref(rect), white)
            count = len(self.qr_matrix)
            scale = max(1, size // count)
            actual = scale * count
            offset_x = left + (size - actual) // 2
            offset_y = top + (size - actual) // 2
            for row_index, row in enumerate(self.qr_matrix):
                for column_index, value in enumerate(row):
                    if not value:
                        continue
                    cell = wintypes.RECT(
                        offset_x + column_index * scale,
                        offset_y + row_index * scale,
                        offset_x + (column_index + 1) * scale,
                        offset_y + (row_index + 1) * scale,
                    )
                    user32.FillRect(hdc, ctypes.byref(cell), black)
            gdi32.DeleteObject(white)
            gdi32.DeleteObject(black)
        finally:
            user32.EndPaint(self.hwnd, ctypes.byref(paint))

    def _add_tray_icon(self):
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self.small_icon or user32.LoadIconW(None, IDI_APPLICATION)
        data.szTip = 'Neko-VRC'
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
        self.tray_data = data

    def _remove_tray_icon(self):
        if self.tray_data is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.tray_data))
            self.tray_data = None

    def _show_tray_menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_SHOW, '打开主窗口')
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_START, '启动服务')
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_STOP, '停止服务')
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_EXIT, '退出')
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, point.x, point.y, 0, self.hwnd, None)
        user32.DestroyMenu(menu)

    def _restore(self):
        user32.ShowWindow(self.hwnd, SW_RESTORE)
        user32.SetForegroundWindow(self.hwnd)

    def _request_exit(self):
        if self.exiting:
            return
        self.exiting = True
        user32.ShowWindow(self.hwnd, SW_HIDE)
        if self.action_running:
            self.pending_exit = True
        else:
            self._start_action('exit')

    def _message(self, text, error=False):
        user32.MessageBoxW(self.hwnd, str(text), 'Neko-VRC', 0x10 if error else 0x40)

    def window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_CREATE:
            self.hwnd = hwnd
            self._build_controls()
            self._sync_steamvr_on_start()
            self._add_tray_icon()
            user32.SetTimer(hwnd, 1, 250, None)
            user32.PostMessageW(hwnd, WM_AUTOSTART, 0, 0)
            return 0
        if message == WM_AUTOSTART:
            self._start_action('start')
            return 0
        if message == WM_SHOW_EXISTING:
            self._restore()
            return 0
        if message == WM_COMMAND:
            command = int(wparam) & 0xFFFF
            if command == ID_NAV_GENERAL:
                self._show_panel('general')
            elif command == ID_NAV_COYOTE:
                self._show_panel('coyote')
            elif command == ID_NAV_OPOSSUM:
                self._show_panel('opossum')
            elif command == ID_NAV_DEBUG:
                self._show_panel('debug')
            elif command == ID_NAV_ABOUT:
                self._show_panel('about')
            elif command in (ID_START, ID_TRAY_START):
                self._start_action('start')
            elif command in (ID_STOP, ID_TRAY_STOP):
                self._start_action('stop')
            elif command == ID_SAVE_RESTART:
                self._save_and_apply()
            elif command in (ID_EXIT, ID_TRAY_EXIT):
                self._request_exit()
            elif command == ID_TRAY_SHOW:
                self._restore()
            return 0
        if message == WM_EVENTS:
            self._process_events()
            return 0
        if message == WM_TIMER:
            self._process_events()
            self._refresh_snapshot()
            return 0
        if message == WM_PAINT:
            self._paint_qr()
            return 0
        if message in (WM_CTLCOLORSTATIC, WM_CTLCOLORBTN):
            hdc = wintypes.HDC(wparam)
            gdi32.SetBkMode(hdc, TRANSPARENT)
            gdi32.SetTextColor(hdc, COLOR_TEXT)
            return self.background_brush
        if message == WM_TRAY:
            mouse_message = int(lparam) & 0xFFFF
            if mouse_message in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._restore()
            elif mouse_message == WM_RBUTTONUP:
                self._show_tray_menu()
            return 0
        if message == WM_CLOSE:
            if not self.exiting and self._is_checked('background'):
                user32.ShowWindow(hwnd, SW_HIDE)
            else:
                self._request_exit()
            return 0
        if message == WM_DESTROY:
            user32.KillTimer(hwnd, 1)
            self._remove_tray_icon()
            if self.font:
                gdi32.DeleteObject(self.font)
                self.font = None
            if self.background_brush:
                gdi32.DeleteObject(self.background_brush)
                self.background_brush = None
            for icon in self.loaded_icons:
                user32.DestroyIcon(icon)
            self.loaded_icons.clear()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self):
        global _application
        _application = self
        user32.SetProcessDPIAware()
        comctl32.InitCommonControls()
        instance = kernel32.GetModuleHandleW(None)
        class_name = 'NekoVRCDesktopWindow'
        self.large_icon = user32.LoadImageW(
            None,
            str(ICON_PATH),
            IMAGE_ICON,
            user32.GetSystemMetrics(SM_CXICON),
            user32.GetSystemMetrics(SM_CYICON),
            LR_LOADFROMFILE,
        )
        self.small_icon = user32.LoadImageW(
            None,
            str(ICON_PATH),
            IMAGE_ICON,
            user32.GetSystemMetrics(SM_CXSMICON),
            user32.GetSystemMetrics(SM_CYSMICON),
            LR_LOADFROMFILE,
        )
        self.loaded_icons = [icon for icon in (self.large_icon, self.small_icon) if icon]
        fallback_icon = user32.LoadIconW(None, IDI_APPLICATION)
        large_icon = self.large_icon or fallback_icon
        small_icon = self.small_icon or fallback_icon
        window_class = WNDCLASSEXW(
            ctypes.sizeof(WNDCLASSEXW),
            0,
            _window_proc,
            0,
            0,
            instance,
            large_icon,
            user32.LoadCursorW(None, IDC_ARROW),
            self.background_brush,
            None,
            class_name,
            small_icon,
        )
        if not user32.RegisterClassExW(ctypes.byref(window_class)):
            error = ctypes.get_last_error()
            if error != 1410:
                raise ctypes.WinError(error)
        style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX
        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            'Neko-VRC',
            style,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            self.WIDTH,
            self.HEIGHT,
            None,
            None,
            instance,
            None,
        )
        if not hwnd:
            raise ctypes.WinError()
        self.hwnd = hwnd
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.UpdateWindow(hwnd)
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        _application = None
        return int(message.wParam)
