# -*- coding: utf-8 -*-
"""稳定性回归：不使用真实声卡，不执行危险的原生上下文调用。"""
from __future__ import annotations
import ast
import contextlib
import ctypes
import io
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import main
import recorder
import shutdown as launcher


class Stop:
    def __init__(self): self.stopped = False
    def is_set(self): return self.stopped
    def set(self): self.stopped = True
    def wait(self, seconds): return self.stopped


class Rec:
    samplerate = 44100
    device_mix_format = client_format = "test"
    def __init__(self, good_blocks=0, stop_error=False):
        self.good_blocks = good_blocks
        self.stop_error = stop_error
        self.closed = False
        self.entered = 0
    def __enter__(self):
        if self.closed: raise AssertionError("reused released recorder")
        self.entered += 1
        return self
    def __exit__(self, *exc):
        self.closed = True
        if self.stop_error: raise RuntimeError("Stop DEVICE_INVALIDATED")
    def record(self, **kw):
        if self.good_blocks <= 0: raise RuntimeError("GetBuffer DEVICE_INVALIDATED")
        self.good_blocks -= 1
        return np.full(recorder.BLOCK, .02, dtype=np.float32)


@contextlib.contextmanager
def capture_environment(open_fn):
    fake_com = types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, {"soundcard": types.SimpleNamespace(), "comtypes": fake_com}))
        for name, value in {"_open_loopback": open_fn, "_enter_mmcss": lambda: None,
                            "_leave_mmcss": lambda s: None, "_raise_capture_thread_priority": lambda: False,
                            "_install_warning_hook": lambda: None}.items():
            stack.enter_context(patch.object(recorder, name, value))
        yield


def recorder_method(name, com):
    tree = ast.parse((ROOT.parent / "deps/soundcard/mediafoundation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_Recorder")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {"_com": com, "numpy": np}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "recorder_method", "exec"), ns)
    return ns[name]


