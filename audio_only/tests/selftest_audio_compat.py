# -*- coding: utf-8 -*-
"""采集兼容层自测：设备格式补丁 + 采集端重采样（不需要声卡/游戏）。

为什么单独测这两块：用户报「某些型号耳机完全没声音、管理员也不行」的根因就在
「端点格式」这一层 —— 内置 soundcard 上游代码对格式做了硬断言（非 32-bit float /
非 40 字节设备格式就直接 AssertionError）。补丁把断言换成了自建 float32 格式，
这里用**纯离线**的方式锁住：
  * 自建格式的 40 字节布局必须与 Windows 真实 IEEE-float WAVEFORMATEXTENSIBLE 一致；
  * 16/24/32-bit、1/2/8 声道的设备都必须能构造出合法结构；
  * 设备只给 48k 时，采集端必须能重采样回 44.1k，且音高不变。
"""
from __future__ import annotations

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "deps"))

import recorder  # noqa: E402
import soundcard.mediafoundation as mf  # noqa: E402

FAILS: list[str] = []


def check(cond, msg: str) -> None:
    if cond:
        print("  [OK ] " + msg)
    else:
        print("  [FAIL] " + msg)
        FAILS.append(msg)


def test_packed_format() -> None:
    """自建 float32 结构 == Windows 真实结构（逐字节）。"""
    fmt, keep = mf._pack_float_format(48000, 2, 3)
    raw = bytes(mf._ffi.buffer(fmt, 40))
    print("  自建格式:", mf._describe_format(raw))
    check(raw[0:2] == b"\xfe\xff", "wFormatTag = 0xFFFE (WAVE_FORMAT_EXTENSIBLE)")
    check(int.from_bytes(raw[2:4], "little") == 2, "nChannels = 2")
    check(int.from_bytes(raw[4:8], "little") == 48000, "nSamplesPerSec = 48000")
    check(int.from_bytes(raw[8:12], "little") == 48000 * 2 * 4, "nAvgBytesPerSec 正确")
    check(int.from_bytes(raw[12:14], "little") == 8, "nBlockAlign = 2ch*4B")
    check(int.from_bytes(raw[14:16], "little") == 32, "wBitsPerSample = 32")
    check(int.from_bytes(raw[16:18], "little") == 22, "cbSize = 22")
    check(int.from_bytes(raw[18:20], "little") == 32, "wValidBitsPerSample = 32")
    check(int.from_bytes(raw[20:24], "little") == 3, "dwChannelMask = 3")
    # 00000003-0000-0010-8000-00AA00389B71
    check(raw[24:40].hex() == "0300000000001000800000aa00389b71",
          "SubFormat = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT")
    check(len(raw) == 40, "结构长度 = 40 字节（WAVEFORMATEXTENSIBLE）")
    del keep


def test_any_device_shape() -> None:
    """16/24/32-bit、1/2/8 声道的设备都能构造（旧代码在这几档上断言会挂）。"""
    for channels in (1, 2, 6, 8):
        for rate in (16000, 44100, 48000, 96000):
            fmt, keep = mf._pack_float_format(rate, channels, (1 << channels) - 1)
            raw = bytes(mf._ffi.buffer(fmt, 40))
            ok = (int.from_bytes(raw[2:4], "little") == channels
                  and int.from_bytes(raw[4:8], "little") == rate
                  and int.from_bytes(raw[14:16], "little") == 32)
            if not ok:
                check(False, f"{channels}ch/{rate}Hz 结构错误")
                return
            del keep
    check(True, "1/2/6/8 声道 × 16k/44.1k/48k/96k 全部构造成功")


def test_describe() -> None:
    fmt, keep = mf._pack_float_format(44100, 2, 3)
    text = mf._describe_format(bytes(mf._ffi.buffer(fmt, 40)))
    check("tag=0xfffe" in text and "bits=32" in text and "rate=44100" in text,
          "格式描述串包含 tag/bits/rate")
    del keep


def test_resample() -> None:
    """48k → 44.1k：长度按比例缩短，1kHz 正弦仍是 1kHz。"""
    src_rate = 48000
    n = src_rate                     # 1 秒
    t = np.arange(n) / src_rate
    tone = (0.5 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
    out = recorder._resample_to_44k(tone, src_rate)
    check(abs(len(out) - recorder.SAMPLE_RATE) <= 2,
          f"48k 1 秒 → 44.1k 长度 {len(out)}（期望 {recorder.SAMPLE_RATE}）")
    spec = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    peak = float(np.fft.rfftfreq(len(out), 1.0 / recorder.SAMPLE_RATE)[int(np.argmax(spec))])
    check(abs(peak - 1000.0) < 3.0, f"重采样后主频 = {peak:.1f}Hz（期望 1000Hz）")
    same = recorder._resample_to_44k(tone, recorder.SAMPLE_RATE)
    check(len(same) == len(tone), "44.1k 输入原样返回（不重复重采样）")


def test_unknown_device() -> None:
    """指定不存在的设备必须给出带原因的错误（而不是 AssertionError）。"""
    try:
        recorder._open_loopback("绝对不存在的设备名-42", recorder.SAMPLE_RATE)
    except RuntimeError as exc:
        msg = str(exc)
        check("绝对不存在的设备名-42" in msg, "错误信息里带上了请求的设备名")
        check("AssertionError" not in msg, "不再是上游那种 AssertionError")
    except Exception as exc:                      # noqa: BLE001
        check(False, f"抛了非 RuntimeError：{type(exc).__name__}: {exc}")
    else:
        check(False, "不存在的设备居然打开成功了")


def main() -> int:
    print("== 采集兼容层：设备格式补丁 + 重采样 ==")
    test_packed_format()
    test_any_device_shape()
    test_describe()
    test_resample()
    test_unknown_device()
    print()
    if FAILS:
        print(f"== FAIL ==  {len(FAILS)} 项失败")
        return 1
    print("== PASS ==  采集兼容层自测通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
