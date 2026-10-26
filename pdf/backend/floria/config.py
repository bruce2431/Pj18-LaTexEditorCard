# -*- coding: utf-8 -*-
"""路径与常量：全后端唯一配置来源。"""
import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # <预览根>/pdf/backend
# 后端可以放在某张卡目录内（当前 = pdf/backend）。此时卡根要上跳一层到预览根，
# 否则静态托管会把 <预览根>/pdf 当卡根，log/ 卡与 pdf/ 卡都找不到。
_parent = os.path.dirname(APP_DIR)
CARD_ROOT = os.path.dirname(_parent) if os.path.isfile(
    os.path.join(_parent, 'card.json')) else _parent
VENDOR_DIR = os.path.join(APP_DIR, 'vendor')
TEX_DIR = os.path.join(APP_DIR, 'tex')

# 书稿根：main() 启动时设定（原来的 module-global BOOKS_ROOT，这里收敛到一处）
BOOKS_ROOT = None


def set_books_root(path):
    global BOOKS_ROOT
    BOOKS_ROOT = os.path.abspath(path)
    return BOOKS_ROOT


IMG_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp'}

CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
    '.bmp': 'image/bmp', '.woff': 'font/woff', '.woff2': 'font/woff2',
    '.ttf': 'font/ttf', '.ico': 'image/x-icon', '.map': 'application/json',
    '.pdf': 'application/pdf',
}

# 递归发现书稿时跳过的目录名（隐藏目录另按前缀 '.' 跳过）
SKIP_DIRS = {'node_modules', '__pycache__', 'venv', '.venv', 'site-packages'}

# 编译账本
LEDGER_NAME = 'compile-history.jsonl'
LEDGER_MAX = 200

# 超时（秒）
COMPILE_TIMEOUT = 420          # 单次编译进程
DIRECT_TIMEOUT = 840           # 无 latexmk 时直驱两遍的总预算
TOOL_PROBE_TIMEOUT = 5         # doctor 探测工具版本
LOCK_STALE = 900               # 跨进程锁残锁接管阈值（与 compile_book.py 一致）
LOCK_WAIT = 30                 # 等锁上限

# 书稿发现缓存 TTL（前端每 4s 轮询 /api/books）
BOOKS_TTL = 2.0
