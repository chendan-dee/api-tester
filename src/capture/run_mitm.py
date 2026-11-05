#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""capture/run_mitm.py — mitmdump 启动入口（供 engine.py 以子进程调用）

用法示例:
    python run_mitm.py -s addon.py --listen-port 8899 --set confdir=... --quiet
"""

from mitmproxy.tools.main import mitmdump

if __name__ == "__main__":
    mitmdump()
