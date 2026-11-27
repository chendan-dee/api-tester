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
│   ├── settings_store.py  ← 统一配置存储（data/settings.json）
│   ├── seqvars.py         ← 自增变量
│   ├── dataset.py         ← 参数化数据集
│   ├── scripts.py         ← 前后置脚本执行
│   ├── script_runtime.js  ← 前后置脚本 Node 运行时
│   ├── data_generator.py  ← 数据生成器
│   ├── yaml_checker.py    ← YAML 语法校验
│   ├── curl2yaml.py       ← curl 转 YAML
│   ├── capture/           ← 抓包引擎（mitmproxy addon）
│   ├── db/                ← 数据库工作台（连接器 + 本地库）
│   ├── phone_mirror/      ← 手机投屏子服务（Node 独立进程，自带 adb/hdc/scrcpy-server 与 npm 依赖）
│   ├── recorder_tool/     ← 屏幕录制工具条
│   └── 工具箱.spec  ← PyInstaller 打包配置
├── tests/                 ← 回归测试（pytest）
├── cases/                 ← 接口用例（example.json 为示例）
├── data/                  ← 持久化配置（运行时自动生成）
│   ├── settings.json          ← 统一配置：变量/环境/数据源/Mock/远程映射/设置/抓包端口/调度/待办/互传门禁
│   ├── _workbench_notes.json  ← 工作台笔记
│   ├── _workbench_quick_notes.json ← 随心记
│   ├── _transfer_items.json   ← 文件互传记录（高频轮询写）
│   ├── _transfer_devices.json ← 互传设备名
│   ├── _capture_*.json        ← 抓包范围 / 改包规则 / 引擎配置（mitmproxy 子进程直读，跨进程不可合并）
│   ├── _record_status.json    ← 录制控制面板状态（PyQt 子进程读写）
│   ├── _datasets/             ← 参数化数据集
│   ├── _schedule_params/      ← 定时任务参数化文件
│   ├── _schedule_runs/        ← 定时任务执行结果
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

### 方式一：双击 exe（需先本地打包）

直接双击 `工具箱.exe`，会自动打开浏览器访问 `http://本机IP:5050`。

> exe 是构建产物（`*.exe` 不进仓库），用 `src/工具箱.spec` 跑 PyInstaller 即可重新生成；没打包时用方式二。
> exe 是无控制台窗口模式，后台运行。如需退出，在任务管理器中结束进程即可。

### 方式二：源码启动

1. 安装 Python 3.12+（requirements 里的 mitmproxy 12 / numpy 2 / opencv 5 需要）
2. 双击 `启动.bat`（会自动定位解释器，依赖缺失时一键安装）；或手动：
   ```
   pip install -r requirements.txt
   python app.py
   ```

### 外部依赖说明

- **无外网 CDN 依赖**：页面所需 CSS/JS 均内联在 `templates/index.html` 或已本地化到 `static/`，断网环境可正常使用。
- **手机投屏依赖已随仓库**：`src/phone_mirror/tools/` 内含 `adb.exe`（Android）、`hdc.exe`（鸿蒙）、`scrcpy-server.jar`，`src/phone_mirror/node_modules/` 内含 `ws` / `hdckit` 及其全部依赖链，clone 后无需 `npm install`；仅需本机装 Node 18+ 运行时。
- **证书首次自动生成**：`certs/` 不进仓库（含 CA 私钥），首次启动时自动生成自签 CA，扫码装到手机即可抓 HTTPS。
- **录制编码自带 ffmpeg**：`imageio-ffmpeg` 随 pip 装好并自带二进制，录制按 CRF 控画质；系统 PATH 里的 ffmpeg 只影响文件互传收到 H.265 视频时的转码，未安装不影响其他功能。

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
- 数据集合（`data/_datasets/`）：导入 Excel/CSV 当参数化数据表，接口里用 `${列名}` 引用，运行时逐行执行；行可标记为「异常入参」，批量执行按「拦截成功 / 未拦截」统计
- 内置动态变量 `${__random_mobile}` / `${__random_uuid}` / `${__timestamp}` / `${__timestamp_ms}`：每次运行现生成，同一行的多个步骤共用同一组值，执行结果里显示替换后的真实值

