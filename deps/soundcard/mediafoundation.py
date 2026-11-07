"""Re-Implementation of https://msdn.microsoft.com/en-us/library/windows/desktop/aa369729%28v=vs.85%29.aspx using the CFFI"""

import os
import cffi
import re
import time
import struct
import collections
import platform
import warnings

import numpy

_ffi = cffi.FFI()
_package_dir, _ = os.path.split(__file__)
with open(os.path.join(_package_dir, 'mediafoundation.py.h'), 'rt') as f:
    _ffi.cdef(f.read())

_ole32 = _ffi.dlopen('ole32')


# use a custom warning subclass that is always shown, instead of once:
class SoundcardRuntimeWarning(RuntimeWarning):
    pass

warnings.simplefilter('always', SoundcardRuntimeWarning)


class _COMLibrary:
    """General functionality of the COM library.

    This class contains functionality related to the COM library, for:
    - initializing and uninitializing the library.
    - checking HRESULT codes.
    - decrementing the reference count of COM objects.

    """

    def __init__(self):
        COINIT_MULTITHREADED = 0x0
        if platform.win32_ver()[0] == '8':
            # On Windows 8, according to Microsoft, the first use of 
            # IAudioClient should be from the STA thread. Calls from 
            # an MTA thread may result in undefined behavior.
            
            # CoInitialize initialises calling thread to STA.
            hr = _ole32.CoInitialize(_ffi.NULL)
        else:
            hr = _ole32.CoInitializeEx(_ffi.NULL, COINIT_MULTITHREADED)

        try:
            self.check_error(hr)

            # Flag to keep track if this class directly initialized
            # COM, required for un-initializing in destructor:
            self.com_loaded = True

        except RuntimeError as e:
            # Error 0x80010106
            RPC_E_CHANGED_MODE = 0x80010106
            if hr + 2 ** 32 == RPC_E_CHANGED_MODE:
                # COM was already initialized before (e.g. by the
                # debugger), therefore trying to initialize it again
                # fails we can safely ignore this error, but we have
                # to make sure that we don't try to unload it
                # afterwards therefore we set this flag to False:
                self.com_loaded = False
            else:
                raise e

    def __del__(self):
        # Don't un-initialize COM if COM was not initialized directly
        # by this class:
        if self.com_loaded:
            _ole32.CoUninitialize()

    @staticmethod
    def check_error(hresult):
        """Check a given HRESULT for errors.

        Throws an error for non-S_OK HRESULTs.

        """
        # see shared/winerror.h:
        S_OK = 0
        E_NOINTERFACE = 0x80004002
        E_POINTER = 0x80004003
        E_OUTOFMEMORY = 0x8007000e
        E_INVALIDARG = 0x80070057
        E_ACCESSDENIED = 0x80070005
        AUDCLNT_E_UNSUPPORTED_FORMAT = 0x88890008
        # --- 兼容性补丁（非上游代码）：把常见 WASAPI 错误翻译成人话 -------------
        # 上游只认 5 个 HRESULT，其余都输出 "Error 0x…"，日志里等于没信息。
        # 采集失败时用户/维护者最需要知道的是「设备被独占 / 格式不支持 / 设备不可用」。
        _MESSAGES = {
            0x80070005: 'E_ACCESSDENIED（无权限：设备被其它进程独占，或被系统策略禁止）',
            0x80070057: 'E_INVALIDARG（参数/格式不被该设备接受）',
            0x88890003: 'AUDCLNT_E_WRONG_ENDPOINT_TYPE（设备方向不对：这不是播放端点）',
            0x88890004: 'AUDCLNT_E_DEVICE_INVALIDATED（设备已失效：被拔出/切换/驱动重载）',
            0x88890006: 'AUDCLNT_E_BUFFER_SIZE_ERROR（缓冲大小不被支持）',
            0x88890007: 'AUDCLNT_E_OUT_OF_ORDER（调用顺序错误）',
            0x88890008: 'AUDCLNT_E_UNSUPPORTED_FORMAT（设备不支持该采样率/位深/声道）',
            0x8889000A: 'AUDCLNT_E_DEVICE_IN_USE（设备正被独占模式占用）',
            0x8889000B: 'AUDCLNT_E_ENDPOINT_CREATE_FAILED（端点创建失败：驱动异常）',
            0x8889000E: 'AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED（该设备不允许独占模式）',
            0x88890010: 'AUDCLNT_E_SERVICE_NOT_RUNNING（Windows 音频服务未运行）',
            0x88890011: 'AUDCLNT_E_EVENTHANDLE_NOT_EXPECTED（事件句柄不匹配）',
            0x88890017: 'AUDCLNT_E_CPUUSAGE_EXCEEDED（音频引擎 CPU 超限）',
        }
        if hresult == S_OK:
            return
        code = hresult + 2 ** 32
        if code in _MESSAGES:
            raise RuntimeError('{} [0x{:08x}]'.format(_MESSAGES[code], code))
        elif code == E_NOINTERFACE:
            raise RuntimeError('The specified class does not implement the '
                               'requested interface, or the controlling '
                               'IUnknown does not expose the requested '
                               'interface.')
        elif code == E_POINTER:
            raise RuntimeError('An argument is NULL.')
        elif code == E_INVALIDARG:
            raise RuntimeError("invalid argument")
        elif code == E_OUTOFMEMORY:
            raise RuntimeError("out of memory")
        elif code == E_ACCESSDENIED:
            raise RuntimeError('access denied')
        elif code == AUDCLNT_E_UNSUPPORTED_FORMAT:
            raise RuntimeError("unsupported format")
        else:
            raise RuntimeError('Error {}'.format(hex(code)))

    @staticmethod
    def release(ppObject):
        """Decrement reference count on COM object."""
        if ppObject[0] != _ffi.NULL:
            ppObject[0][0].lpVtbl.Release(ppObject[0])
            ppObject[0] = _ffi.NULL

