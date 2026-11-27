'use strict';
/**
 * 极简投屏前端：只做「解码显示 + 鼠标点击/滑动/长按 + 三个硬件键 + 文字输入」
 * 数据格式（server.js 定义）：
 *   JSON  {type:'start'|'resize'|'closed'|'error', width, height, ...}
 *   二进制 [flag 1B][ptsUs 8B double][数据]
 *        flag 2=配置帧(SPS/PPS) 1=关键帧 0=普通帧 3=JPEG（鸿蒙 uitest 截图）
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const devSel = $('dev'), statusEl = $('status'), dot = $('dot');
  const canvas = $('screen'), stage = $('stage'), tip = $('tip');
  const ctx = canvas.getContext('2d', { alpha: false });

  // 画布按「显示尺寸 × DPR」建，自己拿视频帧做高质量下采样：
  // 直接把 1080×2358 的帧交给浏览器缩到 400 宽会发虚、出摩尔纹
  let videoW = 0, videoH = 0;
  let viewZoom = 1;            // 画面放大倍数（看细节用）；1 = 自适应窗口
  function setZoom(z) {
    viewZoom = Math.min(3, Math.max(1, Math.round(z * 4) / 4));
    stage.classList.toggle('zoomed', viewZoom > 1);
    const btn = $('zoomBtn');
    if (btn) btn.textContent = viewZoom > 1 ? `🔍 ${viewZoom}x` : '🔍 放大';
    if (videoW) fitCanvas(videoW, videoH);
  }
  function fitCanvas(vw, vh) {
    if (!vw || !vh) return;
    videoW = vw; videoH = vh;
    const availW = stage.clientWidth - 24, availH = stage.clientHeight - 24;  // 减 stage padding
    if (availW <= 0 || availH <= 0) return;   // 窗口还没布局好，下一帧再算
    const scale = Math.min(availW / vw, availH / vh) * viewZoom;
    const cssW = Math.max(1, Math.floor(vw * scale));
    const cssH = Math.max(1, Math.floor(vh * scale));
    canvas.style.width = cssW + 'px';
    canvas.style.height = cssH + 'px';
    const dpr = window.devicePixelRatio || 1;
    let bw = Math.max(1, Math.round(cssW * dpr)), bh = Math.max(1, Math.round(cssH * dpr));
    // 背板像素密度封顶：放大后只要屏幕能显示的像素，再多只是拖慢绘制
    const CAP = 2600;
    if (bw > CAP) { bh = Math.round(bh * CAP / bw); bw = CAP; }
    if (canvas.width !== bw || canvas.height !== bh) {
      canvas.width = bw;
      canvas.height = bh;
      ctx.imageSmoothingEnabled = true;     // 改 canvas 尺寸会重置上下文状态，要重设
      ctx.imageSmoothingQuality = 'high';
    }
  }

  let streamWs = null, ctrlWs = null;
  let deviceId = '';
  let decoder = null;
  let codecStr = 'avc1.42E01E'; // 收到配置帧前先用 Baseline，真机 SPS 到了再覆盖
  let awaitConfig = true;     // 重建后要等新的配置帧
  let lastTs = 0;
  let pendingConfig = null;   // 已收到的 SPS/PPS，等关键帧一起喂

  const setStatus = (text, kind) => {
    statusEl.textContent = text;
    dot.className = 'dot' + (kind === 'on' ? ' on' : kind === 'err' ? ' err' : '');
  };
  const showTip = (html) => { tip.innerHTML = html; tip.style.display = ''; canvas.classList.remove('show'); };
  const hideTip = () => { tip.style.display = 'none'; canvas.classList.add('show'); };

  // WebCodecs 只在「安全上下文」开放：工具箱若用局域网 IP 打开，内嵌的本页也会被判为不安全
  //（即使 iframe 地址是 127.0.0.1），此时 VideoDecoder 直接不存在，并不是浏览器不支持
  let codecWarned = false;
  function explainNoCodec() {
    if (codecWarned || window.VideoDecoder) return;
    codecWarned = true;
    if (window.isSecureContext) {
      setStatus('当前浏览器不支持 WebCodecs', 'err');
      showTip('请在 <b>Chrome / Edge 最新版</b>中打开本页（Chrome 94+ 才提供 WebCodecs）。');
      return;
    }
    setStatus('WebCodecs 被浏览器禁用（非安全上下文）', 'err');
    showTip('浏览器只为<b>安全上下文</b>开放 WebCodecs 解码，<br>' +
      '用<b>局域网 IP</b> 打开工具箱时，这个内嵌页也会被当成不安全页面。<br><br>' +
      '① 改用 <b>http://127.0.0.1:5050</b> 打开工具箱；<br>' +
      '② 或点下面按钮，在独立窗口里投屏。' +
      '<br><button class="btn btn-primary" style="margin-top:10px" ' +
      'onclick="window.open(location.origin + \'/\', \'_blank\')">在独立窗口打开</button>');
  }

  // ── 设备列表 ────────────────────────────────────────────────
  const DEFAULT_TIP = tip.innerHTML;   // IP 连接引导会占掉 tip，取消时要还原
  async function loadDevices(force) {
    devSel.innerHTML = '<option value="">— 选择设备 —</option>';
    try {
      // force = 用户主动点刷新：把手动断开过的设备也重新列出来
      const r = await fetch('/api/devices' + (force ? '?force=1' : ''));
      const d = await r.json();
      (d.devices || []).forEach((x) => {
        const o = document.createElement('option');
        o.value = x.id;
        o.textContent = `${x.model}（${x.platform}）`;
        o.dataset.kind = x.kind;
        devSel.appendChild(o);
      });
      if (!(d.devices || []).length) setStatus('未发现设备：请插线并开启调试');
      else setStatus('发现 ' + d.devices.length + ' 台设备', 'on');
    } catch (e) { setStatus('扫描失败：' + e.message, 'err'); }
  }

  // ── 控制通道 ────────────────────────────────────────────────
  function ensureCtrl() {
    if (ctrlWs && ctrlWs.readyState <= 1) return ctrlWs;
    ctrlWs = new WebSocket(`ws://${location.host}/ws/control`);
    ctrlWs.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (!m.ok && m.error) setStatus(m.error, 'err');
      } catch (_) {}
    };
    ctrlWs.onclose = () => { ctrlWs = null; };
    return ctrlWs;
  }
  const ctrl = (obj) => {
    const ws = ensureCtrl();
    const send = () => ws.send(JSON.stringify({ ...obj, device: deviceId }));
    if (ws.readyState === 1) send(); else ws.addEventListener('open', send, { once: true });
  };

  // ── 解码器 ──────────────────────────────────────────────────
  function resetDecoder() {
    if (decoder) { try { decoder.close(); } catch (_) {} }
    decoder = null;
    awaitConfig = true;
  }

  // 从 Annex-B 配置帧的 SPS 推断 codec 字符串：SPS = 00 00 01 67 [profile][constraint][level]
  // 手机可能是 High/Level 5.1（avc1.640033），写死 Baseline 会让 WebCodecs 直接报 Decoding error
  function codecFromConfig(data) {
    for (let i = 0; i + 4 < data.length; i++) {
      if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1) {
        const hdr = data[i + 3];
        const off = i + 4;
        if (hdr === 0x67 && off + 2 < data.length) {
          const hex = (n) => n.toString(16).padStart(2, '0');
          return 'avc1.' + hex(data[off]) + hex(data[off + 1]) + hex(data[off + 2]);
        }
      }
    }
    return null;
  }

  function ensureDecoder() {
    if (decoder) return decoder;
    decoder = new VideoDecoder({
      output: (frame) => {
        const vw = frame.displayWidth, vh = frame.displayHeight;
        if (vw !== videoW || vh !== videoH) fitCanvas(vw, vh);
        if (canvas.width && canvas.height) ctx.drawImage(frame, 0, 0, canvas.width, canvas.height);
        frame.close();
      },
      error: (e) => { setStatus('解码失败：' + e.message, 'err'); resetDecoder(); },
    });
    // Annex-B 模式不传 description；必须显式声明 avc.format，否则 Chrome 按 avcC 解，报 Decoding error
    decoder.configure({ codec: codecStr, avc: { format: 'annexb' }, optimizeForLatency: true });
    return decoder;
  }

  function feed(flag, ptsUs, data) {
    const d = ensureDecoder();
    const ts = Math.max(ptsUs, lastTs + 1);
    lastTs = ts;
    try {
      d.decode(new EncodedVideoChunk({
        type: flag === 0 ? 'delta' : 'key',   // 2/1 都按 key 喂，WebCodecs 接受配置帧为 key
        timestamp: ts,
        data,
      }));
    } catch (e) { /* 个别帧被丢弃不中断 */ }
    hideTip();
  }

  // 鸿蒙 NEXT：uitest 推的是 JPEG 截图（flag=3），自包含、每帧独立，不需要解码器
  let jpegBusy = false, jpegQueued = null;
  function onJpeg(data) {
    if (jpegBusy) { jpegQueued = data; return; }   // 解码跟不上就只留最新一帧
    jpegBusy = true;
    createImageBitmap(new Blob([data], { type: 'image/jpeg' }))
      .then((bmp) => {
        if (bmp.width !== videoW || bmp.height !== videoH) fitCanvas(bmp.width, bmp.height);
        if (canvas.width && canvas.height) ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
        bmp.close();
        hideTip();
      })
      .catch(() => {})
      .finally(() => {
        jpegBusy = false;
        if (jpegQueued) { const d = jpegQueued; jpegQueued = null; onJpeg(d); }
      });
  }

  function onFrame(buf) {
    if (buf.length < 10) return;
    const flag = buf[0];
    // buf 是 Uint8Array：DataView 必须基于其 buffer + byteOffset，不能直接传视图
    const ptsUs = new DataView(buf.buffer, buf.byteOffset + 1, 8).getFloat64(0);
    const data = buf.subarray(9);

    if (flag === 3) { onJpeg(data); return; }   // 3 = JPEG（鸿蒙）
    if (!window.VideoDecoder) { explainNoCodec(); return; }

    // 配置帧（SPS/PPS）不能单独喂：annexb 下 WebCodecs 要求首个 chunk 是真关键帧，
    // 单独喂会报 "A key frame is required after configure()"。先存起来，和紧跟的关键帧拼成一个 chunk
    if (flag === 2) {
      const c = codecFromConfig(data);
      if (c && c !== codecStr) codecStr = c;   // 先换 profile 再重建，configure 才吃上
      pendingConfig = data;
      return;
    }
    if (flag === 1) {
      let chunk = data;
      if (pendingConfig) {
        const merged = new Uint8Array(pendingConfig.length + data.length);
        merged.set(pendingConfig, 0);
        merged.set(data, pendingConfig.length);
        chunk = merged;
        pendingConfig = null;
        resetDecoder();          // 新配置到了才重建，避免把已缓存的配置丢掉
        awaitConfig = false;
      }
      if (awaitConfig) return;   // 没有配置帧的裸关键帧也先丢，等配置就位
      feed(1, ptsUs, chunk);
      return;
    }
    if (awaitConfig || !decoder) return;
    feed(0, ptsUs, data);
  }

  // ── 连接设备 ────────────────────────────────────────────────
  function currentKind() {
    const o = devSel.selectedOptions[0];
    return (o && o.dataset.kind) || 'adb';
  }

  function connect(id) {
    disconnect();
    if (!id) { showTip('选一台设备开始投屏'); return; }
    deviceId = id;
    lastTs = 0;
    setStatus('连接中…');
    streamWs = new WebSocket(`ws://${location.host}/ws/stream?device=${encodeURIComponent(id)}&kind=${encodeURIComponent(currentKind())}`);
    streamWs.binaryType = 'arraybuffer';
    streamWs.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
        if (m.type === 'start') { resetDecoder(); setStatus(`已连接 ${m.width}×${m.height}` + (m.codec === 'jpeg' ? ' · 截图流' : ''), 'on'); }
        else if (m.type === 'resize') { resetDecoder(); setStatus(`画面重建 ${m.width}×${m.height}`, 'on'); }
        else if (m.type === 'closed') { setStatus('连接已断开：' + (m.reason || ''), 'err'); showTip('画面已断开，重新选设备即可'); }
        else if (m.type === 'error') { setStatus(m.message, 'err'); showTip('连接失败：' + m.message); }
      } else {
        onFrame(new Uint8Array(ev.data));
      }
    };
    streamWs.onclose = () => { if (deviceId) setStatus('连接已断开', 'err'); };
    streamWs.onerror = () => setStatus('连接异常', 'err');
    ensureCtrl();
  }

  function disconnect() {
    resetDecoder();
    if (streamWs) { try { streamWs.close(); } catch (_) {} streamWs = null; }
    deviceId = '';
  }

  // ── 鼠标 → 手机 ─────────────────────────────────────────────
  const rel = (e) => {
    const r = canvas.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    };
  };

  let pressing = false, lastMove = 0, lastPos = null;

  canvas.addEventListener('pointerdown', (e) => {
    if (!deviceId || e.button !== 0) return;
    e.preventDefault();
    canvas.setPointerCapture(e.pointerId);
    pressing = true;
    const p = rel(e);
    lastPos = p;
    ctrl({ type: 'touch', action: 'down', x: p.x, y: p.y });
  });

  canvas.addEventListener('pointermove', (e) => {
    if (!pressing || !deviceId) return;
    const now = performance.now();
    if (now - lastMove < 16) return;      // 拖拽限流 ~60Hz
    lastMove = now;
    const p = rel(e);
    lastPos = p;
    ctrl({ type: 'touch', action: 'move', x: p.x, y: p.y });
  });

  const release = (e) => {
    if (!pressing) return;
    pressing = false;
    const p = lastPos || rel(e);
    ctrl({ type: 'touch', action: 'up', x: p.x, y: p.y });
  };
  canvas.addEventListener('pointerup', release);
  canvas.addEventListener('pointercancel', release);

  // 滚轮：按手势做一次短滑动（手机端没有滚轮概念）
  let wheelBusy = false;
  canvas.addEventListener('wheel', (e) => {
    if (e.ctrlKey) {                      // Ctrl+滚轮 = 放大画面看细节（不发给手机）
      e.preventDefault();
      setZoom(viewZoom + (e.deltaY < 0 ? 0.25 : -0.25));
      return;
    }
    if (!deviceId) return;
    e.preventDefault();
    if (wheelBusy) return;
    wheelBusy = true;
    setTimeout(() => { wheelBusy = false; }, 180);
    const p = lastPos || rel(e);
    const dy = e.deltaY > 0 ? -0.28 : 0.28;   // 轮向下 = 内容向上滑
    const steps = 4;
    ctrl({ type: 'touch', action: 'down', x: p.x, y: p.y });
    for (let i = 1; i <= steps; i++) {
      setTimeout(() => {
        ctrl({ type: 'touch', action: 'move', x: p.x, y: Math.min(1, Math.max(0, p.y + dy * i / steps)) });
        if (i === steps) ctrl({ type: 'touch', action: 'up', x: p.x, y: Math.min(1, Math.max(0, p.y + dy)) });
      }, i * 16);
    }
  }, { passive: false });

  // ── 硬件键 / 文字 ───────────────────────────────────────────
  document.querySelectorAll('[data-key]').forEach((b) => {
    b.addEventListener('click', () => {
      if (!deviceId) return setStatus('先选择设备', 'err');
      ctrl({ type: 'key', key: b.dataset.key });
    });
  });

  function sendText() {
    const el = $('text');
    const v = el.value;
    if (!v || !deviceId) return;
    // adb 的 input text 只吃 ASCII；鸿蒙走 uitest 的 inputText，中文可以直接发
    if (currentKind() !== 'hdc' && /[^\x20-\x7E]/.test(v)) { setStatus('暂只支持英文/数字/符号（中文需装输入法 APK）', 'err'); return; }
    ctrl({ type: 'text', text: v });
    el.value = '';
  }
  $('send').addEventListener('click', sendText);
  $('text').addEventListener('keydown', (e) => { if (e.key === 'Enter') sendText(); });
  $('zoomBtn').addEventListener('click', () => setZoom(viewZoom > 1 ? 1 : 2));

  // ── 顶栏事件 ────────────────────────────────────────────────
  devSel.addEventListener('change', () => connect(devSel.value));
  $('refresh').addEventListener('click', async () => {
    const keep = devSel.value;
    await loadDevices(true);
    if (keep) devSel.value = keep;
  });
  // 手动断开：停画面 + 让后端停会话，并把设备从下拉里拿掉（USB 拔不了，只能隐藏）
  $('offBtn').addEventListener('click', async () => {
    const id = deviceId, kind = currentKind();
    if (!id) return setStatus('先选择设备', 'err');
    disconnect();
    showTip(DEFAULT_TIP);              // 画面停在最后一帧会让人以为还连着
    setStatus('正在断开 ' + id + ' …');
    try {
      const r = await fetch('/api/disconnect', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device: id, kind }),
      });
      const d = await r.json();
      if (!d.ok) { setStatus(d.message || '断开失败', 'err'); await loadDevices(); return; }
      devSel.value = '';
      await loadDevices();
      setStatus('已断开（点刷新可恢复显示）', 'on');
    } catch (e) { setStatus('断开失败：' + e.message, 'err'); }
  });
  $('ipBtn').addEventListener('click', () => {
    const el = $('ipAddr');
    const show = !el.classList.contains('show');
    el.classList.toggle('show', show);
    if (!show) { if (!deviceId) tip.innerHTML = DEFAULT_TIP; return; }
    el.focus();
    // 正在投屏时不抢画面，只在状态栏提示
    if (deviceId) return setStatus('填入 IP:端口 后回车即可连接新设备');
    showTip('<b>用手机无线调试连电脑</b><br>' +
      'Android：设置 → 开发者选项 → <b>无线调试</b> 打开，把手机上显示的 IP:端口 填到左边输入框后回车；<br>' +
      '首次连要配对：点「使用配对码配对设备」，在电脑命令行执行 <b>adb pair IP:端口</b>。<br>' +
      '鸿蒙 NEXT：设置 → 开发人员选项 → <b>无线调试（Hdc）</b> 打开，把显示的 IP:端口 填进去回车。');
  });
  $('ipAddr').addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    const addr = e.target.value.trim();
    if (!addr) return;
    setStatus('连接 ' + addr + ' …');
    try {
      const r = await fetch('/api/connect', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ addr }),
      });
      const d = await r.json();
      if (d.ok) { setStatus('已连接 ' + addr, 'on'); await loadDevices(); }
      else setStatus(d.message || '连接失败', 'err');
    } catch (err) { setStatus('连接失败：' + err.message, 'err'); }
  });

  window.addEventListener('beforeunload', disconnect);
  // 窗口拉伸/换屏（DPR 变）时重算画布，不重算会又回到模糊的二次缩放
  window.addEventListener('resize', () => { if (videoW) fitCanvas(videoW, videoH); });
  // 开局先自查解码能力：非安全上下文下不让用户白选设备
  loadDevices().then(explainNoCodec);
})();
