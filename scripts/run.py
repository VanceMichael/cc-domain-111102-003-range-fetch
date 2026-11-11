#!/usr/bin/env python3
"""隔离模式启动器。

下载目录中的内容是不可信数据，运行时使用 ``python3 -I`` 可阻止
PYTHONPATH、用户 site-packages 与脚本目录注入。``-I`` 下解释器不会
自动加入任何目录，因此这里显式把**项目根**（本文件所在目录）加入
sys.path，而不是依赖当前工作目录。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
