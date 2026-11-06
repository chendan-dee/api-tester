# 工具箱

开箱即用的本地工具箱，支持接口编排、变量传递、Mock 代理、远程代理、文件互传等功能。

---

## 目录结构

```
项目文件夹/
├── 启动.bat               ← 源码启动脚本（自动定位 .venv、自动装依赖）
├── app.py                 ← Web 服务入口（Flask，全部 HTTP 接口）
├── requirements.txt       ← Python 依赖清单
├── icon.ico               ← 程序图标
├── src/                   ← 业务模块（由 app.py 加入 sys.path）
│   ├── engine.py          ← 接口测试引擎（发送/断言/提取）
│   ├── runner.py          ← 命令行运行器
│   ├── printer.py         ← 打印/报告模块
│   ├── scheduler.py       ← 定时任务调度
│   ├── seqvars.py         ← 自增变量
│   ├── dataset.py         ← 参数化数据集
│   ├── scripts.py         ← 前后置脚本执行
│   ├── script_runtime.js  ← 前后置脚本 Node 运行时
│   ├── data_generator.py  ← 数据生成器
│   ├── yaml_checker.py    ← YAML 语法校验
│   ├── curl2yaml.py       ← curl 转 YAML
│   ├── capture/           ← 抓包引擎（mitmproxy addon）
│   ├── db/                ← 数据库工作台（连接器 + 本地库）
│   ├── recorder_tool/     ← 屏幕录制工具条
│   └── 工具箱.spec  ← PyInstaller 打包配置
├── tests/                 ← 回归测试（pytest）
├── cases/                 ← 接口用例（example.json 为示例）
├── data/                  ← 持久化配置（运行时自动生成）
│   ├── _variables.json        ← 全局变量
│   ├── _workbench_notes.json  ← 工作台笔记
│   ├── _workbench_todos.json  ← 待办事项
│   ├── _mock_proxy.json       ← Mock 代理映射
│   ├── _remote_proxy.json     ← 远程代理配置
│   ├── _capture_*.json        ← 抓包配置 / 范围 / Mock
│   ├── _schedule_tasks.json   ← 定时任务
│   ├── _datasets/             ← 参数化数据集
│   ├── _schedule_params/      ← 定时任务参数化文件
│   ├── localdb/               ← 本地 SQLite 库
│   └── thumbs/                ← 文件互传缩略图缓存
├── templates/             ← HTML 页面模板（index.html 桌面端、mobile.html 手机端）
├── static/                ← 静态资源（仅 favicon.png 与本地化的第三方库文件，CSS/JS 均内联于 index.html）
├── certs/                 ← 证书（HTTPS 拦截用，含自签 CA 私钥）
├── transfer/              ← 文件互传接收的文件 / 录屏（运行时自动生成）
├── logs/                  ← 运行日志（运行时自动生成）
└── dist/                  ← PyInstaller 打包输出（可随时删除后重新打包）
```

---

## 启动方式

### 方式一：双击 exe（推荐）

直接双击 `工具箱.exe`，会自动打开浏览器访问 `http://本机IP:5050`。

> exe 是无控制台窗口模式，后台运行。如需退出，在任务管理器中结束进程即可。

### 方式二：源码启动

1. 安装 Python 3.8+
2. 安装依赖：
   ```
   pip install -r requirements.txt
   ```
3. 双击 `启动.bat`，或命令行运行：
   ```
   python app.py
   ```

### 外部依赖说明

- **无外网 CDN 依赖**：页面所需 CSS/JS 均内联在 `templates/index.html` 或已本地化到 `static/`，断网环境可正常使用。
- **ffmpeg（可选）**：文件互传功能收到 H.265 编码视频时，会调用系统 PATH 中的 ffmpeg 转码为 H.264 以保证浏览器可播放；未安装 ffmpeg 不影响其他功能，仅跳过转码。

### 回归测试

```
cd tests
python -m pytest -q
```

`tests/conftest.py` 已把 `src/` 加入 `sys.path`，无需额外配置环境变量。