_com = _COMLibrary()

#: KSDATAFORMAT_SUBTYPE_IEEE_FLOAT（00000003-0000-0010-8000-00AA00389B71）的 16 字节小端表示。
_IEEE_FLOAT_SUBFORMAT = bytes.fromhex('0300000000001000800000aa00389b71')


def _describe_format(raw):
    """把 40 字节 WAVEFORMATEXTENSIBLE 描述成一行日志（诊断用，不抛异常）。"""
    try:
        return ('tag=0x%04x cbSize=%d ch=%d rate=%d bits=%d sub=%s'
                % (int.from_bytes(raw[0:2], 'little'),
                   int.from_bytes(raw[16:18], 'little'),
                   int.from_bytes(raw[2:4], 'little'),
                   int.from_bytes(raw[4:8], 'little'),
                   int.from_bytes(raw[14:16], 'little'),
                   raw[24:40].hex()))
    except Exception:
        return 'unknown'


def _pack_float_format(samplerate, channels, channelmask):
    """手工拼一个 32-bit float 的 WAVEFORMATEXTENSIBLE（40 字节）。

    为什么需要（兼容性补丁，非上游代码）：部分蓝牙耳机 / 廉价 USB 声卡的混音格式是
    16-bit PCM，甚至给的是 18 字节裸 WAVEFORMATEX；上游 soundcard 在 `_AudioClient`
    里硬 assert「必须是 float32 的 WAVEFORMATEXTENSIBLE」，一旦不满足就
    AssertionError，采集直接打不开（用户侧表现：某些型号耳机完全没声音）。
    WASAPI 的 AUTOCONVERTPCM 允许客户端请求 float32、由音频引擎做位深/采样率转换，
    所以这里不再依赖设备给的格式，直接按需要的采样率/声道数拼一个标准 float32 结构。
    """
    data = bytearray(40)
    struct.pack_into('<HHIIHHHHI', data, 0, 0xFFFE, int(channels), int(samplerate),
                     int(samplerate) * int(channels) * 4, int(channels) * 4,
                     32, 22, 32, int(channelmask))
    data[24:40] = _IEEE_FLOAT_SUBFORMAT
    buf = _ffi.new('char[]', 40)
    _ffi.memmove(buf, bytes(data), 40)
    return _ffi.cast('WAVEFORMATEXTENSIBLE *', buf), buf

def all_speakers():
    """A list of all connected speakers."""
    with _DeviceEnumerator() as enum:
        return [_Speaker(dev) for dev in enum.all_devices('speaker')]

def default_speaker():
    """The default speaker of the system."""
    with _DeviceEnumerator() as enum:
        return _Speaker(enum.default_device('speaker'))

def get_speaker(id):
    """Get a specific speaker by a variety of means.

    id can be an a WASAPI id, a substring of the speaker name, or a
    fuzzy-matched pattern for the speaker name.

    """
    return _match_device(id, all_speakers())

def all_microphones(include_loopback=False):
    """A list of all connected microphones.

    By default, this does not include loopback (virtual microphones
    that record the output of a speaker).

    """

    with _DeviceEnumerator() as enum:
        if include_loopback:
            return [_Microphone(dev, isloopback=True) for dev in enum.all_devices('speaker')] + [_Microphone(dev) for dev in enum.all_devices('microphone')]
        else:
            return [_Microphone(dev) for dev in enum.all_devices('microphone')]

def default_microphone():
    """The default microphone of the system."""
    with _DeviceEnumerator() as enum:
        return _Microphone(enum.default_device('microphone'))

def get_microphone(id, include_loopback=False):
    """Get a specific microphone by a variety of means.

    id can be a WASAPI id, a substring of the microphone name, or a
    fuzzy-matched pattern for the microphone name.

    """
    return _match_device(id, all_microphones(include_loopback))

def _match_device(id, devices):
    """Find id in a list of devices.

    id can be a WASAPI id, a substring of the device name, or a
    fuzzy-matched pattern for the microphone name.

    """
    devices_by_id = {device.id: device for device in devices}
    devices_by_name = {device.name: device for device in devices}
    if id in devices_by_id:
        return devices_by_id[id]
    # try substring match:
    for name, device in devices_by_name.items():
        if id in name:
            return device
    # try fuzzy match:
    pattern = '.*'.join(id)
    for name, device in devices_by_name.items():
        if re.match(pattern, name):
            return device
    raise IndexError('no device with id {}'.format(id))