class Stability(unittest.TestCase):
    def test_reopen_failure_never_reuses_closed_recorder(self):
        opened = []; failed = Rec(); healthy = Rec(1000)
        def op(*a, **k):
            opened.append(1)
            if len(opened) == 2: raise RuntimeError("temporary open failure")
            return (failed if len(opened) == 1 else healthy), types.SimpleNamespace(name="fake"), []
        stop = Stop()
        with capture_environment(op):
            gen = recorder.listen_windows(stop)
            win, bg = next(gen)
            self.assertEqual(len(win), int(recorder.SAMPLE_RATE * recorder.WINDOW_SECONDS))
            self.assertEqual(len(opened), 3)
            self.assertTrue(failed.closed)
            self.assertEqual(failed.entered, 1)
            stop.set(); gen.close()
        self.assertTrue(healthy.closed)

    def test_cleanup_error_preserves_device_loss_and_recovers(self):
        calls = []
        def op(*a, **k):
            rec = Rec(0, True) if not calls else Rec(1000)
            calls.append(rec)
            return rec, types.SimpleNamespace(name="fake"), []
        stop = Stop()
        with capture_environment(op):
            gen = recorder.listen_windows(stop); next(gen); stop.set(); gen.close()
        self.assertEqual(len(calls), 2)

    def test_exhaustion_is_visible_error(self):
        opened = []
        def op(*a, **k):
            opened.append(1)
            return Rec(), types.SimpleNamespace(name="fake"), []
        feed = main.AudioFeed(); feed.stop_event = Stop(); feed._windows = 100
        with capture_environment(op): feed._run()
        self.assertEqual(len(opened), recorder.RECOVERY_MAX_FAILURES + 1)
        self.assertIn("恢复失败", feed.last_error)
        self.assertIn("音频错误", main._detection_status_line(types.SimpleNamespace(audio=feed)))

    def test_healthy_capture_resets_failure_budget(self):
        opened = []
        def op(*a, **k):
            rec = Rec(0 if not opened else 65)
            opened.append(rec)
            return rec, types.SimpleNamespace(name="fake"), []
        stop = Stop()
        with capture_environment(op), patch.object(recorder, "RECOVERY_RESET_SECONDS", .01):
            gen = recorder.listen_windows(stop)
            for _ in range(5): next(gen)
            stop.set(); gen.close()
        self.assertGreater(len(opened), recorder.RECOVERY_MAX_FAILURES + 1)

    def test_stop_interrupts_recovery_wait(self):
        stop = Stop(); opened = []
        def wait(_): stop.set(); return True
        stop.wait = wait
        def op(*a, **k):
            opened.append(1); return Rec(), types.SimpleNamespace(name="fake"), []
        with capture_environment(op): self.assertEqual(list(recorder.listen_windows(stop)), [])
        self.assertEqual(len(opened), 1)

    def test_consumer_typeerror_is_not_retried_as_device_loss(self):
        opened = []
        def op(*a, **k):
            opened.append(1); return Rec(1000), types.SimpleNamespace(name="fake"), []
        with capture_environment(op):
            gen = recorder.listen_windows(Stop()); next(gen)
            with self.assertRaisesRegex(TypeError, "consumer"):
                gen.throw(TypeError("consumer error"))
        self.assertEqual(len(opened), 1)

    def test_stop_and_release_failures_release_all_and_preserve_original(self):
        releases = []
        def check(hr):
            if hr: raise RuntimeError("Stop failed")
        def release(ptr):
            releases.append(ptr)
            if ptr == "capture": raise RuntimeError("release failed")
        fn = recorder_method("__exit__", types.SimpleNamespace(check_error=check, release=release))
        ptr = [[types.SimpleNamespace(lpVtbl=types.SimpleNamespace(Stop=lambda p: -1))]]
        obj = types.SimpleNamespace(_ptr=ptr, _ppCaptureClient="capture")
        self.assertFalse(fn(obj, TypeError, TypeError("original"), None))
        self.assertEqual(releases, ["capture", ptr])
        with self.assertRaisesRegex(RuntimeError, "Stop failed"): fn(obj, None, None, None)

    def test_start_failure_releases_interfaces(self):
        released = []
        def check(hr):
            if hr: raise RuntimeError("Start failed")
        fn = recorder_method("__enter__", types.SimpleNamespace(check_error=check, release=released.append))
        ptr = [[types.SimpleNamespace(lpVtbl=types.SimpleNamespace(Start=lambda p: -1))]]
        obj = types.SimpleNamespace(_ptr=ptr, _capture_client=lambda: "capture")
        with self.assertRaises(RuntimeError): fn(obj)
        self.assertEqual(released, ["capture", ptr])

    def test_audio_typeerror_has_traceback(self):
        def broken(*a, **k):
            raise TypeError("CLexer.token() takes 0 positional arguments but 1 was given")
            yield
        feed = main.AudioFeed(); out = io.StringIO()
        with patch.object(recorder, "listen_windows", broken), contextlib.redirect_stderr(out): feed._run()
        self.assertIn("CLexer.token", feed.last_error)
        self.assertIn("Traceback", out.getvalue())

    def test_unexpected_generator_end_is_error(self):
        feed = main.AudioFeed()
        with patch.object(recorder, "listen_windows", lambda *a, **k: iter(())): feed._run()
        self.assertIn("提前结束", feed.last_error)

    def test_audio_can_restart_but_never_duplicate_live_thread(self):
        release = threading.Event(); calls = []
        def source(stop, *a, **k):
            calls.append(1)
            if len(calls) == 1: raise TypeError("first attempt")
            release.wait(1); stop.set(); yield np.ones(100, dtype=np.float32), .01
        feed = main.AudioFeed()
        with patch.object(recorder, "listen_windows", source):
            self.assertTrue(feed.start()); feed._thread.join(1)
            self.assertIn("first attempt", feed.last_error)
            self.assertTrue(feed.start()); self.assertFalse(feed.start())
            release.set(); feed._thread.join(1)
        self.assertIsNone(feed.last_error)
        self.assertEqual(feed.windows, 1)

    def test_user_restart_rebuilds_dead_feed(self):
        engine = main.Engine(main.parse_args([]))
        engine.audio = types.SimpleNamespace(_thread=object(), running=False, start=lambda: calls.append(1))
        calls = []; engine.set_active(True)
        self.assertEqual(calls, [1])

    def test_pause_and_new_generation_discard_inflight_result(self):
        engine = main.Engine(main.parse_args([])); engine.set_active(True)
        old = engine._generation; engine._work_context.generation = old
        engine._emit([{"key": "before"}], "", 95, "audio", time.perf_counter())
        payload = engine.results.get_nowait(); self.assertTrue(engine.result_is_current(payload))
        engine.set_active(False)
        engine._emit([{"key": "paused"}], "", 95, "audio", time.perf_counter())
        self.assertTrue(engine.results.empty())
        engine.set_active(True)
        engine._emit([{"key": "stale"}], "", 95, "audio", time.perf_counter())
        self.assertTrue(engine.results.empty()); self.assertFalse(engine.result_is_current(payload))
        engine._work_context.generation = engine._generation
        engine._emit([{"key": "new"}], "", 95, "audio", time.perf_counter())
        self.assertTrue(engine.result_is_current(engine.results.get_nowait()))

    def test_shutdown_cannot_restart_capture(self):
        engine = main.Engine(main.parse_args([]))
        engine.stop_event.set()
        with patch.object(engine.audio, "start") as start:
            self.assertFalse(engine.set_active(True))
            start.assert_not_called()

    def test_old_noise_profile_cannot_overwrite_new_capture(self):
        import denoise
        feed = main.AudioFeed()
        feed._capture_generation = 2
        with patch.object(denoise, "noise_psd_profile", return_value="old profile"):
            feed._refresh_profile(generation=1)
        self.assertIsNone(feed.profile)

    def test_results_are_bounded_and_keep_latest(self):
        engine = main.Engine(main.parse_args([])); engine.active.set()
        for n in range(20): engine._emit([{"key": str(n)}], "", 95, "audio", time.perf_counter())
        self.assertEqual(engine.results.qsize(), 8)
        items = []
        while not engine.results.empty(): items.append(engine.results.get_nowait())
        self.assertEqual(items[-1]["rows"][0]["key"], "19")

    def test_free_delay_is_preserved_and_cancelled_without_publication(self):
        engine = main.Engine(main.parse_args([])); engine.active.set()
        with patch.object(main, "RESULT_DELAY_S", .1):
            start = time.perf_counter(); engine._emit([], "", 0, "audio", start)
            self.assertGreaterEqual(time.perf_counter() - start, .095)
            engine.results.get_nowait()
            th = threading.Thread(target=engine._emit, args=([], "", 0, "audio", time.perf_counter()))
            th.start(); time.sleep(.02); engine.set_active(False); th.join(1)
            self.assertFalse(th.is_alive()); self.assertTrue(engine.results.empty())

    def test_dead_thread_and_stale_window_are_visible(self):
        audio = types.SimpleNamespace(last_error=None, _thread=object(), running=False)
        self.assertIn("已停止", main._detection_status_line(types.SimpleNamespace(audio=audio)))
        audio.running = True; audio.latest_age = 4
        self.assertIn("未更新", main._detection_status_line(types.SimpleNamespace(audio=audio)))

    def test_doctor_failure_returns_nonzero_and_no_false_device_diagnosis(self):
        out = io.StringIO()
        with patch.object(recorder, "doctor", side_effect=TypeError("parser error")), patch("builtins.open", side_effect=OSError("no file")), contextlib.redirect_stdout(out):
            rc = main.run_doctor(main.parse_args(["--audio-doctor"]))
        self.assertEqual(rc, 2); self.assertIn("Traceback", out.getvalue())
        self.assertNotIn("· 没有枚举到任何播放端点", out.getvalue())

    def test_source_entrypoint_uses_hard_exit_and_preserves_codes(self):
        for outcome, code in [(0, 0), (SystemExit(8), 8), (RuntimeError("test"), 1), (KeyboardInterrupt(), 130)]:
            with patch.object(main, "main", side_effect=outcome if isinstance(outcome, BaseException) else None, return_value=outcome), patch.object(launcher, "_hard_exit") as hard, contextlib.redirect_stderr(io.StringIO()):
                main._source_entrypoint(); self.assertEqual(hard.call_args.args[0], code)



