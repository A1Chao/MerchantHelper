# 贡献指南（草案）

## 可以提 PR 的部分

- `ui.py`：界面布局、交互、可读性
- `candidates.py`：筛选规则、排序、展示字段
- `recognition.py` / `denoise.py` / `recorder.py`：音频识别与录音链路
- `tests/`：为上述模块补测试（**不要**为视觉核心补测试，本仓没有它）
- 文档、错别字、启动脚本

## 不要提 PR 的部分

- `vision.py`：本仓中它是**占位 stub**。真实视觉实现不在本仓，PR 无法被合入；
  若你想自行实现视觉，请按 `vision.py` 顶部的**接口契约**独立实现并放在你自己的 fork 里。
- 任何试图绕过/移除保护与署名信息的改动。

## 提交前请自测

```bat
D:\PY\python.exe main.py --no-vision          :: 界面+音频应能起来
D:\PY\python.exe main.py --list-devices        :: 音频设备枚举
D:\PY\python.exe tests\selftest_recognition.py :: 音频识别（33 模板自识别 100%）
D:\PY\python.exe tests\selftest_candidates.py  :: 候选筛选（116 项）
D:\PY\python.exe tests\selftest_recorder.py    :: 回环录音
D:\PY\python.exe tests\mprobe_session_lock.py  :: 触发会话锁（26 项）
D:\PY\python.exe tests\mprobe_fast_drag.py     :: 快拖触发回归（2 项）
D:\PY\python.exe tests\mprobe_hold_sample.py   :: 按住期间采样兜底（5 项）
```

> 说明：涉及**视觉几何**的测试（`selftest_integration.py`、`mprobe_wiring.py`、
> `mprobe3_wiring.py`、`mprobe_trig_diag.py`）**不在本仓**——它们需要 `vision.py` 的真实实现。

## Issue 模板建议分三类

1. **界面/音频问题** —— 欢迎，附日志与复现步骤；
2. **占格识别问题** —— 本仓不含实现，只能由官方发行版处理，请附官方版日志；
3. **数据问题** —— 说明是自带数据还是官方数据。