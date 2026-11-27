'use strict';
/**
 * phone_mirror/server.js — 极简手机投屏服务（只做：画面 + 点击）
 *
 * 链路：adb + scrcpy-server.jar（H.264 硬编）→ WebSocket → 浏览器 WebCodecs 直解
 * 触控：scrcpy 控制通道 INJECT_TOUCH_EVENT，坐标使用视频帧空间
 * 没有的功能：录制、回放、路径配置、安装 APK、多设备同屏、占用/接管
 *
 * 只监听 127.0.0.1:9300，本机浏览器访问即安全上下文，无需 HTTPS 证书。
 */
const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const crypto = require('crypto');
const { spawn, execFile } = require('child_process');
const { promisify } = require('util');

const execFileAsync = promisify(execFile);

// 日志落系统临时目录（不污染工作区）：从工具箱 iframe 启动时看不到控制台，
// 出问题只看这个文件即可，超过 512KB 自动清空重写
const LOG_FILE = path.join(os.tmpdir(), 'phone_mirror.log');
function log(...parts) {
  const line = `${new Date().toLocaleString('sv-SE')} ${parts.join(' ')}`;
  try {
    if (fs.existsSync(LOG_FILE) && fs.statSync(LOG_FILE).size > 512 * 1024) fs.truncateSync(LOG_FILE, 0);
    fs.appendFileSync(LOG_FILE, line + '\n');
  } catch (_) { /* 日志写不了不影响主流程 */ }
  try { process.stdout.write(line + '\n'); } catch (_) {}
}

// ws 优先用本地安装，缺失时回退复用 mobile-screen-mirror 的依赖（不重复下载）
let WebSocketServer;
try {
  ({ WebSocketServer } = require('ws'));
} catch (_) {
  const fallback = path.resolve(__dirname, '../../../mobile-screen-mirror/node_modules/ws');
  ({ WebSocketServer } = require(fallback));
}

// ── 常量 ────────────────────────────────────────────────────────────────
const HOST = '127.0.0.1';
const PORT = Number(process.env.PHONE_MIRROR_PORT || 9300);
const SKILLS = path.resolve(__dirname, '../../..');           // .qoder/skills
const TOOLS = path.join(SKILLS, 'mobile-screen-mirror', 'tools');
// 本技能自带的工具目录优先（如新版鸿蒙 hdc），缺失时回退共用的 mobile-screen-mirror
const LOCAL_TOOLS = path.join(__dirname, 'tools');
function pickTool(...relParts) {
  const local = path.join(LOCAL_TOOLS, ...relParts);
  if (fs.existsSync(local)) return local;
  const shared = path.join(TOOLS, ...relParts);
  return fs.existsSync(shared) ? shared : null;
}
const ADB = pickTool('platform-tools', 'adb.exe') || 'adb';
const HDC = pickTool('commandline-tools', 'hdc.exe') || 'hdc';
const JAR = pickTool('scrcpy-server.jar') || path.join(TOOLS, 'scrcpy-server.jar');

const SCRCPY_VERSION = '4.1';
const DEVICE_META_SIZE = 64;
const CODEC_ID_SIZE = 4;
const SESSION_META_SIZE = 12;
const FRAME_HEADER_SIZE = 12;
const FLAG_CONFIG = BigInt('0x4000000000000000');
const FLAG_KEY = BigInt('0x2000000000000000');
const FLAG_SESSION = BigInt('0x8000000000000000');

// 鸿蒙 NEXT 走 hdc + snapshot_display 截图流（JPEG），与 scrcpy 的 H.264 用同一个 9 字节帧头，
// 只把 flag 定为 3 区分，前端据此走图片绘制而不是视频解码
const FLAG_JPEG = 3;
// 单路抓图约 1.8fps，多路并发能把帧率抬上去（瓶颈在手机侧合成截图，不是 USB 带宽）。
// 实测桌面类复杂画面：3 路 3.6fps、4 路 4.8fps、5 路掉回 3.6fps（手机侧已过载），取 4
const HARMONY_POLL_CONCURRENCY = 4;

// hdckit 只在 mobile-screen-mirror 里装过（agent.so 也在它的包内），直接复用，不重复下载
function loadHdckit() {
  const local = path.join(__dirname, 'node_modules', 'hdckit');
  const shared = path.join(SKILLS, 'mobile-screen-mirror', 'node_modules', 'hdckit');
  return require(fs.existsSync(local) ? local : shared);
}

// 从 JPEG 的 SOF 段读真实宽高：旋转/折叠屏切换后用它同步触控坐标系
function jpegSize(buf) {
  let i = 2;
  while (i + 9 < buf.length) {
    if (buf[i] !== 0xff) { i++; continue; }
    const m = buf[i + 1];
    if (m === 0xd8 || m === 0x01 || (m >= 0xd0 && m <= 0xd7)) { i += 2; continue; }
    const len = buf.readUInt16BE(i + 2);
    if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) {
      return { height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
    }
    i += 2 + len;
  }
  return null;
}

