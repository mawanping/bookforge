"""让 ``python -m bookforge`` 等价于 ``bookforge``。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
