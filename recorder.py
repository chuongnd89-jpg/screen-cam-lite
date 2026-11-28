import ctypes
from ctypes import wintypes
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from PIL import Image, ImageDraw
from devices import get_ffmpeg_path

logger = logging.getLogger("ScreenCamLite.recorder")


def _create_kill_on_close_job():
    """Creates a Windows Job Object that kills assigned child processes when parent exits."""
    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOp", "WriteOp", "OtherOp", "ReadXfer", "WriteXfer", "OtherXfer")]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        logger.debug("Job object creation skipped", exc_info=True)
        return None


def _assign_proc_to_job(job, pid: int):
    if not job or not pid:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(0x1F0FFF, False, pid)
        if h:
            kernel32.AssignProcessToJobObject(job, h)
            kernel32.CloseHandle(h)
    except Exception:
        logger.debug("AssignProcessToJobObject failed for pid=%s", pid, exc_info=True)


QUALITY_PROFILES = {
    "eco": {"label": "Tiết kiệm (Dung lượng nhỏ)", "qsv_q": "29", "x264_crf": "28", "mf_kbps": "2000k"},
    "balanced": {"label": "Cân bằng (Khuyên dùng)", "qsv_q": "25", "x264_crf": "24", "mf_kbps": "3500k"},
    "high": {"label": "Chất lượng cao", "qsv_q": "21", "x264_crf": "20", "mf_kbps": "6000k"},
}

LIVE_PREV_W = 480
LIVE_PREV_H = 270
LIVE_PREV_FPS = 6


