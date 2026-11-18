"""PyInstaller 入口：等价于 `python bin/v2t.py`"""
import os
import sys
from pathlib import Path

# 冻结后 bin/ 会被解包到 _MEIPASS 或旁边，这里把工程根定到可执行文件所在目录
if getattr(sys, "frozen", False):
    os.environ.setdefault("V2T_ROOT", str(Path(sys.executable).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from v2t import main  # noqa: E402

if __name__ == "__main__":
    main()
