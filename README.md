# MerchantHelper · 行商助手

> External-perception helper for the "field merchant" gameplay in *Arena Breakout: Infinite*
> 《暗区突围：无限》「战地行商」玩法的外部感知辅助工具
> Author / 作者：**烟火**

- **中文说明 → [README-zh.md](README-zh.md)**
- **English → [README-en.md](README-en.md)**
- **详细说明（原项目文档）→ [docs/详细说明.md](docs/详细说明.md)**
- **下载最新版 → [Releases](https://github.com/A1Chao/MerchantHelper/releases/latest)**（当前 v1.1.0）
- **Windows 弹「Windows 已保护你的电脑」→ [docs/SmartScreen与代码签名.md](docs/SmartScreen与代码签名.md)**（一次处理，之后只弹 UAC）

---

This repository is the **semi-open-source** edition: audio recognition, candidate filtering, the UI
and the integration layer are included, while the **vision core** (grid-footprint recognition) is
**not** — `vision.py` here is only a placeholder stub. For the full feature set, use the official
exe in Releases.

本仓库是**半开源版**：包含音频识别、候选筛选、界面与集成层；**不包含视觉（占格识别）核心**，
仓库里的 `vision.py` 只是占位 stub。**想要完整功能请用 Releases 里的官方 exe。**

The tool is **strictly read-only** — no memory access, no injection, no input synthesis, no network.
本工具**纯只读**：不读内存、不注入、不模拟键鼠、不联网。

> Study and exchange only; commercial use prohibited.
> 仅供学习交流，禁止商业用途。