// 高清固定档：设备原生分辨率 + 60fps + 16Mbps（带宽不足时由前端丢帧，不降分辨率）
const SCRCPY_ARGS = {
  max_size: 0,
  max_fps: 60,
  video_bit_rate: 32000000,   // 原生分辨率 1080×2358 下 16M 动态画面会有块效应，局域网带宽够，抬到 32M
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── adb / hdc 小工具 ───────────────────────────────────────────────────
async function adb(serial, ...args) {
  const { stdout } = await execFileAsync(ADB, ['-s', serial, ...args], {
    timeout: 20000, windowsHide: true, maxBuffer: 8 * 1024 * 1024,
  });
  return stdout;
}

async function adbShell(serial, cmd) {
  return adb(serial, 'shell', cmd);
}

// 平台识别：鸿蒙 4.x 仍走 adb，只是标注不同；NEXT 只出现在 hdc
const platCache = new Map(); // serial -> {label, at}
async function detectLabel(serial) {
  const cached = platCache.get(serial);
  if (cached && Date.now() - cached.at < 60000) return cached.label;
  let label = 'Android';
  try {
    const v = (await adb(serial, 'shell', 'getprop hw_sc.build.platform.version')).trim();
    if (v) label = '鸿蒙 ' + v;
  } catch (_) { /* 非华为设备没有该属性 */ }
  platCache.set(serial, { label, at: Date.now() });
  return label;
}

// 手动断开的设备：先从列表里拿掉。等它真下线（拔线/关无线）后自动清标记，重新接上就会再出现；
// 点顶栏「刷新」也会清空——那是用户主动要求重新扫描全部设备的意思
const hidden = new Set();

async function listDevices() {
  const out = [];
  const seen = new Set();
  const alive = new Set();   // 本轮确认在线的设备，用来回收 hidden
  try {
    const txt = (await execFileAsync(ADB, ['devices', '-l'], { timeout: 15000, windowsHide: true })).stdout;
    for (const line of txt.split(/\r?\n/).slice(1)) {
      const m = line.match(/^(\S+)\s+(\w+)(.*)$/);
      if (!m || m[2] !== 'device') continue;
      const model = (m[3].match(/model:(\S+)/) || [])[1] || m[1];
      seen.add(m[1]);
      alive.add(m[1]);
      out.push({ id: m[1], model: model.replace(/_/g, ' '), platform: await detectLabel(m[1]), kind: 'adb' });
    }
  } catch (_) { /* adb 不可用 */ }
  try {
    // -v 才能拿到连接方式与授权状态：`<id>  USB|UART|TCP  Ready|Unauthorized  ...`
    const txt = (await execFileAsync(HDC, ['list', 'targets', '-v'], { timeout: 15000, windowsHide: true })).stdout;
    for (const line of txt.split(/\r?\n/)) {
      const cells = line.trim().split(/\s+/);
      const id = cells[0];
      if (!id || /^\[Empty\]$/.test(id) || /^List$/i.test(id) || /^List of targets/.test(line.trim())) continue;
      // UART/串口条目（如 COM1）不是真机，投屏用不了，不列出来干扰选择
      if (/^(UART|COM\d+)$/i.test(cells[1] || '') || /^COM\d+$/i.test(id)) continue;
      // 拔线后 hdc 不会立刻删条目，而是留一条 Offline（shell 已报 E001005）——这不是可用设备
      if (/^Offline$/i.test(cells[2] || '')) continue;
      if (seen.has(id)) continue;
      seen.add(id);
      alive.add(id);
      // 手机没点「允许调试」时状态是 Unauthorized，先列出来并说明原因，避免用户以为没识别到
      const authorized = !/Unauthorized/i.test(line);
      out.push({
        id,
        model: 'HarmonyOS NEXT',
        platform: authorized ? '鸿蒙 NEXT' : '鸿蒙 NEXT（待手机授权）',
        kind: 'hdc',
      });
    }
  } catch (_) { /* hdc 不可用 */ }
  // hidden 只压住「还挂在列表上」的设备；设备真不在了就清掉，重新插上就能出现
  for (const id of hidden) if (!alive.has(id)) hidden.delete(id);
  return out.filter((d) => !hidden.has(d.id));
}

// ── scrcpy 会话（单设备单会话） ────────────────────────────────────────
class ScrcpySession {
  constructor(serial) {
    this.serial = serial;
    this.clients = new Set();        // /ws/stream 的浏览器连接
    // scrcpy 服务端用 Integer.parseInt(scid, 16) 解析，必须在 0x00000000–0x7fffffff 内；
    // 超出会直接 NumberFormatException 退出（表现为画面永远等不到）
    this.scid = (crypto.randomBytes(4).readUInt32BE(0) & 0x7fffffff).toString(16).padStart(8, '0');
    this.port = 27200 + crypto.randomInt(0, 400);
    this.proc = null;
    this.video = null;
    this.control = null;
    this.buf = Buffer.alloc(0);
    this.phase = 'dummy';
    this.pending = 1;
    this.config = null;
    this.width = 0;
    this.height = 0;
    this.started = false;
    this.closed = false;
  }

  // 广播给所有观看者
  #json(obj) {
    const s = JSON.stringify(obj);
    for (const c of this.clients) if (c.readyState === 1) c.send(s);
  }

  #frame(flag, ptsUs, payload) {
    const head = Buffer.alloc(9);
    head.writeUInt8(flag, 0);
    head.writeDoubleBE(ptsUs, 1);
    // 观看端积压超过 4MB 时丢帧（只保关键帧，避免延迟越拖越大）
    for (const c of this.clients) {
      if (c.readyState !== 1) continue;
      if (flag !== 2 && c.bufferedAmount > 4 * 1024 * 1024) continue;
      c.send(Buffer.concat([head, payload]));
    }
  }

  async start() {
    log(`[mirror] 启动 ${this.serial} scid=${this.scid} port=${this.port}`);
    // 清残留：设备端进程 + 旧 forward（否则抽象 socket 被占）
    await adbShell(this.serial, 'pkill -f com.genymobile.scrcpy').catch(() => {});
    await execFileAsync(ADB, ['-s', this.serial, 'forward', '--remove-all']).catch(() => {});
    await sleep(400);

    if (!fs.existsSync(JAR)) throw new Error('scrcpy-server.jar 不存在：' + JAR);
    await adb(this.serial, 'push', JAR, '/data/local/tmp/scrcpy-server.jar');
    log('[mirror] jar 已推送');
    await adb(this.serial, 'forward', `tcp:${this.port}`, `localabstract:scrcpy_${this.scid}`);
    log('[mirror] forward 已建立，等待 scrcpy 输出');

    const args = [
      '-s', this.serial, 'shell',
      'CLASSPATH=/data/local/tmp/scrcpy-server.jar',
      'app_process', '/', 'com.genymobile.scrcpy.Server',
      SCRCPY_VERSION,
      `scid=${this.scid}`,
      'tunnel_forward=true',
      'audio=false',
      'control=true',
      'cleanup=false',
      'send_frame_meta=true',
      'send_stream_meta=true',
      'send_dummy_byte=true',
      'send_device_meta=true',
      `max_size=${SCRCPY_ARGS.max_size}`,
      `max_fps=${SCRCPY_ARGS.max_fps}`,
      `video_bit_rate=${SCRCPY_ARGS.video_bit_rate}`,
      'log_level=info',
    ];
    this.proc = spawn(ADB, args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    this.proc.stdout.on('data', (d) => log('[scrcpy] ' + d.toString().trimEnd()));
    this.proc.stderr.on('data', (d) => log('[scrcpy!] ' + d.toString().trimEnd()));
    this.proc.on('exit', (code) => {
      log(`[mirror] scrcpy 退出 code=${code}`);
      this.#teardown('scrcpy 已退出');
    });

    // 等 scrcpy 真正开始输出再接管：adb forward 在服务端还没 listen 时也会「连上」再立刻关闭，
    // 只有收到 dummy 字节才证明这条连接是画面口，这时才能建控制口（服务端按顺序 accept 两条）
    const { socket: video, first } = await this.#connectVideo(this.port, 20, 300);
    this.video = video;
    this.buf = Buffer.from(first);

    video.on('data', (chunk) => {
      this.buf = Buffer.concat([this.buf, chunk]);
      this.#parse();
    });
    video.on('close', () => this.#teardown('画面连接断开'));
    video.on('error', () => this.#teardown('画面连接异常'));
    this.#parse();          // 先把已读到的 dummy 吃掉，phase 推进到 device meta
    video.resume();         // #connectVideo 里 pause 过，必须恢复，否则后续 meta/帧永远读不到

    this.control = net.createConnection({ host: '127.0.0.1', port: this.port });
    this.control.on('error', () => { this.control = null; });
    this.control.on('data', () => {});
  }

  // 连上不算数，要读到第一字节（dummy）才算就绪；中途被关闭就重试
  #connectVideo(port, times, gap) {
    return new Promise((resolve, reject) => {
      let left = times;
      const tryOnce = () => {
        const sock = net.createConnection({ host: '127.0.0.1', port });
        let done = false;
        const timer = setTimeout(() => fail(new Error('等待画面流超时')), 2500);
        const fail = (err) => {
          if (done) return;
          done = true;
          clearTimeout(timer);
          sock.destroy();
          log(`[mirror] 画面流未就绪（${err.message}），剩余重试 ${left - 1}`);
          if (--left <= 0) reject(err);
          else setTimeout(tryOnce, gap);
        };
        sock.once('data', (first) => {
          if (done) return;
          done = true;
          clearTimeout(timer);
          sock.pause();     // 空档期先攒着，避免 meta 数据在绑定监听前被丢掉
          resolve({ socket: sock, first });
        });
        sock.once('close', () => fail(new Error('画面连接被关闭')));
        sock.once('error', (e) => fail(e));
      };
      tryOnce();
    });
  }

  // v4.1 协议：dummy(1) → 设备名(64) → codecID(4) → 会话元数据(12) → 帧(12B 头 + 数据)
  #parse() {
    while (!this.closed) {
      if (this.phase === 'dummy') {
        if (this.buf.length < 1) return;
        this.buf = this.buf.subarray(1);
        this.phase = 'meta'; this.pending = DEVICE_META_SIZE; continue;
      }
      if (this.phase === 'meta') {
        if (this.buf.length < DEVICE_META_SIZE) return;
        this.buf = this.buf.subarray(DEVICE_META_SIZE);
        this.phase = 'codec'; this.pending = CODEC_ID_SIZE; continue;
      }
      if (this.phase === 'codec') {
        if (this.buf.length < CODEC_ID_SIZE) return;
        this.buf = this.buf.subarray(CODEC_ID_SIZE);
        this.phase = 'session'; this.pending = SESSION_META_SIZE; continue;
      }
      if (this.phase === 'session') {
        if (this.buf.length < SESSION_META_SIZE) return;
        this.width = this.buf.readInt32BE(4);
        this.height = this.buf.readInt32BE(8);
        this.buf = this.buf.subarray(SESSION_META_SIZE);
        this.phase = 'frames';
        log(`[mirror] ${this.serial} 画面 ${this.width}x${this.height}`);
        this.#json({ type: 'start', codec: 'h264', width: this.width, height: this.height });
        continue;
      }
      // frames
      if (this.buf.length < FRAME_HEADER_SIZE) return;
      const ptsFlags = this.buf.readBigUInt64BE(0);
      const size = this.buf.readUInt32BE(8);
      // v4.1 session 包（编码器重启/旋转/客户端 resize）没有 payload：
      // 12 字节头里低 32 位是 width，size 字段是 height，不能按普通帧去等 payload
      if (ptsFlags & FLAG_SESSION) {
        this.buf = this.buf.subarray(FRAME_HEADER_SIZE);
        const w = Number(ptsFlags & 0xffffffffn);
        const h = size;
        if (w > 0 && h > 0) {
          this.width = w;
          this.height = h;
          log(`[mirror] ${this.serial} 画面重建 ${this.width}x${this.height}`);
          this.#json({ type: 'resize', width: this.width, height: this.height });
        }
        continue;
      }
      if (this.buf.length < FRAME_HEADER_SIZE + size) return;
      const payload = this.buf.subarray(FRAME_HEADER_SIZE, FRAME_HEADER_SIZE + size);
      this.buf = this.buf.subarray(FRAME_HEADER_SIZE + size);

      const ptsUs = Number(ptsFlags & ~FLAG_CONFIG & ~FLAG_KEY & ~FLAG_SESSION) / 1000;
      if (ptsFlags & FLAG_CONFIG) {
        this.config = Buffer.from(payload);
        this.#frame(2, ptsUs, this.config);   // 2 = 配置帧，前端先喂给解码器
        continue;
      }
      const isKey = (ptsFlags & FLAG_KEY) !== 0n;
      this.#frame(isKey ? 1 : 0, ptsUs, payload);
    }
  }

  // 新观看端接入时让服务端重置编码（TYPE_RESET_VIDEO=17）：重发 codec 配置 + 关键帧。
  // 否则新客户端只能收到引用缺失的 delta 帧，解码器永远出不了画面
  resetVideo() {
    if (!this.control || this.control.destroyed) return false;
    try { this.control.write(Buffer.from([17])); return true; } catch (_) { return false; }
  }

  // 触控：32 字节 INJECT_TOUCH_EVENT，坐标必须是视频帧空间
  touch(action, x, y) {
    if (!this.control || this.control.destroyed || !this.width) return false;
    const px = Math.max(0, Math.min(this.width - 1, Math.round(x * this.width)));
    const py = Math.max(0, Math.min(this.height - 1, Math.round(y * this.height)));
    const buf = Buffer.alloc(32);
    buf.writeUInt8(2, 0);                       // INJECT_TOUCH_EVENT
    buf.writeUInt8(action, 1);                  // 0=DOWN 1=UP 2=MOVE
    buf.writeBigInt64BE(BigInt('-2'), 2);       // pointerId: GENERIC_FINGER
    buf.writeInt32BE(px, 10);
    buf.writeInt32BE(py, 14);
    buf.writeInt16BE(this.width, 18);
    buf.writeInt16BE(this.height, 20);
    buf.writeUInt16BE(action === 1 ? 0 : 0xFFFF, 22); // pressure
    // 布局（v4.1 ControlMessageReader.parseInjectTouchEvent）：
    // 0 type | 1 action | 2 pointerId(8) | 10 x(4) | 14 y(4) | 18 w(2) | 20 h(2) | 22 pressure(2) | 24 actionButton(4) | 28 buttons(4)
    buf.writeInt32BE(0, 24);                    // actionButton
    buf.writeInt32BE(0, 28);                    // buttons
    this.control.write(buf);
    return true;
  }

  async key(name) {
    const map = { back: 'KEYCODE_BACK', home: 'KEYCODE_HOME', power: 'KEYCODE_POWER' };
    if (!map[name]) throw new Error('未知按键: ' + name);
    await adbShell(this.serial, 'input keyevent ' + map[name]);
  }

  async text(str) {
    // adb input text 只吃 ASCII：空格转 %s，其余特殊字符按 shell 规则转义
    const safe = String(str).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/ /g, '%s');
    await adbShell(this.serial, `input text "${safe}"`);
  }

  #teardown(reason) {
    if (this.closed) return;
    this.closed = true;
    this.#json({ type: 'closed', reason });
    try { this.video && this.video.destroy(); } catch (_) {}
    try { this.control && this.control.destroy(); } catch (_) {}
    try { this.proc && this.proc.kill(); } catch (_) {}
    execFileAsync(ADB, ['-s', this.serial, 'forward', '--remove', `tcp:${this.port}`]).catch(() => {});
    adbShell(this.serial, 'pkill -f com.genymobile.scrcpy').catch(() => {});
    if (sessions.get(this.serial) === this) sessions.delete(this.serial);
    log(`[mirror] 结束 ${this.serial}: ${reason}`);
  }

  stop(reason) { this.#teardown(reason || '主动停止'); }
}

