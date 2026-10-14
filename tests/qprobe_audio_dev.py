import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comtypes  # noqa: 必须先于 soundcard（STA）
import soundcard as sc
print('默认扬声器(speaker):', sc.default_speaker().name, '| channels=', getattr(sc.default_speaker(),'channels',None))
try:
    print('默认麦克风(microphone):', sc.default_microphone().name)
except Exception as e:
    print('默认麦克风: 失败', e)
print()
print('--- 所有 loopback（可录的"扬声器"端点）---')
for m in sc.all_microphones(include_loopback=True):
    islb = 'loopback' in m.name.lower()
    try:
        ch = m.channels
    except Exception as e:
        ch = f'?{type(e).__name__}'
    # soundcard 的 loopback 端点通常暴露 samplerate=48000/44100
    try:
        sr = m.samplerate
    except Exception as e:
        sr = f'?{type(e).__name__}'
    print(f'  name={m.name!r} loopback={islb} channels={ch} samplerate={sr} id={getattr(m,"id","")[:40]}')
print()
print('--- 所有渲染端点（扬声器）---')
for s in sc.all_speakers():
    try: ch = s.channels
    except Exception as e: ch = f'?{type(e).__name__}'
    try: sr = s.samplerate
    except Exception as e: sr = f'?{type(e).__name__}'
    print(f'  name={s.name!r} channels={ch} samplerate={sr}')
