"""Neko-VRC application entry point."""

import sys
import traceback

from loguru import logger

from shocking_vrchat import main


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logger.error(traceback.format_exc())
        if sys.platform == 'win32':
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                traceback.format_exc(),
                'Neko-VRC 启动失败',
                0x10,
            )
        else:
            raise
