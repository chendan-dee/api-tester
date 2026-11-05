---
kind: frontend_style
name: 内联 CSS + CSS 变量主题的单页 Web 界面
category: frontend_style
scope:
    - '**'
source_files:
    - templates/index.html
---

本项目的 Web 前端位于 templates/index.html，采用单文件内联样式方案：所有 CSS 通过 <style> 标签直接嵌入 HTML，无任何外部 CSS/SCSS/Less 文件或第三方 UI 框架。整体风格为轻量级、自托管的桌面工具式界面。

1. 样式系统与方法论
- 使用 CSS 自定义属性（:root 变量）集中定义设计令牌：背景色、卡片色、主色、成功/危险/警告色、文本层级、圆角与阴影等，如 --primary: #4f6ef7、--text2: #64748b、--radius: 10px。
- 组件类命名遵循 BEM 风格前缀（.card、.btn、.step-*、.result-*、.mock-*、.yaml-ref-*），按功能域分组，无嵌套选择器，保持扁平可读。
- 布局以 Flexbox 为主，辅以少量 Grid（如 .mock-grid），未引入任何响应式断点库，仅用一条 @media (max-width: 900px) 处理移动端网格折叠。
- 交互状态通过 class 切换实现（.active、.open、.filtered-out），配合 transition 做淡入/展开动画。

2. 关键文件与位置
- 全部样式集中在 templates/index.html 的 <head><style> 块中（约 230 行），HTML 结构与 JS 逻辑同文件，属于典型的 Flask/Jinja 模板直出模式。
- 无独立 CSS 文件、无构建产物、无 Tailwind/Bootstrap/Ant Design 等依赖引用。

3. 架构约定与设计决策
- 零依赖：不引入任何前端框架或 CSS 库，降低部署复杂度，适合 Python 后端直出模板场景。
- 主题即变量：修改 :root 中的颜色变量即可全局换肤，但项目未提供暗色/多主题切换入口。
- 语义化类名 + 实用类混用：既有 .btn-primary、.badge-pass 等语义类，也有 .mt-8、.row、.empty 等通用实用类，兼顾可读性与开发效率。
- 结果/错误态视觉编码：通过固定色板区分通过（绿色）、失败（红色）、信息（蓝色），在 JSON 预览、断言标签、步骤方法徽章等处保持一致。

4. 开发者应遵循的规则
- 新增样式优先复用现有 CSS 变量和已有类名，避免硬编码颜色值。
- 组件类名沿用 模块前缀 + 语义描述 形式（如 .step-editor、.json-result、.filter-help-popup）。
- 复杂弹窗/侧边栏统一使用 .overlay + .dialog 结构，参考 .setup-modal-overlay、.var-edit-overlay 的实现。
- 如需新增主题，应在 :root 下扩展变量并维护一份对照表，而非在组件类中重复声明颜色。