class CloseEvent:
    def __init__(self, spontaneous=True): self.native = spontaneous; self.accepted = False; self.ignored = False
    def spontaneous(self): return self.native
    def accept(self): self.accepted = True
    def ignore(self): self.ignored = True


class WindowStability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ui
        cls.ui = ui; cls.app = ui.QApplication.instance() or ui.QApplication([])
    def setUp(self): self.panel = self.ui.Panel(None, start_thread=False)
    def tearDown(self): self.panel.close(); self.app.processEvents()
    def test_native_close_needs_two_requests(self):
        first, second = CloseEvent(), CloseEvent()
        with patch.object(self.ui.time, "monotonic", side_effect=[10., 11.]):
            self.panel.closeEvent(first); self.assertTrue(first.ignored); self.assertFalse(self.panel._closed)
            self.panel.closeEvent(second); self.assertTrue(second.accepted)
    def test_expired_confirmation_does_not_accept(self):
        with patch.object(self.ui.time, "monotonic", side_effect=[10., 13.]):
            self.panel.closeEvent(CloseEvent()); second = CloseEvent(); self.panel.closeEvent(second)
        self.assertTrue(second.ignored)
    def test_programmatic_close_bypasses_confirmation(self):
        event = CloseEvent(); self.panel._allow_close = True; self.panel.closeEvent(event)
        self.assertTrue(event.accepted)
    def test_cancelled_shutdown_restores_close_confirmation(self):
        from ctypes import wintypes
        msg = wintypes.MSG(); msg.message = 0x0011
        self.panel.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))
        msg.message = 0x0016; msg.wParam = 0
        self.panel.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))
        event = CloseEvent(); self.panel.closeEvent(event)
        self.assertTrue(event.ignored)

    def test_shutdown_bypasses_confirmation(self):
        from ctypes import wintypes
        msg = wintypes.MSG(); msg.message = 0x0011
        handled, answer = self.panel.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))
        self.assertTrue(handled); self.assertEqual(answer, 1)
        event = CloseEvent(); self.panel.closeEvent(event); self.assertTrue(event.accepted)


if __name__ == "__main__": unittest.main(verbosity=2)
