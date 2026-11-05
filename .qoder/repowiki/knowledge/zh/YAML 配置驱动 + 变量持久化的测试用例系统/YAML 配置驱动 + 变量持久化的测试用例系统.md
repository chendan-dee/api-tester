---
kind: configuration_system
name: YAML 配置驱动 + 变量持久化的测试用例系统
category: configuration_system
scope:
    - '**'
source_files:
    - app.py
    - engine.py
    - cases/_variables.json
    - cases/example.yaml
    - cases/1.yaml
    - yaml_checker.py
    - curl2yaml.py
    - runner.py
---

## 1. 系统概述
本仓库采用「YAML 文件即配置」的纯声明式方案，所有接口测试用例以 YAML 描述，通过 Web/CLI 加载、校验、执行。运行时还维护一份 JSON 格式的「预设变量」持久化存储，配合 `${var}` 占位符实现跨步骤变量传递与参数化。

## 2. 核心文件与职责
- `app.py`：Flask Web 服务入口，提供 YAML 文件 CRUD、运行、变量管理、数组探测等 API；负责 cases 目录扫描、YAML 读写、变量持久化（`_variables.json`）。
- `engine.py`：核心引擎，定义 `eval_setup`、`do_request`、`run_assert`、`do_extract`、`_replace/_replace_deep`、`get_by_path` 等函数，是 YAML 配置的语义解释器。
- `cases/*.yaml`：用户编写的测试用例，每个文件代表一个测试集。
- `cases/_variables.json`：全局预设变量持久化存储（含 `saved_at` 元数据）。
- `templates/index.html`：前端编辑器，直接操作上述 YAML 与变量。
- `yaml_checker.py`：YAML 结构校验规则（被 `/api/validate` 调用）。
- `curl2yaml.py`：curl → YAML 转换工具。
- `runner.py`：CLI 入口（调用 engine 的 `run_single` / `run_steps`）。

## 3. 架构与约定
### 3.1 YAML 用例格式
```yaml
name: 接口测试          # 可选，显示名
setup:                  # 可选，根级前置条件（Python 表达式）
  timestamp: datetime.now().strftime('%Y-%m-%d %H:%M:%S')
steps:
- name: Step1           # 步骤名
  request:              # HTTP 请求配置
    url: https://.../${postId}/comments
    method: GET
    headers: { ... }
    params: { ... }
    body: { ... }
    timeout: 10
  assert:               # 断言配置
    status_code: 200
  extract:              # 从响应提取变量，写入 variables
    postId: '[0].id'
  extract_list:         # 批量提取整个数组
    allItems: 'data.items'
  mock:                 # 本地 Mock（不发起真实请求）
    status_code: 200
    response: { ... }
```
- 支持两种模式：单接口（顶层 `request:`）和多步骤（`steps:` 列表）。
- 每步可拥有独立的 `setup`，覆盖根级同名变量。
- 变量引用统一使用 `${varName}` 语法，在 URL/body/headers/params 中均可出现。

### 3.2 变量体系与优先级
| 来源 | 说明 | 优先级 |
|---|---|---|
| 预设变量 `_variables.json` | 通过 `/api/variables/*` 持久化保存 | 最低 |
| 根级 `setup` | 每次运行前计算一次 | 覆盖预设变量 |
| 步骤级 `setup` | 每个 step 执行前计算 | 覆盖根级同名变量 |
| 上一步 `extract`/`extract_list` | 从响应中提取并注入 variables | 按步骤顺序生效 |
| 批量参数化 Excel 列 | 上传 Excel 后映射到变量名 | 同步骤级 setup 覆盖顺序 |

变量加载流程（SSE 流式路径）：`preset_vars = _load_variables_values()` → `setup_vars = eval_setup(cfg.setup)` → `variables = preset_vars ∪ setup_vars` → 逐 step 追加 `extract` 结果。

### 3.3 变量持久化机制
- 存储位置：`cases/_variables.json`，格式为 `{ varName: { value, saved_at } }`。
- 自动迁移：旧格式 `{ varName: value }` 会在读取时自动升级为新格式并回写。
- 提供 REST API：`GET /api/variables`、`POST /api/variables/set`（支持 bulk 与重命名）、`DELETE /api/variables/delete`、`POST /api/variables/clear`。
- 执行时仅取 `value` 字段注入 variables，`saved_at` 仅用于 UI 展示。

### 3.4 Setup 安全沙箱
`engine._SETUP_SAFE_GLOBALS` 白名单限制 `eval_setup` 可用的模块与内置函数，禁止任意 Python 代码执行，仅开放 `datetime/random/uuid/math/time/json/re/int/float/str/bool/list/dict/len/range/round/abs/max/min/sum/sorted/enumerate/zip/print` 等。

### 3.5 变量替换与取值
- `_replace(text, variables)`：字符串内 `${var}` 替换。
- `_replace_deep(obj, variables)`：递归对 dict/list/str 执行替换。
- `get_by_path(obj, path)`：支持 `[0]`、`items[0].name` 等混合路径。

## 4. 开发者应遵循的规则
1. **用例文件放在 `cases/` 目录下**，文件名以 `.yaml` 或 `.yml` 结尾，且不以 `_` 开头（否则会被 Web 界面忽略）。
2. **敏感信息不要硬编码进 YAML**，应通过 `setup` 表达式或 `_variables.json` 中的预设变量注入。
3. **变量命名使用小写字母+下划线**，避免与 Python 关键字冲突；`${var}` 中的 var 必须匹配正则 `\w+`。
4. **优先使用 `extract`/`extract_list` 串联步骤**，而非在多个步骤间重复传参。
5. **Setup 表达式只使用白名单模块和函数**，不要尝试绕过沙箱。
6. **修改 `_variables.json` 后需重启 Web 服务**才能生效（当前无热重载）。
7. **多步骤用例建议保持缩进一致**：Web 界面的 `_detect_step_indent` 会推断已有文件的缩进风格，手动编辑时请保持一致。
8. **批量参数化时 Excel 表头即为变量名**，需在 UI 中映射到 YAML 中实际引用的变量名。