// ── 鸿蒙 NEXT 会话：hdc 抓图 + uitest 触控（单设备单会话） ─────────────
class HarmonySession {
  constructor(serial) {
    this.serial = serial;
    this.kind = 'hdc';
    this.codec = 'jpeg';
    this.clients = new Set();
    this.width = 0;            // 画面尺寸（= 截图尺寸，随旋转/折叠变化）
    this.height = 0;
    this.touchW = 0;           // 触控坐标空间（uitest getDisplaySize，拿不到时用画面尺寸）
    this.touchH = 0;
    this.driver = null;        // uitest daemon：只负责点按/打字，画面不靠它
    this.started = false;
    this.closed = false;
    this.polling = false;
    this.touchChain = Promise.resolve();
    this.pending = 0;          // 还没回来的触控 RPC 数（拖动据此丢帧）
    this.lastTouch = -1;       // 最后入队的动作（0 down / 1 up / 2 move）
    this.downPos = null;
    this.touchHoldUntil = 0;   // 触摸瞬间暂停抓图的截止时间（把手机 CPU 让给输入）
  }

  #json(obj) {
    const s = JSON.stringify(obj);
    for (const c of this.clients) if (c.readyState === 1) c.send(s);
  }

  #frame(flag, ptsUs, payload) {
    const head = Buffer.alloc(9);
    head.writeUInt8(flag, 0);
    head.writeDoubleBE(ptsUs, 1);
    // 截图只有「最新一帧」有意义：观看端积压就直接丢，不排队等
    for (const c of this.clients) {
      if (c.readyState !== 1 || c.bufferedAmount > 4 * 1024 * 1024) continue;
      c.send(Buffer.concat([head, payload]));
    }
  }

  async #hdcShell(cmd) {
    const { stdout } = await execFileAsync(HDC, ['-t', this.serial, 'shell', cmd], {
      timeout: 20000, windowsHide: true, maxBuffer: 4 * 1024 * 1024,
    });
    return stdout;
  }

  // 抓图循环：snapshot_display 落盘 → hdc file recv 拉回。单路太慢（~1.8fps），开多路并发
  async #pollLoop(slot) {
    const remote = `/data/local/tmp/phonemirror_${slot}.jpeg`;
    const local = path.join(os.tmpdir(), `phonemirror_${this.serial}_${slot}.jpeg`);
    while (this.polling && !this.closed) {
      // 没人看就不抓：截图很吃手机侧 CPU，空闲时不能空转
      if (!this.clients.size) { await sleep(400); continue; }
      // 刚按下/抬起时先让路：截图线程满载时输入事件会排队，点按会有明显延迟。
      // 窗口只开到百毫秒级——让路越久，点击后的新画面来得越晚，而那个延迟用户更敏感
      if (Date.now() < this.touchHoldUntil) { await sleep(40); continue; }
      const t0 = Date.now();
      try {
        await execFileAsync(HDC, ['-t', this.serial, 'shell', `snapshot_display -f ${remote}`], { timeout: 15000, windowsHide: true });
        await execFileAsync(HDC, ['-t', this.serial, 'file', 'recv', remote, local], { timeout: 15000, windowsHide: true, maxBuffer: 8 * 1024 * 1024 });
        this.#onJpeg(fs.readFileSync(local));
      } catch (e) {
        log('[harmony] 抓图失败: ' + e.message);
        await sleep(800);
      }
      const cost = Date.now() - t0;
      if (cost < 30) await sleep(30 - cost);   // 不让手机侧截图线程一直满负荷
    }
    try { fs.unlinkSync(local); } catch (_) {}
  }

  #onJpeg(buf) {
    if (this.closed || !buf || buf.length < 1000) return;
    if (buf[0] !== 0xff || buf[1] !== 0xd8) return;   // 不是 JPEG（抓图失败时报错文本）
    const dim = jpegSize(buf);
    if (dim) {
      const first = !this.width;
      if (first || Math.abs(dim.width - this.width) > 2 || Math.abs(dim.height - this.height) > 2) {
        this.width = dim.width;
        this.height = dim.height;
        log(`[harmony] ${this.serial} 画面 ${this.width}x${this.height}`);
        this.#json(first
          ? { type: 'start', codec: 'jpeg', width: this.width, height: this.height }
          : { type: 'resize', width: this.width, height: this.height });
      }
    }
    this.#frame(FLAG_JPEG, Date.now() * 1000, buf);
  }

  async start() {
    // 点按/打字靠 uitest daemon；起不来不影响看画面，只是点不动
    try {
      const { Hdc, UiDriver } = loadHdckit();
      const driver = new UiDriver(Hdc.createClient({ bin: HDC }).getTarget(this.serial));
      // E000004：hdc 通道刚恢复/刚插线时首连会失败，等 2s 重试即可
      for (let i = 0; ; i++) {
        try { await driver.start(); break; } catch (e) {
          if (i >= 2 || !/E000004/.test(String(e.message))) throw e;
          log(`[harmony] ${this.serial} uitest 通道未就绪，2s 后重试 ${i + 1}/3`);
          await sleep(2000);
        }
      }
      this.driver = driver;
      const size = await driver.getDisplaySize().catch(() => null);
      if (size && Number(size.width) > 0) {
        this.touchW = Number(size.width);
        this.touchH = Number(size.height);
      }
      log(`[harmony] ${this.serial} uitest 就绪，触控坐标 ${this.touchW || '?'}x${this.touchH || '?'}`);
    } catch (e) {
      log(`[harmony] ${this.serial} uitest 不可用（只影响点按/打字）: ${e.message}`);
    }

    // 画面：系统截图轮询。uitest 的持续截图流在这类设备上只有 0.1~0.5fps，不采用
    this.polling = true;
    for (let slot = 0; slot < HARMONY_POLL_CONCURRENCY; slot++) this.#pollLoop(slot);
    log(`[harmony] ${this.serial} 截图轮询已启动 (${HARMONY_POLL_CONCURRENCY} 路)`);
  }

  // 触控：uitest 的 Gestures 与 getDisplaySize 同一坐标空间，动作必须串行发
  touch(action, x, y) {
    if (this.closed) return false;
    const w = this.touchW || this.width, h = this.touchH || this.height;
    if (!w || !h) return false;
    const px = Math.max(0, Math.min(w - 1, Math.round(x * w)));
    const py = Math.max(0, Math.min(h - 1, Math.round(y * h)));
    // 按下让路 60ms：只保证 down 不被这一刻刚起的抓图挡住；
    // 再长就会把「点击后的新画面」推后，而画面晚到比触摸慢 10ms 更容易被看出来
    if (action === 0) this.touchHoldUntil = Date.now() + 60;
    const d = this.driver;
    if (!d) {
      // 没有 uitest 时退回 uinput：抬手时一次 -d/-u（点按可用，拖动退化成直线滑动）
      if (action === 0) this.downPos = [px, py];
      else if (action === 1) {
        const [x0, y0] = this.downPos || [px, py];
        this.downPos = null;
        execFileAsync(HDC, ['-t', this.serial, 'shell', 'uinput', '-T', '-d', String(x0), String(y0), '-u', String(px), String(py)],
          { timeout: 15000, windowsHide: true }).catch(() => {});
      }
      return true;
    }
    // down/up 一律按序执行，绝不丢：丢掉 up 会让手机停在上一次「按下」状态，一次点击变成一次长按
    // move 在积压时丢——拖动时最新位置才有意义，排队的旧位置只会越拖越滞；
    // 但按下后的第一个 move 不能丢，否则拖动起点会跳一下
    if (action === 2 && this.pending > 0 && this.lastTouch === 2) return false;
    this.pending++;
    this.lastTouch = action;
    const run = action === 0 ? () => d.touchDown(px, py)
      : action === 1 ? () => d.touchUp(px, py) : () => d.touchMove(px, py);
    this.touchChain = this.touchChain.then(run)
      .catch((e) => log('[harmony] 触控失败: ' + e.message))
      .then(() => { this.pending--; });
    return true;
  }

  async key(name) {
    // 鸿蒙系统键码（@ohos.multimodalInput.keyCode）与 Android 不同
    const map = { back: 2, home: 1, power: 18 };
    if (!map[name]) throw new Error('未知按键: ' + name);
    await this.#hdcShell(`uinput -K -d ${map[name]} -u ${map[name]}`);
  }

  async text(str) {
    if (!this.driver) throw new Error('文字输入不可用（uitest 未就绪）');
    // uitest 的 inputText 直接吃 UTF-8，中文不用装输入法
    await this.driver.inputText(String(str));
  }

  stop(reason) {
    if (this.closed) return;
    this.closed = true;
    this.polling = false;
    this.#json({ type: 'closed', reason: reason || '主动停止' });
    const d = this.driver;
    this.driver = null;
    if (d) d.stop().catch(() => {});
    execFileAsync(HDC, ['-t', this.serial, 'shell', 'rm -f /data/local/tmp/phonemirror_*.jpeg'],
      { timeout: 10000, windowsHide: true }).catch(() => {});
    if (sessions.get(this.serial) === this) sessions.delete(this.serial);
    log(`[harmony] 结束 ${this.serial}: ${reason || '主动停止'}`);
  }
}

