---
kind: dependency_management
name: 依赖管理：无声明式清单，纯 pip install 安装第三方库
category: dependency_management
scope:
    - '**'
source_files:
    - README.md
---

本仓库未采用任何 Python 包管理器的声明式清单文件（如 `requirements.txt`、`pyproject.toml`、`Pipfile`、`poetry.lock` 等），也未使用 `setup.py`/`setup.cfg` 或 `go.mod`/`package.json`。所有第三方依赖通过 README.md 中的单行命令直接安装：

```bash
pip install requests pyyaml colorama pandas openpyxl
```

- **依赖来源**：全部来自 PyPI 官方源，未发现私有镜像、GOPRIVATE 或 vendor 目录。
- **版本约束**：无任何版本锁定，安装时一律取最新版，不存在 lockfile。
- **可选依赖**：`pandas`、`colorama`、`yaml` 在代码中采用运行时 `import` 延迟加载（见 `engine.py`、`printer.py`、`runner.py`），避免在未安装这些包时报错，属于“软依赖”模式。
- **标准库**：其余导入均为 Python 标准库（`json`、`os`、`re`、`sys`、`datetime`、`uuid` 等），无需额外安装。

开发者需自行维护该 `pip install` 命令与代码实际 import 的一致性；仓库内没有自动化校验或更新机制。