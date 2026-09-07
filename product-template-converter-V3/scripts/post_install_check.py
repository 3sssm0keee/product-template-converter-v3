"""Skill 安装或升级后运行的依赖自检入口。"""

# A-ABL-06：依赖自检薄入口候选，尚未消融验证；先迁移调用方再决定退役。
from preflight_dependencies import main


if __name__ == "__main__":
    raise SystemExit(main())
