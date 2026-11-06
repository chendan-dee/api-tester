# -*- coding: utf-8 -*-
"""让 tests 目录下的用例直接 import 技能源码（src/）"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
for p in (os.path.join(SKILL, "src"), SKILL):
    if p not in sys.path:
        sys.path.insert(0, p)
