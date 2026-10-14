# 第三方组件与许可证

本仓运行时会用到下列第三方组件。**注意：请自行核对版本对应的许可证原文。**

| 组件 | 位置 | 许可证（需自行确认版本） | 说明 |
| --- | --- | --- | --- |
| comtypes | `deps/comtypes/`（内置） | MIT | Windows COM 绑定，音频设备枚举用 |
| soundcard | `deps/soundcard/`（内置） | BSD-3-Clause | 回环录音（WASAPI loopback） |
| numpy | pip | BSD-3-Clause | 数值计算 |
| scipy | pip | BSD-3-Clause | 滤波器/频谱 |
| soundfile | pip | BSD-3-Clause | 音频读写 |
| pillow | pip | MIT-CMU | 截图/图像基础操作 |
| opencv-python | pip | Apache-2.0（OpenCV 本体） | 图像基础操作 |
| **PyQt5** | pip | **GPL v3 / 商业双许可（Riverbank）** | **界面库——这是本项目许可证上的硬约束，务必阅读 `LICENSE-草案.md` 第 2 节** |

## 关于 PyQt5 的重要提醒

PyQt5 采用 **GPL v3 与商业许可双授权**。若你分发使用 PyQt5 的二进制程序，
GPL v3 要求你向接收者提供完整对应源码，并**不得限制其再分发（包括修改版）**。
这与"禁止再分发修改版"的自定义许可相冲突。

零成本的两条出路：
1. 项目整体按 **GPL-3.0** 发布（放弃"禁止再分发修改版"）；
2. 界面迁移到 **PySide6（LGPL v3）**，即可配合自定义许可分发。

## 游戏数据

`assets/`（物品图标、音效、物品数据）来自游戏本体，**版权属于游戏权利人**，
本仓严格来说不含这些数据的再分发授权。发布前请自行评估风险。