"""把 ``work/ps_search`` 加入 ``sys.path``，使测试可直接导入被测模块。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