const sessions = new Map();

async function ensureSession(serial, kind) {
  let s = sessions.get(serial);
  if (s && !s.closed) {
    if (!s.started) await s.start();
    return s;
  }
  // 纯血鸿蒙没有 adb，只能走 hdc + uitest 截图流；其它设备照旧用 scrcpy
  s = kind === 'hdc' ? new HarmonySession(serial) : new ScrcpySession(serial);
  s.started = true;
  sessions.set(serial, s);
  try {
    await s.start();
  } catch (e) {
    sessions.delete(serial);
    throw e;
  }
  return s;
}

// ── HTTP ────────────────────────────────────────────────────────────────
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const send = (code, body, type = 'application/json; charset=utf-8') => {
    res.writeHead(code, { 'Content-Type': type, 'Cache-Control': 'no-store' });
    res.end(body);
  };

  try {
    if (url.pathname === '/api/devices') {
      if (url.searchParams.get('force')) hidden.clear();   // 刷新 = 重新扫描全部，被手动断开的也放回来
      return send(200, JSON.stringify({ ok: true, devices: await listDevices() }));
    }
    if (url.pathname === '/api/connect' && req.method === 'POST') {
      const body = await readBody(req);
      const addr = String(body.addr || '').trim();
      if (!/^[\w.\-]+:\d{2,5}$/.test(addr)) return send(400, JSON.stringify({ ok: false, message: '格式应为 IP:端口，例如 192.168.1.8:5555' }));
      // 安卓走 adb connect、鸿蒙走 hdc tconn：两条链路都试，谁先通就用谁（端口就是手机无线调试页上显示的那个）
      const run = async (bin, args) => {
        try {
          const o = await execFileAsync(bin, args, { timeout: 12000, windowsHide: true });
          return ((o.stdout || '') + (o.stderr || '')).trim();
        } catch (e) { return ((e.stdout || '') + (e.stderr || '')).trim() || e.message; }
      };
      const adbOut = await run(ADB, ['connect', addr]);
      if (/connected to|already connected/i.test(adbOut)) {
        hidden.delete(addr);
        return send(200, JSON.stringify({ ok: true, message: adbOut, channel: 'adb' }));
      }
      const hdcOut = await run(HDC, ['tconn', addr]);
      if (/ok/i.test(hdcOut) && !/fail/i.test(hdcOut)) {
        hidden.delete(addr);
        return send(200, JSON.stringify({ ok: true, message: hdcOut, channel: 'hdc' }));
      }
      return send(200, JSON.stringify({ ok: false, message: [adbOut, hdcOut].filter(Boolean).join(' ｜ ') }));
    }
    // 手动断开：停会话 + 从列表拿掉；无线设备额外断开传输层，USB 设备拔不了就只隐藏
    if (url.pathname === '/api/disconnect' && req.method === 'POST') {
      const body = await readBody(req);
      const id = String(body.device || '').trim();
      const kind = String(body.kind || '');
      if (!id) return send(400, JSON.stringify({ ok: false, message: '缺少设备 id' }));
      const s = sessions.get(id);
      if (s) { try { s.stop('手动断开'); } catch (_) {} }
      hidden.add(id);
      if (id.includes(':')) {
        const cmd = kind === 'hdc' ? [HDC, ['tconn', id, 'remove']] : [ADB, ['disconnect', id]];
        execFileAsync(cmd[0], cmd[1], { timeout: 8000, windowsHide: true }).catch(() => {});
      }
      log(`[mirror] 手动断开 ${id}`);
      return send(200, JSON.stringify({ ok: true }));
    }
    // 静态资源
    const rel = url.pathname === '/' ? '/index.html' : url.pathname;
    const file = path.join(__dirname, 'public', path.normalize(rel).replace(/^([/\\])+/, ''));
    if (file.startsWith(path.join(__dirname, 'public')) && fs.existsSync(file) && fs.statSync(file).isFile()) {
      return send(200, fs.readFileSync(file), MIME[path.extname(file)] || 'application/octet-stream');
    }
    return send(404, 'not found', 'text/plain; charset=utf-8');
  } catch (e) {
    return send(500, JSON.stringify({ ok: false, error: e.message }));
  }
});

