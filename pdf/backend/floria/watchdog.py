# -*- coding: utf-8 -*-
"""源码指纹轮询 + 空闲自退（仅 --auto-restart 生效）。

网关每次代理请求都会调 ensureBackend（已读网关源码确认），所以进程退出后
下一次请求会把新代码拉起来。编译中检测到变更不算数：等 state 空闲后再退。
"""
import os
import threading
import time

from . import BUILD_ID, log, source_fingerprint


class Watchdog(threading.Thread):
    def __init__(self, state, interval=5.0):
        super().__init__(daemon=True, name='floria-watchdog')
        self.state = state
        self.interval = interval
        self.baseline = BUILD_ID
        self.stop_flag = threading.Event()

    def run(self):
        log.info('自动重启监视已启动（build %s，每 %.0fs 检查）'
                 % (self.baseline, self.interval))
        while not self.stop_flag.wait(self.interval):
            cur = source_fingerprint()[0][:12]
            if cur == self.baseline:
                continue
            if self.state.busy():
                continue          # 编译中：等编完再退
            log.warn('检测到源码变更（%s → %s），空闲退出以便网关重拉'
                     % (self.baseline, cur))
            try:
                os._exit(0)
            except Exception:     # noqa: BLE001
                return

    def stop(self):
        self.stop_flag.set()
