# -*- coding: utf-8 -*-
"""编译互斥：跨进程文件锁 + 进程内状态机。

**路径与格式严格对齐 AI 侧 `.claude/skills/latex-writing/scripts/compile_book.py`**：
`<书稿>/.build/.<job>.lock`（job 默认 main），`O_CREAT|O_EXCL` 创建，内容写 JSON
`{pid,ts,by,token}`；读取端兼容纯 pid 文本（AI 侧就是只写 pid）。
残锁 >900s 才接管（与 AI 侧同阈值），等待上限 30s，超时抛 Busy。
"""
import json
import os
import threading
import time
import uuid

from . import config


class Busy(Exception):
    """另一个编译（本进程或 AI 会话）正持有该书的锁。"""


def lock_path(book_dir, job='main'):
    return os.path.join(book_dir, '.build', '.' + job + '.lock')


def read_lock(path):
    """读锁内容：返回 dict（兼容纯 pid 文本）；读不到返回 None。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            txt = f.read().strip()
    except OSError:
        return None
    if not txt:
        return {}
    try:
        d = json.loads(txt)
        return d if isinstance(d, dict) else {'raw': txt}
    except ValueError:
        try:
            return {'pid': int(txt)}
        except ValueError:
            return {'raw': txt}


class BuildLock:
    """一本书一把锁；with 语句使用，退出时只有 token 相符才删除。"""

    def __init__(self, book_dir, job='main', by='page', wait=None, token=None):
        self.path = lock_path(book_dir, job)
        self.by = by
        self.wait = config.LOCK_WAIT if wait is None else wait
        self.token = token or uuid.uuid4().hex[:16]
        self.held = False

    def _try_create(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            pass
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError as e:
            raise Busy('无法创建编译锁：%s' % e)
        try:
            os.write(fd, json.dumps({'pid': os.getpid(), 'ts': round(time.time(), 3),
                                     'by': self.by, 'token': self.token}).encode('utf-8'))
        finally:
            os.close(fd)
        self.held = True
        return True

    def acquire(self):
        t0 = time.time()
        while True:
            if self._try_create():
                return self
            # 残锁：进程被杀留下的锁，超过 LOCK_STALE 秒强行接管
            try:
                age = time.time() - os.path.getmtime(self.path)
            except OSError:
                continue                     # 锁刚被释放，立刻重试
            if age > config.LOCK_STALE:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                continue
            if time.time() - t0 > self.wait:
                raise Busy('另一个编译正在进行（锁 %s，已等待 %ds）'
                           % (self.path, int(time.time() - t0)))
            time.sleep(0.25)

    def release(self):
        if not self.held:
            return
        cur = read_lock(self.path)
        if cur is None or cur.get('token') in (None, self.token):
            try:
                os.unlink(self.path)
            except OSError:
                pass
        self.held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False

    def owner(self):
        """当前锁的持有者描述（供 /api/health 与前端显示）。"""
        d = read_lock(self.path) or {}
        if not d:
            return None
        return {'pid': d.get('pid'), 'by': d.get('by'), 'ts': d.get('ts')}


class CompileState:
    """进程内编译状态机（/api/compile 与 /api/health 共用，线程安全）。"""

    def __init__(self):
        self._m = threading.Lock()
        self.status = 'idle'
        self.book = None
        self.phase = ''
        self.started_at = None
        self.lock_owner = None
        self._token = None

    def begin(self, book, phase='lock'):
        """返回本次调用的 token；只有持 token 的调用能 end（并发者失败退出不得清状态）。"""
        tok = uuid.uuid4().hex
        with self._m:
            self.status = 'busy'
            self.book = book
            self.phase = phase
            self.started_at = time.time()
            self._token = tok
        return tok

    def set_phase(self, phase, lock_owner=None, token=None):
        with self._m:
            if token is not None and token != self._token:
                return
            self.phase = phase
            if lock_owner is not None:
                self.lock_owner = lock_owner

    def end(self, token=None):
        with self._m:
            if token is not None and token != self._token:
                return
            self.status = 'idle'
            self.book = None
            self.phase = ''
            self.started_at = None
            self.lock_owner = None
            self._token = None

    def busy(self):
        with self._m:
            return self.status == 'busy'

    def snapshot(self):
        with self._m:
            return {'status': self.status, 'book': self.book, 'phase': self.phase,
                    'started_at': self.started_at,
                    'elapsed': (round(time.time() - self.started_at, 1)
                                if self.started_at else None),
                    'lock_owner': self.lock_owner}