function readBody(req) {
  return new Promise((resolve) => {
    let d = '';
    req.on('data', (c) => (d += c));
    req.on('end', () => { try { resolve(JSON.parse(d || '{}')); } catch { resolve({}); } });
  });
}

// ── WebSocket：/ws/stream（画面） /ws/control（操作） ───────────────────
const wssStream = new WebSocketServer({ noServer: true, perMessageDeflate: false });
const wssControl = new WebSocketServer({ noServer: true, maxPayload: 64 * 1024 });

server.on('upgrade', (req, socket, head) => {
  const { pathname, searchParams } = new URL(req.url, 'http://x');
  if (pathname === '/ws/stream') {
    wssStream.handleUpgrade(req, socket, head, (ws) => wssStream.emit('connection', ws, req, searchParams));
  } else if (pathname === '/ws/control') {
    wssControl.handleUpgrade(req, socket, head, (ws) => wssControl.emit('connection', ws, req));
  } else {
    socket.destroy();
  }
});

wssStream.on('connection', async (ws, req, q) => {
  const serial = q.get('device') || '';
  const kind = q.get('kind') || 'adb';
  let session;
  try {
    session = await ensureSession(serial, kind);
  } catch (e) {
    ws.send(JSON.stringify({ type: 'error', message: e.message }));
    ws.close();
    return;
  }
  session.clients.add(ws);
  if (session.width) ws.send(JSON.stringify({ type: 'start', codec: session.codec || 'h264', width: session.width, height: session.height }));
  // 编码器已启动过就说明 config/关键帧已经错过，必须请求重置，新观看端才能从头解码
  // （鸿蒙是每帧独立的 JPEG，没有这个问题，HarmonySession 没有 resetVideo）
  if (session.config && typeof session.resetVideo === 'function') {
    log(`[mirror] 新观看端接入 ${serial}，请求重置编码`);
    session.resetVideo();
  }
  ws.on('close', () => session.clients.delete(ws));
});