def _str2wstr(string):
    """Converts a Python str to a Windows WSTR_T."""
    return _ffi.new('int16_t[]', [ord(s) for s in string]+[0])

def _guidof(uuid_str):
    """Creates a Windows LPIID from a str."""
    IID = _ffi.new('LPIID')
    # convert to zero terminated wide string
    uuid = _str2wstr(uuid_str)
    hr = _ole32.IIDFromString(_ffi.cast("char*", uuid), IID)
    _com.check_error(hr)
    return IID


def get_name():
    raise NotImplementedError()


def set_name(name):
    raise NotImplementedError()


class _DeviceEnumerator:
    """Wrapper class for an IMMDeviceEnumerator**.

    Provides methods for retrieving _Devices and pointers to the
    underlying IMMDevices.

    """

    def __init__(self):
        self._ptr = _ffi.new('IMMDeviceEnumerator **')
        IID_MMDeviceEnumerator = _guidof("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        IID_IMMDeviceEnumerator = _guidof("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        # see shared/WTypesbase.h and um/combaseapi.h:
        CLSCTX_ALL = 23
        hr = _ole32.CoCreateInstance(IID_MMDeviceEnumerator, _ffi.NULL, CLSCTX_ALL,
                                  IID_IMMDeviceEnumerator, _ffi.cast("void **", self._ptr))
        _com.check_error(hr)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        _com.release(self._ptr)

    def __del__(self):
        _com.release(self._ptr)

    def _device_id(self, device_ptr):
        """Returns the WASAPI device ID for an IMMDevice**."""
        ppId = _ffi.new('LPWSTR *')
        hr = device_ptr[0][0].lpVtbl.GetId(device_ptr[0], ppId)
        _com.check_error(hr)
        return _ffi.string(ppId[0])

    def all_devices(self, kind):
        """Yields all sound cards of a given kind.

        Kind may be 'speaker' or 'microphone'.
        Sound cards are returned as _Device objects.

        """
        if kind == 'speaker':
            data_flow = 0 # render
        elif kind == 'microphone':
            data_flow = 1 # capture
        else:
            raise TypeError('Invalid kind: {}'.format(kind))

        DEVICE_STATE_ACTIVE = 0x1
        ppDevices = _ffi.new('IMMDeviceCollection **')
        hr = self._ptr[0][0].lpVtbl.EnumAudioEndpoints(self._ptr[0], data_flow, DEVICE_STATE_ACTIVE, ppDevices);
        _com.check_error(hr)

        for ppDevice in _DeviceCollection(ppDevices):
            device = _Device(self._device_id(ppDevice))
            _com.release(ppDevice)
            yield device

    def default_device(self, kind):
        """Returns the default sound card of a given kind.

        Kind may be 'speaker' or 'microphone'.
        Default sound card is returned as a _Device object.

        """
        if kind == 'speaker':
            data_flow = 0 # render
        elif kind == 'microphone':
            data_flow = 1 # capture
        else:
            raise TypeError('Invalid kind: {}'.format(kind))

        ppDevice = _ffi.new('IMMDevice **')
        eConsole = 0
        hr = self._ptr[0][0].lpVtbl.GetDefaultAudioEndpoint(self._ptr[0], data_flow, eConsole, ppDevice);
        _com.check_error(hr)
        device = _Device(self._device_id(ppDevice))
        _com.release(ppDevice)
        return device

    def device_ptr(self, devid):
        """Retrieve IMMDevice** for a WASAPI device ID."""
        ppDevice = _ffi.new('IMMDevice **')
        devid = _str2wstr(devid)
        hr = self._ptr[0][0].lpVtbl.GetDevice(self._ptr[0], _ffi.cast('wchar_t *', devid), ppDevice);
        _com.check_error(hr)
        return ppDevice

class _DeviceCollection:
    """Wrapper class for an IMMDeviceCollection**.

    Generator for IMMDevice** pointers.

    """
    def __init__(self, ptr):
        self._ptr = ptr

    def __del__(self):
        _com.release(self._ptr)

    def __len__(self):
        pCount = _ffi.new('UINT *')
        hr = self._ptr[0][0].lpVtbl.GetCount(self._ptr[0], pCount)
        _com.check_error(hr)
        return pCount[0]

    def __getitem__(self, idx):
        if idx >= len(self):
            raise StopIteration()
        ppDevice = _ffi.new('IMMDevice **')
        hr = self._ptr[0][0].lpVtbl.Item(self._ptr[0], idx, ppDevice)
        _com.check_error(hr)
        return ppDevice

class _PropVariant:
    """Wrapper class for a PROPVARIANT.

    Correctly allocates and frees a PROPVARIANT. Normal CFFI
    malloc/free is incompatible with PROPVARIANTs, since COM expects
    PROPVARIANTS to be freely reallocatable by its own allocator.

    Access the PROPVARIANT* pointer using .ptr.

    """
    def __init__(self):
        self.ptr = _ole32.CoTaskMemAlloc(_ffi.sizeof('PROPVARIANT'))
        self.ptr = _ffi.cast("PROPVARIANT *", self.ptr)

    def __del__(self):
        hr = _ole32.PropVariantClear(self.ptr)
        _com.check_error(hr)

class _Device:
    """Wrapper class for an IMMDevice.

    Implements memory management and retrieval of the device name, the
    number of channels, and device activation.

    Subclassed by _Speaker and _Microphone for playback and recording.

    """

    def __init__(self, id):
        self._id = id

    def _device_ptr(self):
        with _DeviceEnumerator() as enum:
            return enum.device_ptr(self._id)

    @property
    def id(self):
        return self._id

    @property
    def name(self):
        # um/coml2api.h:
        ppPropertyStore = _ffi.new('IPropertyStore **')
        ptr = self._device_ptr()
        hr = ptr[0][0].lpVtbl.OpenPropertyStore(ptr[0], 0, ppPropertyStore)
        _com.release(ptr)
        _com.check_error(hr)
        propvariant = _PropVariant()
        # um/functiondiscoverykeys_devpkey.h and https://msdn.microsoft.com/en-us/library/windows/desktop/dd370812(v=vs.85).aspx
        PKEY_Device_FriendlyName = _ffi.new("PROPERTYKEY *",
                                            [[0xa45c254e, 0xdf1c, 0x4efd, [0x80, 0x20, 0x67, 0xd1, 0x46, 0xa8, 0x50, 0xe0]],
                                            14])
        hr = ppPropertyStore[0][0].lpVtbl.GetValue(ppPropertyStore[0], PKEY_Device_FriendlyName, propvariant.ptr)
        _com.check_error(hr)
        if propvariant.ptr[0].vt != 31:
            raise RuntimeError('Property was expected to be a string, but is not a string')
        data = _ffi.cast("short*", propvariant.ptr[0].data)
        for idx in range(256):
            if data[idx] == 0:
                break
        devicename = struct.pack('h' * idx, *data[0:idx]).decode('utf-16')
        _com.release(ppPropertyStore)
        return devicename

    @property
    def channels(self):
        # um/coml2api.h:
        ppPropertyStore = _ffi.new('IPropertyStore **')
        ptr = self._device_ptr()
        hr = ptr[0][0].lpVtbl.OpenPropertyStore(ptr[0], 0, ppPropertyStore)
        _com.release(ptr)
        _com.check_error(hr)
        propvariant = _PropVariant()
        # um/functiondiscoverykeys_devpkey.h and https://msdn.microsoft.com/en-us/library/windows/desktop/dd370812(v=vs.85).aspx
        PKEY_AudioEngine_DeviceFormat = _ffi.new("PROPERTYKEY *",
                                                 [[0xf19f064d, 0x82c, 0x4e27, [0xbc, 0x73, 0x68, 0x82, 0xa1, 0xbb, 0x8e, 0x4c]],
                                                  0])
        hr = ppPropertyStore[0][0].lpVtbl.GetValue(ppPropertyStore[0], PKEY_AudioEngine_DeviceFormat, propvariant.ptr)
        _com.release(ppPropertyStore)
        _com.check_error(hr)
        # --- 兼容性补丁（非上游代码）-------------------------------------------
        # 上游这里断言属性必须是 40 字节的 WAVEFORMATEXTENSIBLE blob：
        #   assert pPropVariantBlob[0].blob.cbSize == 40
        # 但「设备默认格式」在只设成 16-bit 立体声时，Windows 存的是 18 字节的
        # 裸 WAVEFORMATEX（cbSize==18），断言直接抛 AssertionError →
        # `_Microphone.recorder()` 里的 `self.channels` 把整个采集打挂，
        # 表现为「某些型号耳机一开监测就没声音/无信号」，管理员权限也没用。
        # 这里改成：能读几个通道就读几个，读不到再退回混音格式，绝不抛断言。
        if propvariant.ptr[0].vt == 65:
            pPropVariantBlob = _ffi.cast("BLOB_PROPVARIANT *", propvariant.ptr)
            cb = int(pPropVariantBlob[0].blob.cbSize)
            if cb >= 18:                       # WAVEFORMATEX 或 WAVEFORMATEXTENSIBLE 都够读 nChannels
                waveformat = _ffi.cast("WAVEFORMATEX *", pPropVariantBlob[0].blob.pBlobData)
                channels = int(waveformat[0].nChannels)
                if channels > 0:
                    return channels
        return self._mix_format_channels()

    def _mix_format_channels(self):
        """读取设备混音格式的通道数；任何失败都退回 2 声道（绝不抛异常）。"""
        try:
            ac = self._audio_client()
            try:
                ppFormat = _ffi.new('WAVEFORMATEXTENSIBLE**')
                hr = ac[0][0].lpVtbl.GetMixFormat(ac[0], ppFormat)
                _com.check_error(hr)
                ch = int(ppFormat[0][0].Format.nChannels)
                _ole32.CoTaskMemFree(ppFormat[0])
                return ch if ch > 0 else 2
            finally:
                _com.release(ac)
        except Exception:
            return 2

    def _audio_client(self):
        CLSCTX_ALL = 23
        ppAudioClient = _ffi.new("IAudioClient **")
        IID_IAudioClient = _guidof("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
        ptr = self._device_ptr()
        hr = ptr[0][0].lpVtbl.Activate(ptr[0], IID_IAudioClient, CLSCTX_ALL, _ffi.NULL, _ffi.cast("void**", ppAudioClient))
        _com.release(ptr)
        _com.check_error(hr)
        return ppAudioClient

class _Speaker(_Device):
    """A soundcard output. Can be used to play audio.

    Use the `play` method to play one piece of audio, or use the
    `player` method to get a context manager for playing continuous
    audio.

    Properties:
    - `channels`: the number of available channels.
    - `name`: the name of the sound card.
    - `id`: the WASAPI ID of the sound card.

    """

    def __init__(self, device):
        self._id = device._id

    def __repr__(self):
        return '<Speaker {} ({} channels)>'.format(self.name,self.channels)

    def player(self, samplerate, channels=None, blocksize=None, exclusive_mode=False):
        if channels is None:
            channels = self.channels
        return _Player(self._audio_client(), samplerate, channels, blocksize, False, exclusive_mode)

    def play(self, data, samplerate, channels=None, blocksize=None):
        with self.player(samplerate, channels, blocksize) as p:
            p.play(data)


class _Microphone(_Device):
    """A soundcard input. Can be used to record audio.

    Use the `record` method to record one piece of audio, or use the
    `recorder` method to get a context manager for recording
    continuous audio.

    Properties:
    - `channels`: the number of available channels.
    - `name`: the name of the sound card.
    - `id`: the WASAPI ID of the sound card.

    """

    def __init__(self, device, isloopback=False):
        self._id = device._id
        self.isloopback = isloopback

    def __repr__(self):
        if self.isloopback:
            return '<Loopback {} ({} channels)>'.format(self.name,self.channels)
        else:
            return '<Microphone {} ({} channels)>'.format(self.name,self.channels)

    def recorder(self, samplerate, channels=None, blocksize=None, exclusive_mode=False):
        if channels is None:
            channels = self.channels
        ptr = self._audio_client()
        try:
            return _Recorder(ptr, samplerate, channels, blocksize, self.isloopback, exclusive_mode)
        except Exception:
            try:
                _com.release(ptr)
            except Exception:
                pass
            raise

    def record(self, numframes, samplerate, channels=None, blocksize=None):
        with self.recorder(samplerate, channels, blocksize) as r:
            return r.record(numframes)

class _AudioClient:
    """Wrapper class for an IAudioClient** object.

    Implements memory management and various property retrieval for
    IAudioClient objects.

    Subclassed by _Player and _Recorder for playback and recording.

    """

    def __init__(self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode=False):
        self._ptr = ptr

        if isinstance(channels, int):
            self.channelmap = list(range(channels))
        elif isinstance(channels, collections.abc.Iterable):
            self.channelmap = channels
        else:
            raise TypeError('channels must be iterable or integer')

        if list(range(len(set(self.channelmap)))) != sorted(list(set(self.channelmap))):
            raise TypeError('Due to limitations of WASAPI, channel maps on Windows '
                            'must be a combination of `range(0, x)`.')

        if blocksize is None:
            blocksize = self.deviceperiod[0]*samplerate

        ppMixFormat = _ffi.new('WAVEFORMATEXTENSIBLE**')
        hr = self._ptr[0][0].lpVtbl.GetMixFormat(self._ptr[0], ppMixFormat)
        _com.check_error(hr)
        raw = bytes(_ffi.buffer(ppMixFormat[0], 40))
        self.device_mix_format = _describe_format(raw)

        channels = len(set(self.channelmap))
        channelmask = 0
        for ch in self.channelmap:
            channelmask |= 1<<ch

        # --- 兼容性补丁（非上游代码）-----------------------------------------
        # 上游这里用 4 个 assert 死认「设备混音格式 = WAVEFORMATEXTENSIBLE 32-bit float」，
        # 不满足就 AssertionError（整个采集打不开）。现实里蓝牙耳机、廉价 USB 声卡
        # 可能给 16-bit PCM 混音格式。处理方式：
        #   * 是 float32 → 沿用设备给的结构（与旧版行为完全一致）；
        #   * 不是 float32 → 自建标准 float32 结构，交给 AUTOCONVERTPCM 转换位深。
        is_ext = raw[0:2] == b'\xfe\xff'
        is_float32 = (is_ext
                      and raw[24:40] == _IEEE_FLOAT_SUBFORMAT
                      and int.from_bytes(raw[14:16], 'little') == 32
                      and int.from_bytes(raw[16:18], 'little') == 22)
        if is_float32:
            fmt = ppMixFormat[0]
            fmt.Format.nChannels=channels
            fmt.Format.nSamplesPerSec=int(samplerate)
            fmt.Format.nAvgBytesPerSec=int(samplerate) * channels * 4
            fmt.Format.nBlockAlign=channels * 4
            fmt.Format.wBitsPerSample=32
            fmt.Samples=dict(wValidBitsPerSample=32)
            # does not work:
            # fmt.dwChannelMask=channelmask
            keepalive = None
            self.client_format = 'float32/设备格式 %dHz/%dch' % (int(samplerate), channels)
        else:
            fmt, keepalive = _pack_float_format(int(samplerate), channels, channelmask)
            self.client_format = ('float32(自建) %dHz/%dch ← 设备混音格式 %s'
                                  % (int(samplerate), channels, self.device_mix_format))

        if exclusive_mode:
            sharemode = _ole32.AUDCLNT_SHAREMODE_EXCLUSIVE
        else:
            sharemode = _ole32.AUDCLNT_SHAREMODE_SHARED
        #             resample   | remix      | better-SRC | nopersist
        streamflags = 0x00100000 | 0x80000000 | 0x08000000 | 0x00080000
        if isloopback:
            streamflags |= 0x00020000 #loopback
        bufferduration = int(blocksize/samplerate * 10000000) # in hecto-nanoseconds (1000_000_0)
        try:
            hr = self._ptr[0][0].lpVtbl.Initialize(self._ptr[0], sharemode, streamflags, bufferduration, 0, fmt, _ffi.NULL)
            _com.check_error(hr)
        finally:
            _ole32.CoTaskMemFree(ppMixFormat[0])
        del keepalive

        # save samplerate for later
        self.samplerate = samplerate
        # placeholder for the last time we had audio input available
        self._idle_start_time = None


    @property
    def buffersize(self):
        pBufferSize = _ffi.new("UINT32*")
        hr = self._ptr[0][0].lpVtbl.GetBufferSize(self._ptr[0], pBufferSize)
        _com.check_error(hr)
        return pBufferSize[0]

    @property
    def deviceperiod(self):
        pDefaultPeriod = _ffi.new("REFERENCE_TIME*")
        pMinimumPeriod = _ffi.new("REFERENCE_TIME*")
        hr = self._ptr[0][0].lpVtbl.GetDevicePeriod(self._ptr[0], pDefaultPeriod, pMinimumPeriod)
        _com.check_error(hr)
        return pDefaultPeriod[0]/10_000_000, pMinimumPeriod[0]/10_000_000

    def cached_deviceperiod(self):
        """取一次设备周期并缓存（不要每圈都问系统）。

        为什么必须缓存（非上游代码，2026-10-31 20:24 崩溃的直接原因）：
        上游 ``_record_chunk()`` 在「设备暂时没有可用帧」的等待循环里，**每一圈**
        都调用 ``self.deviceperiod``，而设备静音/选错设备时这个循环一秒会转几百圈，
        等于一秒几百次 COM 调用。蓝牙耳机休眠、端点被切换或驱动重载时，
        ``self._ptr`` 指向的 IAudioClient 已经失效，下一次 GetDevicePeriod 就会
        访问违例（c0000005），进程当场被系统杀掉 —— Python / faulthandler 只能在
        事后留下栈，救不回来。
        设备周期在一次会话内不会变，取一次就够；顺带把每秒数百次 COM 调用降到 1 次。
        """
        period = getattr(self, "_deviceperiod_cache", None)
        if period is None:
            period = self.deviceperiod
            self._deviceperiod_cache = period
        return period

    @property
    def currentpadding(self):
        pPadding = _ffi.new("UINT32*")
        hr = self._ptr[0][0].lpVtbl.GetCurrentPadding(self._ptr[0], pPadding)
        _com.check_error(hr)
        return pPadding[0]

class _Player(_AudioClient):
    """A context manager for an active output stream.

    Audio playback is available as soon as the context manager is
    entered. Audio data can be played using the `play` method.
    Successive calls to `play` will queue up the audio one piece after
    another. If no audio is queued up, this will play silence.

    This context manager can only be entered once, and can not be used
    after it is closed.

    """

    # https://msdn.microsoft.com/en-us/library/windows/desktop/dd316756(v=vs.85).aspx
    def _render_client(self):
        iid = _guidof("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
        ppRenderClient = _ffi.new("IAudioRenderClient**")
        hr = self._ptr[0][0].lpVtbl.GetService(self._ptr[0], iid, _ffi.cast("void**", ppRenderClient))
        _com.check_error(hr)
        return ppRenderClient

    def _render_buffer(self, numframes):
        data = _ffi.new("BYTE**")
        hr = self._ppRenderClient[0][0].lpVtbl.GetBuffer(self._ppRenderClient[0], numframes, data)
        _com.check_error(hr)
        return data

    def _render_release(self, numframes):
        hr = self._ppRenderClient[0][0].lpVtbl.ReleaseBuffer(self._ppRenderClient[0], numframes, 0)
        _com.check_error(hr)

    def _render_available_frames(self):
        return self.buffersize-self.currentpadding

    def __enter__(self):
        self._ppRenderClient = self._render_client()
        hr = self._ptr[0][0].lpVtbl.Start(self._ptr[0])
        _com.check_error(hr)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        hr = self._ptr[0][0].lpVtbl.Stop(self._ptr[0])
        _com.check_error(hr)
        _com.release(self._ppRenderClient)
        _com.release(self._ptr)

    def play(self, data):
        """Play some audio data.

        Internally, all data is handled as float32 and with the
        appropriate number of channels. For maximum performance,
        provide data as a `frames x channels` float32 numpy array.

        If single-channel or one-dimensional data is given, this data
        will be played on all available channels.

        This function will return *before* all data has been played,
        so that additional data can be provided for gapless playback.
        The amount of buffering can be controlled through the
        blocksize of the player object.

        If data is provided faster than it is played, later pieces
        will be queued up and played one after another.

        """

        data = numpy.array(data, dtype='float32', order='C')
        if data.ndim == 1:
            data = data[:, None] # force 2d
        if data.ndim != 2:
            raise TypeError('data must be 1d or 2d, not {}d'.format(data.ndim))
        if data.shape[1] == 1 and len(set(self.channelmap)) != 1:
            data = numpy.tile(data, [1, len(set(self.channelmap))])

        # internally, channel numbers are always ascending:
        sortidx = sorted(range(len(self.channelmap)), key=lambda k: self.channelmap[k])
        data = data[:, sortidx]

        if data.shape[1] != len(set(self.channelmap)):
            raise TypeError('second dimension of data must be equal to the number of channels, not {}'.format(data.shape[1]))

        while data.nbytes > 0:
            towrite = self._render_available_frames()
            if towrite == 0:
                time.sleep(0.001)
                continue
            bytes = data[:towrite].ravel().tobytes()
            buffer = self._render_buffer(towrite)
            _ffi.memmove(buffer[0], bytes, len(bytes))
            self._render_release(towrite)
            data = data[towrite:]

class _Recorder(_AudioClient):
    """A context manager for an active input stream.

    Audio recording is available as soon as the context manager is
    entered. Recorded audio data can be read using the `record`
    method. If no audio data is available, `record` will block until
    the requested amount of audio data has been recorded.

    This context manager can only be entered once, and can not be used
    after it is closed.

    """

    # https://msdn.microsoft.com/en-us/library/windows/desktop/dd370800(v=vs.85).aspx
    def _capture_client(self):
        iid = _guidof("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
        ppCaptureClient = _ffi.new("IAudioCaptureClient**")
        hr = self._ptr[0][0].lpVtbl.GetService(self._ptr[0], iid, _ffi.cast("void**", ppCaptureClient))
        _com.check_error(hr)
        return ppCaptureClient

    def _capture_buffer(self):
        data = _ffi.new("BYTE**")
        toread = _ffi.new('UINT32*')
        flags = _ffi.new('DWORD*')
        hr = self._ppCaptureClient[0][0].lpVtbl.GetBuffer(self._ppCaptureClient[0], data, toread, flags, _ffi.NULL, _ffi.NULL)
        _com.check_error(hr)
        return data[0], toread[0], flags[0]

    def _capture_release(self, numframes):
        hr = self._ppCaptureClient[0][0].lpVtbl.ReleaseBuffer(self._ppCaptureClient[0], numframes)
        _com.check_error(hr)

    def _capture_available_frames(self):
        pSize = _ffi.new("UINT32*")
        hr = self._ppCaptureClient[0][0].lpVtbl.GetNextPacketSize(self._ppCaptureClient[0], pSize)
        _com.check_error(hr)
        return pSize[0]

    def __enter__(self):
        try:
            self._ppCaptureClient = self._capture_client()
            hr = self._ptr[0][0].lpVtbl.Start(self._ptr[0])
            _com.check_error(hr)
        except Exception:
            for ptr in (getattr(self, "_ppCaptureClient", None), self._ptr):
                if ptr is not None:
                    try:
                        _com.release(ptr)
                    except Exception:
                        pass
            raise
        self._pending_chunk = numpy.zeros([0], dtype='float32')
        self._is_first_frame = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        errors = []
        try:
            hr = self._ptr[0][0].lpVtbl.Stop(self._ptr[0])
            _com.check_error(hr)
        except Exception as exc:
            errors.append(exc)
        finally:
            # 两个接口独立释放；Stop/某次 Release 失败不能跳过剩余清理。
            for ptr in (getattr(self, "_ppCaptureClient", None), self._ptr):
                if ptr is not None:
                    try:
                        _com.release(ptr)
                    except Exception as exc:
                        errors.append(exc)
        if errors:
            if exc_type is None:
                raise errors[0]
            try:
                import sys
                print("[audio] Stop/Release 失败，保留原始异常：%s" % errors[0],
                      file=sys.stderr, flush=True)
            except Exception:
                pass
        return False

    def _record_chunk(self):
        """Record one chunk of audio data, as returned by WASAPI

        The data will be returned as a 1D numpy array, which will be used by
        the `record` method. This function is the interface of the `_Recorder`
        object with WASAPI.

        """

        while self._capture_available_frames() == 0:
            # some sound cards indicate silence by not making any
            # frames available. If that is the case, we need to
            # estimate the number of zeros to return, by measuring the
            # silent time:
            if self._idle_start_time is None:
                self._idle_start_time = time.perf_counter_ns()

            default_block_length, minimum_block_length = self.cached_deviceperiod()
            time.sleep(minimum_block_length/4)
            elapsed_time_ns = time.perf_counter_ns() - self._idle_start_time
            # waiting times shorter than a block length or so are
            # normal, and not indicative of a silent sound card. If
            # the waiting times get longer however, we must assume
            # that there is no audio data forthcoming, and return
            # zeros instead:
            if elapsed_time_ns / 1_000_000_000 > default_block_length * 4:
                num_frames = int(self.samplerate * elapsed_time_ns / 1_000_000_000)
                # 上限保护（非上游代码）。2026-10-31 20:52 实测：进程内存一次从 199MB
                # 跳到 1482MB 并保持 16 分钟不降 —— 就是这一行按"等待了多久"算出
                # 天文数字的帧数、一次性分配了约 1.28GB 的 zeros，然后被 record()
                # 切成 _pending_chunk 永久挂住（每读一块还要再拼一次，等于卡死）。
                # 静音补零没有信息量：最多补 1 秒，并把计时基准重置到"现在"。
                if num_frames > int(self.samplerate):
                    num_frames = int(self.samplerate)
                    self._idle_start_time = time.perf_counter_ns()
                else:
                    self._idle_start_time += elapsed_time_ns
                num_channels = len(set(self.channelmap))
                return numpy.zeros([num_frames * num_channels], dtype='float32')

        self._idle_start_time = None
        data_ptr, nframes, flags = self._capture_buffer()
        if data_ptr != _ffi.NULL:
            chunk = numpy.frombuffer(_ffi.buffer(data_ptr, nframes*4*len(set(self.channelmap))), dtype='float32').copy()
        else:
            raise RuntimeError('Could not create capture buffer')
        if flags & _ole32.AUDCLNT_BUFFERFLAGS_SILENT:
            # see https://learn.microsoft.com/en-us/windows/win32/api/audioclient/ne-audioclient-_audclnt_bufferflags
            chunk[:] = 0
        if self._is_first_frame:
            # on first run, clear data discontinuity error, as it will always be set:
            flags &= ~_ole32.AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY
            self._is_first_frame = False
        if flags & _ole32.AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
            warnings.warn("data discontinuity in recording", SoundcardRuntimeWarning)
        # ignore _ole32.AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR, since we don't use
        # time stamps.
        if nframes > 0:
            self._capture_release(nframes)
            return chunk
        else:
            return numpy.zeros([0], dtype='float32')

    def record(self, numframes=None):
        """Record a block of audio data.

        The data will be returned as a frames × channels float32 numpy array.
        This function will wait until numframes frames have been recorded.
        If numframes is given, it will return exactly `numframes` frames,
        and buffer the rest for later.

        If numframes is None, it will return whatever the audio backend
        has available right now.
        Use this if latency must be kept to a minimum, but be aware that
        block sizes can change at the whims of the audio backend.

        If using `record` with `numframes=None` after using `record` with a
        required `numframes`, the last buffered frame will be returned along
        with the new recorded block.
        (If you want to empty the last buffered frame instead, use `flush`)

        """

        if numframes is None:
            recorded_data = [self._pending_chunk, self._record_chunk()]
            self._pending_chunk = numpy.zeros([0], dtype='float32')
        else:
            recorded_frames = len(self._pending_chunk)
            recorded_data = [self._pending_chunk]
            self._pending_chunk = numpy.zeros([0], dtype='float32')
            required_frames = numframes*len(set(self.channelmap))
            while recorded_frames < required_frames:
                chunk = self._record_chunk()
                if len(chunk) == 0:
                    # no data forthcoming: return zeros
                    chunk = numpy.zeros(required_frames-recorded_frames,
                                        dtype='float32')
                recorded_data.append(chunk)
                recorded_frames += len(chunk)
            if recorded_frames > required_frames:
                to_split = -int(recorded_frames-required_frames)
                recorded_data[-1], self._pending_chunk = numpy.split(recorded_data[-1], [to_split])
                # 兜底：遗留缓冲最多留 1 秒。上游这里会把"多出来的全部"留下，
                # 一旦某次块异常大（见 _record_chunk 的静音补零），这个缓冲就永远
                # 释放不掉，且每读一块都要 concatenate 一次 → 内存翻倍、CPU 卡死。
                cap = int(self.samplerate * len(set(self.channelmap)))
                if len(self._pending_chunk) > cap:
                    self._pending_chunk = self._pending_chunk[-cap:]

        data = numpy.reshape(numpy.concatenate(recorded_data), [-1, len(set(self.channelmap))])
        return data[:, self.channelmap]

    def flush(self):
        """Return the last pending chunk
        After using the record method, this will return the last incomplete
        chunk and delete it.

        """
        last_chunk = numpy.reshape(self._pending_chunk, [-1, len(set(self.channelmap))])
        self._pending_chunk = numpy.zeros([0], dtype='float32')
        return last_chunk
