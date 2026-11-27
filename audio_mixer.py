import collections
import logging
import socket
import threading
import time
import numpy as np
import pyaudiowpatch as pa

logger = logging.getLogger("ScreenCamLite.audio")


TARGET_RATE = 48000
TARGET_CHANNELS = 2
CHUNK_FRAMES = 960  # 20ms @ 48kHz
CHUNK_BYTES = CHUNK_FRAMES * TARGET_CHANNELS * 2


def _resample_to_48k_stereo(raw_bytes: bytes, src_rate: int, src_channels: int) -> np.ndarray:
    arr = np.frombuffer(raw_bytes, dtype=np.int16)
    if arr.size == 0:
        return np.zeros((0, 2), dtype=np.float32)

    if src_channels == 1:
        arr = np.repeat(arr[:, None], 2, axis=1)
    elif src_channels >= 2:
        arr = arr.reshape(-1, src_channels)[:, :2]

    f = arr.astype(np.float32)
    if src_rate == TARGET_RATE:
        return f

    n_in = f.shape[0]
    n_out = max(1, int(round(n_in * TARGET_RATE / src_rate)))
    x_old = np.linspace(0.0, 1.0, n_in, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    out = np.empty((n_out, 2), dtype=np.float32)
    out[:, 0] = np.interp(x_new, x_old, f[:, 0])
    out[:, 1] = np.interp(x_new, x_old, f[:, 1])
    return out


class AudioMixer:
    def __init__(self):
        self._pa = pa.PyAudio()
        self._lock = threading.Lock()
        self._running = True

        self.mic_enabled = True
        self.sys_enabled = True
        self.mic_gain = 1.0
        self.sys_gain = 1.0
        self.mic_level = 0.0
        self.sys_level = 0.0

        self._mic_buf = collections.deque(maxlen=8)  # ~160ms cap
        self._sys_buf = collections.deque(maxlen=8)

        self._mic_stream = None
        self._sys_stream = None
        self._mic_rate = TARGET_RATE
        self._mic_ch = TARGET_CHANNELS
        self._sys_rate = TARGET_RATE
        self._sys_ch = TARGET_CHANNELS

        self.selected_mic_index = None
        self.selected_sys_index = None

        self._tcp_srv = None
        self._tcp_conn = None
        self._tcp_streaming = False

        self._mixer_thread = threading.Thread(target=self._mix_loop, daemon=True)
        self._mixer_thread.start()

    def list_devices(self):
        mics = []
        loopbacks = []
        seen_mics = set()
        seen_sys = set()

        wasapi_idx = None
        try:
            wasapi_idx = self._pa.get_host_api_info_by_type(pa.paWASAPI)["index"]
        except Exception:
            pass

        for i in range(self._pa.get_device_count()):
            try:
                info = self._pa.get_device_info_by_index(i)
            except Exception:
                continue
            name = info.get("name", f"Device {i}")
            is_loop = bool(info.get("isLoopbackDevice", False))
            in_ch = int(info.get("maxInputChannels", 0))
            if is_loop and in_ch > 0:
                if name not in seen_sys:
                    seen_sys.add(name)
                    loopbacks.append({"index": i, "name": name})
            elif not is_loop and in_ch > 0:
                # Prefer WASAPI or MME unique names
                if wasapi_idx is not None and info.get("hostApi") == wasapi_idx:
                    if name not in seen_mics:
                        seen_mics.add(name)
                        mics.append({"index": i, "name": name})

        if not mics:
            for i in range(self._pa.get_device_count()):
                try:
                    info = self._pa.get_device_info_by_index(i)
                    if not info.get("isLoopbackDevice") and int(info.get("maxInputChannels", 0)) > 0:
                        name = info.get("name", f"Mic {i}")
                        if name not in seen_mics:
                            seen_mics.add(name)
                            mics.append({"index": i, "name": name})
                except Exception:
                    pass

        return {"mics": mics, "loopbacks": loopbacks}

    def open_streams(self, mic_index=None, sys_index=None):
        self.close_streams()
        if mic_index is None:
            try:
                mic_index = self._pa.get_default_input_device_info()["index"]
            except Exception:
                mic_index = None
        if sys_index is None:
            try:
                sys_index = self._pa.get_default_wasapi_loopback()["index"]
            except Exception:
                sys_index = None

        self.selected_mic_index = mic_index
        self.selected_sys_index = sys_index

        if mic_index is not None:
            try:
                info = self._pa.get_device_info_by_index(mic_index)
                self._mic_rate = int(info.get("defaultSampleRate", TARGET_RATE))
                self._mic_ch = min(2, max(1, int(info.get("maxInputChannels", 1))))
                frames_per_buf = max(256, int(self._mic_rate * 0.02))
                self._mic_stream = self._pa.open(
                    format=pa.paInt16,
                    channels=self._mic_ch,
                    rate=self._mic_rate,
                    input=True,
                    input_device_index=mic_index,
                    frames_per_buffer=frames_per_buf,
                    stream_callback=self._on_mic_data,
                )
            except Exception:
                logger.exception("Failed to open microphone stream (index=%s)", mic_index)
                self._mic_stream = None

        if sys_index is not None:
            try:
                info = self._pa.get_device_info_by_index(sys_index)
                self._sys_rate = int(info.get("defaultSampleRate", TARGET_RATE))
                self._sys_ch = min(2, max(1, int(info.get("maxInputChannels", 2))))
                frames_per_buf = max(256, int(self._sys_rate * 0.02))
                self._sys_stream = self._pa.open(
                    format=pa.paInt16,
                    channels=self._sys_ch,
                    rate=self._sys_rate,
                    input=True,
                    input_device_index=sys_index,
                    frames_per_buffer=frames_per_buf,
                    stream_callback=self._on_sys_data,
                )
            except Exception:
                logger.exception("Failed to open WASAPI loopback stream (index=%s)", sys_index)
                self._sys_stream = None

    def _on_mic_data(self, in_data, _frame_count, _time_info, _status):
        samples = _resample_to_48k_stereo(in_data, self._mic_rate, self._mic_ch)
        if samples.shape[0] > 0:
            peak = float(np.max(np.abs(samples))) / 32768.0
            with self._lock:
                if self.mic_enabled:
                    self._mic_buf.append(samples)
                    self.mic_level = min(1.0, peak * self.mic_gain * 1.4)
                else:
                    self.mic_level = 0.0
        return (None, pa.paContinue)

    def _on_sys_data(self, in_data, _frame_count, _time_info, _status):
        samples = _resample_to_48k_stereo(in_data, self._sys_rate, self._sys_ch)
        if samples.shape[0] > 0:
            peak = float(np.max(np.abs(samples))) / 32768.0
            with self._lock:
                if self.sys_enabled:
                    self._sys_buf.append(samples)
                    self.sys_level = min(1.0, peak * self.sys_gain * 1.4)
                else:
                    self.sys_level = 0.0
        return (None, pa.paContinue)

    def _drain_chunk(self, buf: collections.deque, need: int) -> np.ndarray:
        if not buf:
            return np.zeros((need, 2), dtype=np.float32)
        collected = []
        got = 0
        while buf and got < need:
            head = buf.popleft()
            if got + head.shape[0] <= need:
                collected.append(head)
                got += head.shape[0]
            else:
                take = need - got
                collected.append(head[:take])
                buf.appendleft(head[take:])
                got += take
        out = np.vstack(collected) if len(collected) > 1 else collected[0]
        if out.shape[0] < need:
            pad = np.zeros((need - out.shape[0], 2), dtype=np.float32)
            out = np.vstack([out, pad])
        return out

    def start_tcp_server(self) -> int:
        self.stop_tcp_server()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(5.0)
        port = srv.getsockname()[1]
        self._tcp_srv = srv
        self._tcp_conn = None
        self._tcp_streaming = True

        def accept_loop():
            try:
                conn, _ = srv.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(1.0)
                with self._lock:
                    self._mic_buf.clear()
                    self._sys_buf.clear()
                    self._tcp_conn = conn
            except Exception:
                pass

        threading.Thread(target=accept_loop, daemon=True).start()
        return port

    def stop_tcp_server(self):
        self._tcp_streaming = False
        with self._lock:
            conn = self._tcp_conn
            self._tcp_conn = None
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        srv = self._tcp_srv
        self._tcp_srv = None
        if srv:
            try:
                srv.close()
            except Exception:
                pass

    def _mix_loop(self):
        next_tick = time.perf_counter()
        while self._running:
            next_tick += 0.02
            with self._lock:
                conn = self._tcp_conn if self._tcp_streaming else None

            if conn is not None:
                with self._lock:
                    mic_arr = self._drain_chunk(self._mic_buf, CHUNK_FRAMES) if self.mic_enabled else np.zeros((CHUNK_FRAMES, 2), dtype=np.float32)
                    sys_arr = self._drain_chunk(self._sys_buf, CHUNK_FRAMES) if self.sys_enabled else np.zeros((CHUNK_FRAMES, 2), dtype=np.float32)
                    mg = self.mic_gain if self.mic_enabled else 0.0
                    sg = self.sys_gain if self.sys_enabled else 0.0

                mixed = np.clip(mic_arr * mg + sys_arr * sg, -32768.0, 32767.0).astype(np.int16)
                try:
                    conn.sendall(mixed.tobytes())
                except Exception:
                    with self._lock:
                        if self._tcp_conn is conn:
                            self._tcp_conn = None
            else:
                # Decay VU meters smoothly when no new audio arrives
                with self._lock:
                    self.mic_level *= 0.8
                    self.sys_level *= 0.8

            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()

    def close_streams(self):
        for s in (self._mic_stream, self._sys_stream):
            if s is not None:
                try:
                    s.stop_stream()
                    s.close()
                except Exception:
                    pass
        self._mic_stream = None
        self._sys_stream = None

    def shutdown(self):
        self._running = False
        self.stop_tcp_server()
        self.close_streams()
        try:
            self._pa.terminate()
        except Exception:
            pass