### 全局变量

- 可在「变量管理」标签页中设置全局变量
- 支持 Python 表达式动态计算（如 `datetime.now()`）
- 变量可在 YAML 中用 `{{变量名}}` 引用

### 代理 Mock

- 拦截指定 URL 并返回自定义的 Mock 数据，无需启动外部 Mock 服务，直接在界面中配置映射
- 支持两种接入方式：通过代理 URL 直接访问，或开启系统代理让流量自动经过本服务
- 支持配置多个响应报文并一键切换，方便测试不同场景
- 响应报文只有两种形态，由页签决定：普通报文（完全 Mock，返回你配的状态码/响应头/响应体）/ 真实报文（请求照常打到真实服务，命中后先挂起，看清或改完报文再放行，60 秒未处理自动原样放行）
- 抓包页的「改包 Mock」另有返回配置报文 / 仅延时透传 / 真实响应改字段三种模式；抓包侧由引擎在响应阶段就地改写，请求天然只发一次，无需缓存

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

- 内置姓名、手机号、身份证、邮箱、地址、银行卡等 27 类 Mock 数据生成
- 一键复制生成的测试数据
- 数据集合页可直接「批量造数」：勾字段、填条数（1-500）、按需改列名，生成结果直接落进当前表（追加到现有行或整表覆盖），不用「生成 → 复制 → 粘贴」三步走

---

## 数据持久化

所有用户数据都保存在**项目文件夹内**，随项目一起迁移：

| 数据 | 存储位置 |
|------|---------|
| YAML 接口用例 | `cases/` 目录 |
| 变量 / 环境 / 数据源 / Mock 代理 / 远程映射 / 定时任务 / 待办 / 抓包端口 / 互传门禁 | `data/settings.json`（一项一个顶层段落） |
| 工作台笔记 / 随心记 | `data/_workbench_notes.json`、`data/_workbench_quick_notes.json`（打字自动保存，单文件更安全） |
| 文件互传记录 / 设备 | `data/_transfer_*.json` |
| 抓包范围 / 改包规则 / 引擎配置 | `data/_capture_*.json`（mitmproxy 子进程跨进程直读） |
| 录制面板状态 | `data/_record_status.json` |

> 旧版分散的 `_variables.json` / `_mock_proxy.json` / `config.json` 等在启动时会**一次性并入 `settings.json`**（逐项校验通过后删源文件），旧文件不需要手动处理。

> **迁移时**：直接复制整个项目文件夹到新位置即可，所有数据跟着走。

---

## 安全注意事项

本工具定位为**内网测试工具**，默认不提供任何访问控制，请勿暴露到公网：

- **服务绑定 0.0.0.0**：同网段内任何设备都能直接访问 5050 端口的页面与接口。
- **代理与删除类接口无认证**：Mock 代理、远程代理的增删改、用例/文件删除等接口均无鉴权，任何能访问该端口的人都能操作。
- **自签 CA 私钥存放在 `certs/`**：`api-tester-ca-key.pem` 是本工具生成的自签 CA 私钥（RSA-2048，明文 PEM，无口令），泄露后可用于伪造任意网站的 HTTPS；请勿将 `certs/` 目录上传到公开仓库或分享给不信任的人。
  - 已做的加固：启动与生成证书时用 `icacls` 把 `certs/` 收紧为「当前用户 + SYSTEM + Administrators」可读，并断开从上级目录继承的权限；停止抓包时删除 `certs/mitm/mitmproxy-ca.pem` 这份含私钥的明文副本（下次启动抓包时自动重新注入）。
  - 上述措施只防「同机其他账户直读」和「副本残留」，**挡不住整个 `certs` 目录被拷走或上传网盘**；若需要更强的保护，可自行给私钥 PEM 加 passphrase，或用 Windows 本机凭据（DPAPI）加密存放（代价：重装系统或换 Windows 用户后必须重建 CA，所有设备要重装证书）。
  - 换机器：把 `certs/` 一并拷过去可沿用同一套 CA，设备无需重装；否则首次下载/安装证书时会生成全新 CA，所有设备都要装新证书。
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