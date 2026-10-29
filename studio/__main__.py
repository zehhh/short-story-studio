# -*- coding: utf-8 -*-
import sys

from .cli import main

if __name__ == "__main__":
    # main() 返回进程退出码（0 成功 / 1 失败 / 130 中断），必须透传，
    # 否则调度器与 `verify` 看到的水远是 0（失败被当成功）。
    sys.exit(main())