def _even(v: int) -> int:
    return max(2, (int(v) // 2) * 2)


def build_pip_mask_png(path: str, w: int, h: int, shape: str, border: bool):
    """Generates a 4x supersampled alpha mask PNG (and optional border PNG) once before recording."""
    scale = 4
    sw, sh = w * scale, h * scale
    mask = Image.new("L", (sw, sh), 0)
    draw = ImageDraw.Draw(mask)

    if shape == "circle":
        draw.ellipse((0, 0, sw - 1, sh - 1), fill=255)
    elif shape == "rounded":
        r = max(8, int(min(sw, sh) * 0.1))
        draw.rounded_rectangle((0, 0, sw - 1, sh - 1), radius=r, fill=255)
    else:
        draw.rectangle((0, 0, sw - 1, sh - 1), fill=255)

    mask = mask.resize((w, h), Image.Resampling.LANCZOS)
    rgba = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    rgba.putalpha(mask)
    rgba.save(path)

    border_path = None
    if border:
        bimg = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(bimg)
        bw = max(8, int(min(sw, sh) * 0.02))
        col = (129, 140, 248, 235)
        if shape == "circle":
            bdraw.ellipse((bw // 2, bw // 2, sw - 1 - bw // 2, sh - 1 - bw // 2), outline=col, width=bw)
        elif shape == "rounded":
            r = max(8, int(min(sw, sh) * 0.1))
            bdraw.rounded_rectangle((bw // 2, bw // 2, sw - 1 - bw // 2, sh - 1 - bw // 2), radius=r, outline=col, width=bw)
        else:
            bdraw.rectangle((bw // 2, bw // 2, sw - 1 - bw // 2, sh - 1 - bw // 2), outline=col, width=bw)
        border_path = path.replace(".png", "_border.png")
        bimg.resize((w, h), Image.Resampling.LANCZOS).save(border_path)

    return border_path


class RecorderSession:
    def __init__(self, audio_mixer, encoder_info: dict):
        self.audio_mixer = audio_mixer
        self.encoder_info = encoder_info
        self.ffmpeg = get_ffmpeg_path()

        self.is_recording = False
        self.is_paused = False
        self.part_index = 1
        self._op_lock = threading.Lock()
        self._proc = None
        self._stderr_lines = []
        self._temp_dir = None
        self._segments = []  # segments within current part
        self._final_paths = ()
        self._config = None

        self._live_lock = threading.Lock()
        self._latest_live_img = None
        self._last_proc_stop_at = 0.0
        self._job_handle = _create_kill_on_close_job()

    def pop_live_frame(self):
        with self._live_lock:
            img = self._latest_live_img
            self._latest_live_img = None
            return img

    def _encoder_args(self, quality_key: str) -> list:
        prof = QUALITY_PROFILES.get(quality_key, QUALITY_PROFILES["balanced"])
        codec = self.encoder_info["codec"]
        if codec == "h264_qsv":
            return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", prof["qsv_q"], "-pix_fmt", "nv12"]
        if codec == "h264_mf":
            return ["-c:v", "h264_mf", "-b:v", prof["mf_kbps"], "-pix_fmt", "nv12"]
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", prof["x264_crf"], "-threads", "2", "-pix_fmt", "yuv420p"]

    def _screen_input_args(self, cfg: dict) -> list:
        src = cfg["source"]
        fps = str(cfg["fps"])
        if src["kind"] == "window":
            return ["-thread_queue_size", "512", "-f", "gdigrab", "-framerate", fps, "-draw_mouse", "1", "-i", f"title={src['title']}"]
        return [
            "-thread_queue_size",
            "512",
            "-f",
            "gdigrab",
            "-framerate",
            fps,
            "-draw_mouse",
            "1",
            "-offset_x",
            str(src["left"]),
            "-offset_y",
            str(src["top"]),
            "-video_size",
            f"{src['width']}x{src['height']}",
            "-i",
            "desktop",
        ]

    def _webcam_input_args(self, cfg: dict) -> list:
        cam_fps = str(min(30, int(cfg["fps"])))
        cam_name = cfg["camera_name"]
        return [
            "-thread_queue_size",
            "512",
            "-f",
            "dshow",
            "-rtbufsize",
            "64M",
            "-framerate",
            cam_fps,
            "-i",
            f"video={cam_name}",
        ]

    def _target_dims(self, cfg: dict) -> tuple:
        src = cfg["source"]
        nat_w = src.get("width", 1920)
        nat_h = src.get("height", 1080)
        aspect = nat_w / max(1, nat_h)
        res = cfg["resolution"]

        if cfg["mode"] in ("webcam", "pip-screen"):
            if res == "720p":
                return (1280, 720)
            return (1920, 1080) if res == "1080p" else (1280, 720)

        if cfg["mode"] == "stack-tb":
            sa = cfg["stack_aspect"]
            if sa == "9:16":
                return (720, 1280) if res == "720p" else (1080, 1920)
            if sa == "16:9":
                return (1280, 720) if res == "720p" else (1920, 1080)
            w = 960 if res == "720p" else 1280
            top_h = _even(w / aspect)
            bot_h = _even(w / (16 / 9))
            return (w, top_h + bot_h + 4)

        if res == "720p":
            h = min(720, nat_h) if nat_h < 720 else 720
            return (_even(h * aspect), _even(h))
        if res == "1080p":
            h = min(1080, nat_h)
            return (_even(h * aspect), _even(h))
        return (_even(nat_w), _even(nat_h))

    def _pip_geometry(self, cfg: dict, canvas_w: int, canvas_h: int) -> tuple:
        pip_w = _even(canvas_w * cfg["pip_size_pct"] / 100.0)
        shape = cfg["pip_shape"]
        pip_h = pip_w if shape in ("circle", "square") else _even(pip_w * 9 / 16)
        margin = max(16, int(canvas_w * 0.018))
        pos = cfg["pip_pos"]
        if pos == "tl":
            px, py = margin, margin
        elif pos == "tr":
            px, py = canvas_w - pip_w - margin, margin
        elif pos == "bl":
            px, py = margin, canvas_h - pip_h - margin
        else:
            px, py = canvas_w - pip_w - margin, canvas_h - pip_h - margin
        return (pip_w, pip_h, px, py)

    def _preview_pipe_args(self) -> list:
        return ["-map", "[vprev]", "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]

    def _build_command(self, cfg: dict, audio_port: int, seg_paths: tuple) -> list:
        mode = cfg["mode"]
        live_prev = bool(cfg.get("live_while_recording", False))
        enc_args = self._encoder_args(cfg["quality"])
        aud_args = ["-c:a", "aac", "-b:a", "128k"]
        audio_in = [
            "-thread_queue_size", "512",
            "-f", "s16le", "-ar", "48000", "-ac", "2",
            "-i", f"tcp://127.0.0.1:{audio_port}",
        ]

        W, H = self._target_dims(cfg)
        cam_flip = "hflip," if cfg["mirror_cam"] else ""
        prev_branch = (
            f"split=2[venc][vp_in];[vp_in]fps={LIVE_PREV_FPS},"
            f"scale={LIVE_PREV_W}:{LIVE_PREV_H}:force_original_aspect_ratio=decrease,"
            f"pad={LIVE_PREV_W}:{LIVE_PREV_H}:(ow-iw)/2:(oh-ih)/2:color=#090b12,format=rgb24[vprev]"
        )

        if mode == "screen":
            base = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=#090b12,setsar=1"
            if live_prev:
                fc = f"{base},{prev_branch}"
                return [
                    self.ffmpeg, "-hide_banner", "-y",
                    *self._screen_input_args(cfg),
                    *audio_in,
                    "-filter_complex", fc,
                    "-map", "[venc]", "-map", "1:a", *enc_args, *aud_args, "-movflags", "+faststart", seg_paths[0],
                    *self._preview_pipe_args(),
                ]
            return [
                self.ffmpeg, "-hide_banner", "-y",
                *self._screen_input_args(cfg),
                *audio_in,
                "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=#090b12,setsar=1",
                *enc_args, *aud_args,
                "-movflags", "+faststart",
                seg_paths[0],
            ]

        if mode == "webcam":
            base = f"[0:v]{cam_flip}scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
            if live_prev:
                fc = f"{base},{prev_branch}"
                return [
                    self.ffmpeg, "-hide_banner", "-y",
                    *self._webcam_input_args(cfg),
                    *audio_in,
                    "-filter_complex", fc,
                    "-map", "[venc]", "-map", "1:a", *enc_args, *aud_args, "-movflags", "+faststart", seg_paths[0],
                    *self._preview_pipe_args(),
                ]
            return [
                self.ffmpeg, "-hide_banner", "-y",
                *self._webcam_input_args(cfg),
                *audio_in,
                "-vf", f"{cam_flip}scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1",
                *enc_args, *aud_args,
                "-movflags", "+faststart",
                seg_paths[0],
            ]

        if mode == "dual":
            if live_prev:
                hw = LIVE_PREV_W // 2 - 2
                fc = (
                    f"[0:v]setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=#090b12,setsar=1,split=2[vs][vs_p];"
                    f"[1:v]setpts=PTS-STARTPTS,{cam_flip}setsar=1,split=2[vc][vc_p];"
                    f"[vs_p]fps={LIVE_PREV_FPS},scale={hw}:{LIVE_PREV_H}:force_original_aspect_ratio=decrease,pad={hw + 2}:{LIVE_PREV_H}:(ow-iw)/2:(oh-ih)/2:color=#090b12[p1];"
                    f"[vc_p]fps={LIVE_PREV_FPS},scale={hw}:{LIVE_PREV_H}:force_original_aspect_ratio=decrease,pad={hw + 2}:{LIVE_PREV_H}:(ow-iw)/2:(oh-ih)/2:color=#090b12[p2];"
                    f"[p1][p2]hstack=inputs=2,format=rgb24[vprev];"
                    f"[2:a]asplit=2[a1][a2]"
                )
                extra = self._preview_pipe_args()
            else:
                fc = (
                    f"[0:v]setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=#090b12,setsar=1[vs];"
                    f"[1:v]setpts=PTS-STARTPTS,{cam_flip}setsar=1[vc];"
                    f"[2:a]asplit=2[a1][a2]"
                )
                extra = []
            return [
                self.ffmpeg, "-hide_banner", "-y",
                *self._screen_input_args(cfg),
                *self._webcam_input_args(cfg),
                *audio_in,
                "-filter_complex", fc,
                "-map", "[vs]", "-map", "[a1]", *enc_args, *aud_args, "-movflags", "+faststart", seg_paths[0],
                "-map", "[vc]", "-map", "[a2]", *enc_args, *aud_args, "-movflags", "+faststart", seg_paths[1],
                *extra,
            ]

        if mode in ("pip", "pip-screen"):
            pw, ph, px, py = self._pip_geometry(cfg, W, H)
            mask_path = os.path.join(self._temp_dir, "pip_mask.png")
            border_path = build_pip_mask_png(mask_path, pw, ph, cfg["pip_shape"], cfg["cam_border"])

            inputs = [*self._screen_input_args(cfg), *self._webcam_input_args(cfg), *audio_in]
            inputs += ["-loop", "1", "-framerate", str(cfg["fps"]), "-i", mask_path]
            if border_path:
                inputs += ["-loop", "1", "-framerate", str(cfg["fps"]), "-i", border_path]

            if mode == "pip":
                bg_filter = f"[0:v]setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=#090b12,setsar=1[bg]"
                fg_filter = f"[1:v]setpts=PTS-STARTPTS,{cam_flip}fps={cfg['fps']},scale={pw}:{ph}:force_original_aspect_ratio=increase,crop={pw}:{ph},format=rgba[fg_raw]"
            else:
                bg_filter = f"[1:v]setpts=PTS-STARTPTS,{cam_flip}fps={cfg['fps']},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[bg]"
                fg_filter = f"[0:v]setpts=PTS-STARTPTS,scale={pw}:{ph}:force_original_aspect_ratio=decrease,pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:color=#0f172a,format=rgba[fg_raw]"

            fg_masked = "[fg_raw][3:v]alphamerge[fg]"
            ov = f"[bg][fg]overlay=x={px}:y={py}:shortest=1"
            out_label = "[venc]" if live_prev else "[vout]"
            if border_path:
                comp = f"{bg_filter};{fg_filter};{fg_masked};{ov}[comp];[comp][4:v]overlay=x={px}:y={py}:shortest=1"
            else:
                comp = f"{bg_filter};{fg_filter};{fg_masked};{ov}"

            if live_prev:
                fc = f"{comp},{prev_branch}"
                extra = self._preview_pipe_args()
            else:
                fc = f"{comp}{out_label}"
                extra = []

            return [
                self.ffmpeg, "-hide_banner", "-y",
                *inputs,
                "-filter_complex", fc,
                "-map", out_label, "-map", "2:a",
                *enc_args, *aud_args,
                "-movflags", "+faststart",
                seg_paths[0],
                *extra,
            ]

        # mode == "stack-tb"
        gap = 4
        if cfg["stack_aspect"] == "auto":
            src_aspect = cfg["source"].get("width", 1920) / max(1, cfg["source"].get("height", 1080))
            scr_h = _even(W / src_aspect)
            cam_h = _even(W / (16 / 9))
        else:
            scr_h = _even((H - gap) // 2)
            cam_h = _even(H - gap - scr_h)

        scr_box = f"[0:v]setpts=PTS-STARTPTS,scale={W}:{scr_h}:force_original_aspect_ratio=decrease,pad={W}:{scr_h}:(ow-iw)/2:(oh-ih)/2:color=#090b12,setsar=1[vscr]"
        cam_fit = "decrease" if cfg["stack_aspect"] == "auto" else "increase"
        if cam_fit == "decrease":
            cam_box = f"[1:v]setpts=PTS-STARTPTS,{cam_flip}fps={cfg['fps']},scale={W}:{cam_h}:force_original_aspect_ratio=decrease,pad={W}:{cam_h}:(ow-iw)/2:(oh-ih)/2:color=#090b12,setsar=1[vcam]"
        else:
            cam_box = f"[1:v]setpts=PTS-STARTPTS,{cam_flip}fps={cfg['fps']},scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h},setsar=1[vcam]"

        if cfg["stack_order"] == "screen-top":
            vstack = f"{scr_box};{cam_box};[vscr]pad={W}:{scr_h + gap}:0:0:color=#1e293b[vtop];[vtop][vcam]vstack=inputs=2"
        else:
            vstack = f"{scr_box};{cam_box};[vcam]pad={W}:{cam_h + gap}:0:0:color=#1e293b[vtop];[vtop][vscr]vstack=inputs=2"

        out_label = "[venc]" if live_prev else "[vout]"
        if live_prev:
            fc = f"{vstack},{prev_branch}"
            extra = self._preview_pipe_args()
        else:
            fc = f"{vstack}{out_label}"
            extra = []

        return [
            self.ffmpeg, "-hide_banner", "-y",
            *self._screen_input_args(cfg),
            *self._webcam_input_args(cfg),
            *audio_in,
            "-filter_complex", fc,
            "-map", out_label, "-map", "2:a",
            *enc_args, *aud_args,
            "-movflags", "+faststart",
            seg_paths[0],
            *extra,
        ]

    def _make_part_paths(self, part_idx: int) -> tuple:
        cfg = self._config
        stamp = datetime.now().strftime("ScreenCam_%Y-%m-%d_%H-%M-%S")
        part_tag = f"_p{part_idx:02d}" if cfg.get("split_minutes", 0) > 0 else ""
        suffix_map = {
            "pip": "_pip",
            "pip-screen": "_cam-pip",
            "stack-tb": "_tren-duoi",
            "screen": "_man-hinh",
            "webcam": "_webcam",
        }
        if cfg["mode"] == "dual":
            return (
                os.path.join(cfg["output_dir"], f"{stamp}{part_tag}_man-hinh.mp4"),
                os.path.join(cfg["output_dir"], f"{stamp}{part_tag}_webcam.mp4"),
            )
        return (
            os.path.join(cfg["output_dir"], f"{stamp}{part_tag}{suffix_map.get(cfg['mode'], '')}.mp4"),
        )

    def _spawn_segment(self):
        if self._config.get("mode") != "screen" and self._last_proc_stop_at > 0:
            elapsed_since_stop = time.monotonic() - self._last_proc_stop_at
            if elapsed_since_stop < 0.45:
                time.sleep(0.45 - elapsed_since_stop)

        seg_idx = len(self._segments) + 1
        if self._config["mode"] == "dual":
            seg_paths = (
                os.path.join(self._temp_dir, f"p{self.part_index}_seg_{seg_idx:03d}_screen.mp4"),
                os.path.join(self._temp_dir, f"p{self.part_index}_seg_{seg_idx:03d}_cam.mp4"),
            )
        else:
            seg_paths = (os.path.join(self._temp_dir, f"p{self.part_index}_seg_{seg_idx:03d}.mp4"),)

        port = self.audio_mixer.start_tcp_server()
        cmd = self._build_command(self._config, port, seg_paths)
        logger.info("Spawning FFmpeg (mode=%s, part=%d): %s", self._config["mode"], self.part_index, " ".join(cmd))
        self._stderr_lines = []
        live_prev = bool(self._config.get("live_while_recording", False))
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if live_prev else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        self._proc = proc
        _assign_proc_to_job(self._job_handle, proc.pid)
        self._segments.append(seg_paths)

        def read_err():
            try:
                for raw in proc.stderr:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        self._stderr_lines.append(line)
                        if len(self._stderr_lines) > 60:
                            self._stderr_lines.pop(0)
            except Exception:
                logger.exception("Error reading FFmpeg stderr")

        threading.Thread(target=read_err, daemon=True).start()

        if live_prev and proc.stdout is not None:
            frame_bytes = LIVE_PREV_W * LIVE_PREV_H * 3

            def read_preview():
                try:
                    while True:
                        buf = proc.stdout.read(frame_bytes)
                        if not buf or len(buf) < frame_bytes:
                            break
                        img = Image.frombytes("RGB", (LIVE_PREV_W, LIVE_PREV_H), buf)
                        with self._live_lock:
                            self._latest_live_img = img
                except Exception:
                    pass

            threading.Thread(target=read_preview, daemon=True).start()

    def start(self, config: dict):
        with self._op_lock:
            if self.is_recording:
                return
            self._config = config
            os.makedirs(config["output_dir"], exist_ok=True)
            self._temp_dir = tempfile.mkdtemp(prefix=".tmp_sc_", dir=config["output_dir"])
            self._segments = []
            self.part_index = 1
            self._final_paths = self._make_part_paths(self.part_index)

            with self._live_lock:
                self._latest_live_img = None

            self.is_recording = True
            self.is_paused = False
            self._spawn_segment()

    def _stop_active_proc(self):
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.write(b"q\n")
                proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
        self.audio_mixer.stop_tcp_server()
        self._last_proc_stop_at = time.monotonic()
        return proc.returncode if proc is not None else 0

    def _finalize_current_part_files(self) -> list:
        saved = []
        n_tracks = len(self._final_paths)
        for idx in range(n_tracks):
            valid_segs = [
                seg[idx]
                for seg in self._segments
                if os.path.exists(seg[idx]) and os.path.getsize(seg[idx]) > 0
            ]
            if not valid_segs:
                continue
            out_path = self._final_paths[idx]
            if len(valid_segs) == 1:
                try:
                    os.replace(valid_segs[0], out_path)
                except OSError:
                    shutil.move(valid_segs[0], out_path)
            else:
                list_file = os.path.join(self._temp_dir, f"concat_p{self.part_index}_{idx}.txt")
                with open(list_file, "w", encoding="utf-8") as f:
                    for sp in valid_segs:
                        safe = sp.replace("'", "'\\''")
                        f.write(f"file '{safe}'\n")
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                concat_res = subprocess.run(
                    [
                        self.ffmpeg, "-hide_banner", "-y",
                        "-f", "concat", "-safe", "0",
                        "-i", list_file, "-c", "copy", "-movflags", "+faststart", out_path,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    creationflags=creationflags,
                )
                if concat_res.returncode != 0:
                    logger.error("Segment concat failed (rc=%s): %s", concat_res.returncode, concat_res.stderr[-500:])
                for sp in valid_segs:
                    try:
                        os.remove(sp)
                    except OSError:
                        pass
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                saved.append(out_path)
        return saved

    def split_part(self) -> list:
        """Stops current segment cleanly, saves completed part, and immediately starts next part."""
        with self._op_lock:
            if not self.is_recording or self.is_paused:
                return []
            self._stop_active_proc()
            saved = self._finalize_current_part_files()
            self.part_index += 1
            self._segments = []
            self._final_paths = self._make_part_paths(self.part_index)
            self._spawn_segment()
            return saved

    def pause(self):
        with self._op_lock:
            if not self.is_recording or self.is_paused:
                return
            self.is_paused = True
            self._stop_active_proc()

    def resume(self):
        with self._op_lock:
            if not self.is_recording or not self.is_paused:
                return
            self.is_paused = False
            self._spawn_segment()

    def active_size_bytes(self) -> int:
        total = 0
        for seg in list(self._segments):
            for p in seg:
                if os.path.exists(p):
                    try:
                        total += os.path.getsize(p)
                    except OSError:
                        pass
        return total

    def check_alive(self) -> tuple:
        if not self._op_lock.acquire(blocking=False):
            return (True, "")
        try:
            if not self.is_recording or self.is_paused:
                return (True, "")
            if self._proc and self._proc.poll() is not None:
                rc = self._proc.returncode
                err = "\n".join(self._stderr_lines[-12:])
                logger.error("FFmpeg exited unexpectedly (rc=%s):\n%s", rc, "\n".join(self._stderr_lines))
                return (False, err)
            return (True, "")
        finally:
            self._op_lock.release()

    def stop(self) -> list:
        with self._op_lock:
            if not self.is_recording:
                return []
            if not self.is_paused:
                self._stop_active_proc()
            self.is_recording = False
            self.is_paused = False

            saved = []
            try:
                saved = self._finalize_current_part_files()
            finally:
                with self._live_lock:
                    self._latest_live_img = None
                if self._temp_dir and os.path.exists(self._temp_dir):
                    for root, _dirs, files in os.walk(self._temp_dir, topdown=False):
                        for fn in files:
                            try:
                                os.remove(os.path.join(root, fn))
                            except OSError:
                                pass
                        try:
                            os.rmdir(root)
                        except OSError:
                            pass
                    self._temp_dir = None

            if not saved and self._stderr_lines:
                raise RuntimeError("\n".join(self._stderr_lines[-6:]))
            return saved
