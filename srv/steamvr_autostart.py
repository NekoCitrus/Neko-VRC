"""SteamVR application-manifest registration for opt-in auto launch."""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


APPLICATION_KEY = 'org.vrchatnext.shocking-vrchat'
MANIFEST_FILENAME = 'shocking-vrchat.vrmanifest'


class SteamVRAutoStartError(RuntimeError):
    """Raised when SteamVR cannot confirm an auto-start change."""


@dataclass(frozen=True)
class SteamVRAutoStartResult:
    enabled: bool
    pending_restart: bool
    message: str


def current_launch_target():
    """Return the binary and optional argument SteamVR should launch."""
    executable = Path(sys.executable).resolve()
    if getattr(sys, 'frozen', False):
        return executable, ''
    windowed = executable.with_name('pythonw.exe')
    if windowed.exists():
        executable = windowed
    script = Path(__file__).resolve().parents[1] / 'neko_vrc.py'
    return executable, f'"{script}"'


def build_manifest(executable, arguments=''):
    application = {
        'app_key': APPLICATION_KEY,
        'launch_type': 'binary',
        'binary_path_windows': str(Path(executable).resolve()),
        'is_dashboard_overlay': True,
        'strings': {
            'en_us': {
                'name': 'Neko-VRC',
                'description': 'VRChat OSC controller for DG-LAB Coyote and Opossum devices',
            },
            'zh_cn': {
                'name': 'Neko-VRC',
                'description': '用于 DG-LAB 郊狼和负鼠设备的 VRChat OSC 控制程序',
            },
        },
    }
    if arguments:
        application['arguments'] = str(arguments)
    return {'source': 'builtin', 'applications': [application]}


def write_manifest(config_dir, executable=None, arguments=None):
    manifest_dir = Path(config_dir) / 'steamvr'
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / MANIFEST_FILENAME
    if executable is None:
        executable, detected_arguments = current_launch_target()
        if arguments is None:
            arguments = detected_arguments
    if arguments is None:
        arguments = ''
    document = build_manifest(executable, arguments)
    temporary = manifest_path.with_suffix(manifest_path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, manifest_path)
    return manifest_path


class OpenVRApplicationsBackend:
    def __init__(self):
        self.openvr = None
        self.applications = None

    def __enter__(self):
        try:
            import openvr
        except (ImportError, OSError) as exc:
            raise SteamVRAutoStartError('OpenVR 组件未能加载，请重新安装程序。') from exc
        self.openvr = openvr
        try:
            # Background mode never starts SteamVR on its own.
            try:
                openvr.init(openvr.VRApplication_Background)
            except openvr.error_code.InitError_Init_HmdNotFound:
                # A running SteamVR instance can reject a background app when
                # no headset is connected. Utility mode is intended for
                # installers and guarantees access to IVRApplications without
                # requiring VR hardware.
                try:
                    openvr.shutdown()
                except Exception:
                    pass
                openvr.init(openvr.VRApplication_Utility)
            self.applications = openvr.VRApplications()
        except Exception as exc:
            try:
                openvr.shutdown()
            except Exception:
                pass
            raise SteamVRAutoStartError(
                '无法连接 SteamVR。请先启动 SteamVR，再保存此选项。'
            ) from exc
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.openvr is not None:
            self.openvr.shutdown()

    def is_installed(self):
        return bool(self.applications.isApplicationInstalled(APPLICATION_KEY))

    def add_manifest(self, manifest_path):
        self.applications.addApplicationManifest(str(manifest_path), False)

    def remove_manifest(self, manifest_path):
        try:
            self.applications.removeApplicationManifest(str(manifest_path))
        except (
            self.openvr.error_code.ApplicationError_NoManifest,
            self.openvr.error_code.ApplicationError_NoApplication,
            self.openvr.error_code.ApplicationError_UnknownApplication,
        ):
            return

    def set_auto_launch(self, enabled):
        self.applications.setApplicationAutoLaunch(APPLICATION_KEY, bool(enabled))

    def get_auto_launch(self):
        return bool(self.applications.getApplicationAutoLaunch(APPLICATION_KEY))


def configure_steamvr_autostart(
    config_dir,
    enabled,
    *,
    executable=None,
    arguments=None,
    backend_factory=OpenVRApplicationsBackend,
):
    """Apply and verify the requested SteamVR auto-start state."""
    manifest_path = Path(config_dir) / 'steamvr' / MANIFEST_FILENAME
    if enabled:
        manifest_path = write_manifest(config_dir, executable, arguments)
    try:
        with backend_factory() as backend:
            if enabled:
                if not backend.is_installed():
                    backend.add_manifest(manifest_path)
                if not backend.is_installed():
                    return SteamVRAutoStartResult(
                        enabled=True,
                        pending_restart=True,
                        message='清单已注册；请重启 SteamVR 后再打开本程序完成自动启动设置。',
                    )
                backend.set_auto_launch(True)
                if not backend.get_auto_launch():
                    raise SteamVRAutoStartError('SteamVR 未确认自动启动设置。')
                return SteamVRAutoStartResult(
                    enabled=True,
                    pending_restart=False,
                    message='SteamVR 跟随启动已启用。',
                )

            if backend.is_installed():
                backend.set_auto_launch(False)
                if backend.get_auto_launch():
                    raise SteamVRAutoStartError('SteamVR 未确认关闭自动启动。')
            if manifest_path.exists():
                backend.remove_manifest(manifest_path)
    except SteamVRAutoStartError:
        raise
    except Exception as exc:
        action = '启用' if enabled else '关闭'
        raise SteamVRAutoStartError(f'{action} SteamVR 跟随启动失败：{exc}') from exc

    if manifest_path.exists():
        manifest_path.unlink()
    try:
        manifest_path.parent.rmdir()
    except OSError:
        pass
    return SteamVRAutoStartResult(
        enabled=False,
        pending_restart=False,
        message='SteamVR 跟随启动已关闭。',
    )
