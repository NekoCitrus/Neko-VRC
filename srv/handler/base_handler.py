import asyncio
import math
from loguru import logger

class BaseHandler():
    
    @staticmethod
    def param_sanitizer(param):
        if isinstance(param, (tuple, list)):
            if len(param) != 1:
                raise ValueError('OSC 参数必须且只能包含一个值。')
            param = param[0]
        if isinstance(param, bool):
            param = 1 if param else 0
        elif isinstance(param, float):
            if not math.isfinite(param):
                raise ValueError('OSC 参数必须是有限数值。')
            param = min(max(param, 0.0), 1.0)
        elif isinstance(param, int):
            param = min(max(param, 0), 1)
        else:
            raise ValueError('错误的输入参数类型，仅支持 0~1 之间的 float、int 或 bool。')
        return param

    def osc_handler(self, address, *args):
        # logger.debug(f"VRCOSC: CHANN {self.channel}: {address}: {args}")
        try:
            val = self.param_sanitizer(args)
        except ValueError as exc:
            logger.warning(f'忽略无效 OSC 参数 {address}: {exc}')
            return None
        track_task = getattr(self, '_track_task', None)
        if callable(track_task):
            track_task(self._handler(val))
        else:
            asyncio.create_task(self._handler(val))
        # python-osc interprets every non-None callback return value as an OSC
        # reply address. A Task must therefore never escape this callback.
        return None
