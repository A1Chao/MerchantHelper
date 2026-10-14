import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, 'deps'))
import comtypes
import soundcard as sc
def fmt(d):
    out = {}
    for k in ('name','id','channels','samplerate'):
        try: out[k] = getattr(d, k)
        except Exception as e: out[k] = f'ERR:{type(e).__name__}'
    return out
print('default_speaker :', fmt(sc.default_speaker()))
try:
    print('default_microphone:', fmt(sc.default_microphone()))
except Exception as e:
    print('default_microphone: ERR', e)
print()
print('--- loopback 端点 ---')
for m in sc.all_microphones(include_loopback=True):
    f = fmt(m)
    print('  loopback' if 'loopback' in m.name.lower() else '  capture ', f)
print()
print('--- 渲染端点 ---')
for s in sc.all_speakers():
    print('  ', fmt(s))