---

## 主要功能

### 接口测试

- 在 `cases/` 目录下创建 `.yaml` 文件，按 YAML 格式定义接口
- 支持单接口 / 多步骤串联测试
- 支持变量传递：上一个接口的返回值可传递给下一个接口
- 支持响应取值：`data.token`、`[0].id` 等路径语法

### 全局变量

- 可在「变量管理」标签页中设置全局变量
- 支持 Python 表达式动态计算（如 `datetime.now()`）
- 变量可在 YAML 中用 `{{变量名}}` 引用

### 代理 Mock

- 拦截指定 URL 并返回自定义的 Mock 数据，无需启动外部 Mock 服务，直接在界面中配置映射
- 支持两种接入方式：通过代理 URL 直接访问，或开启系统代理让流量自动经过本服务
- 支持配置多个响应报文并一键切换，方便测试不同场景

### 远程映射

- 类似 Charles 的 Remote Mapping，配置远程主机和路径后，匹配的请求返回本地 mock，未匹配的转发到真实服务器（透明代理）
- 支持 PAC 自动代理配置，开启后仅已启用的域名走代理，其他流量直连，不影响正常上网
- 支持 HTTP 和 HTTPS 代理，HTTPS 需安装内置 CA 证书

### 工作台

- 笔记：记录测试过程中的想法和注意事项
- 待办事项：跟踪测试任务进度

### 文件互传

- 在同一局域网内，设备之间传输文件
- 支持图片、视频、任意文件类型
- 扫码即可连接

### 手机端页面（/mobile）

- 手机浏览器访问 `http://本机IP:5050/mobile` 即可打开移动端页面
- 适合在手机上扫码连接文件互传、上传/下载文件

### 数据生成器

- 内置姓名、手机号、身份证、邮箱、地址等 Mock 数据生成
- 一键复制生成的测试数据

---

## 数据持久化

所有用户数据都保存在**项目文件夹内**，随项目一起迁移：

| 数据 | 存储位置 |
|------|---------|
| YAML 接口用例 | `cases/` 目录 |
| 全局变量配置 | `data/_variables.json` |
| 工作台笔记 | `data/_workbench_notes.json` |
| 待办事项 | `data/_workbench_todos.json` |
| Mock 代理映射 | `data/_mock_proxy.json` |
| 远程代理配置 | `data/_remote_proxy.json` |
| 文件传输设置 | `data/` 下相关文件 |

> **迁移时**：直接复制整个项目文件夹到新位置即可，所有数据跟着走。

---

## 安全注意事项

本工具定位为**内网测试工具**，默认不提供任何访问控制，请勿暴露到公网：

- **服务绑定 0.0.0.0**：同网段内任何设备都能直接访问 5050 端口的页面与接口。
- **代理与删除类接口无认证**：Mock 代理、远程代理的增删改、用例/文件删除等接口均无鉴权，任何能访问该端口的人都能操作。
- **自签 CA 私钥存放在 `certs/`**：`api-tester-ca-key.pem` 是本工具生成的自签 CA 私钥，泄露后可被用于伪造 HTTPS 拦截；请勿将 `certs/` 目录上传到公开仓库或分享给不信任的人。
- **建议仅在可信局域网内使用**；如需长期后台运行，建议配合防火墙限制 5050 端口的访问来源。

---

## 常见问题

**Q: 端口被占用怎么办？**
A: `启动.bat` 会自动清理端口 5050 上的旧进程。如果手动启动，先 `taskkill /F /IM python.exe` 再运行。

**Q: 页面打开显示"接口不存在"？**
A: 检查是否有多个进程占用了 5050 端口，杀掉所有 Python 和 exe 进程后重试。

**Q: 如何修改端口？**
A: 在 `app.py` 中找到 `app.run(host="0.0.0.0", port=5050)`，修改 port 值。

**Q: 需要 HTTPS 代理功能？**
A: 程序首次运行会自动生成 CA 证书，安装到系统信任列表后即可拦截 HTTPS 请求。