wssControl.on('connection', (ws) => {
  // 控制连接按消息里的 device 路由（页签切换设备时不需要重连）
  ws.on('message', async (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch { return; }
    const session = sessions.get(msg.device);
    if (!session || session.closed) {
      ws.send(JSON.stringify({ type: 'ack', ok: false, error: '设备未连接' }));
      return;
    }
    try {
      if (msg.type === 'touch') {
        session.touch(msg.action === 'down' ? 0 : msg.action === 'up' ? 1 : 2, msg.x, msg.y);
      } else if (msg.type === 'key') {
        await session.key(msg.key);
      } else if (msg.type === 'text') {
        if (/[^\x20-\x7E\u4e00-\u9fa5]/.test(msg.text || '')) { /* 交给 adb 判定 */ }
        await session.text(msg.text || '');
      }
      ws.send(JSON.stringify({ type: 'ack', ok: true, echo: msg.type }));
    } catch (e) {
      ws.send(JSON.stringify({ type: 'ack', ok: false, error: e.message }));
    }
  });
});

server.listen(PORT, HOST, () => {
  log(`[phone-mirror] http://${HOST}:${PORT}  (adb: ${ADB})  日志: ${LOG_FILE}`);
});

// 进程退出时清干净设备端 scrcpy，不留孤儿进程
const cleanupAll = () => { for (const s of sessions.values()) s.stop('服务退出'); };
process.on('SIGINT', () => { cleanupAll(); process.exit(0); });
process.on('SIGTERM', () => { cleanupAll(); process.exit(0); });
process.on('exit', cleanupAll);
