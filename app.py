import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import logging
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
import cv2
import mss
from PIL import Image, ImageDraw, ImageTk

from audio_mixer import AudioMixer
from devices import (
    detect_best_encoder,
    get_asset_path,
    get_ffmpeg_path,
    get_window_rect,
    list_dshow_cameras,
    list_monitors,
    list_windows,
)
from logger_setup import get_log_file_path, setup_logging
from recorder import QUALITY_PROFILES, RecorderSession
from region_selector import FloatingRegionFrame, RegionSelector

logger = setup_logging()


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

APP_NAME = "ScreenCam Lite"
PREVIEW_W, PREVIEW_H = 640, 360

MODES = [
    ("pip", "Màn hình + Cam nhỏ", "Màn hình nền, Cam góc"),
    ("pip-screen", "Cam + Màn hình nhỏ", "Webcam nền, Màn hình góc"),
    ("stack-tb", "Xếp Trên & Dưới", "Tách 2 khung trên/dưới"),
    ("dual", "Song song 2 file", "Lưu riêng 2 file MP4"),
    ("screen", "Chỉ Màn hình", "Không dùng Webcam"),
    ("webcam", "Chỉ Webcam", "Chỉ quay từ Camera"),
]


def format_duration(sec: float) -> str:
    total = max(0, int(sec))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_bytes(n: int) -> str:
    if n < 1024 * 1024:
        return f"{n // 1024} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _fit_contain(img: Image.Image, tw: int, th: int, bg=(9, 11, 18)) -> Image.Image:
    iw, ih = img.size
    scale = min(tw / max(1, iw), th / max(1, ih))
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = img.resize((nw, nh), Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (tw, th), bg)
    canvas.paste(resized, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


def _fit_cover(img: Image.Image, tw: int, th: int) -> Image.Image:
    iw, ih = img.size
    scale = max(tw / max(1, iw), th / max(1, ih))
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = img.resize((nw, nh), Image.Resampling.BILINEAR)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return resized.crop((left, top, left + tw, top + th))


class ScreenCamLiteApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1120x740")
        self.minsize(960, 660)
        self.configure(fg_color="#0c0f17")

        icon_path = get_asset_path("icon.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass

        self.settings_path = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")),
            "ScreenCamLite",
            "settings.json",
        )
        self.output_dir = self._load_output_dir()
        os.makedirs(self.output_dir, exist_ok=True)

        # Core state
        self.mode = "pip"
        self.pip_pos = "br"
        self.pip_shape = "rounded"
        self.pip_size_pct = 24
        self.mirror_cam = True
        self.cam_border = True
        self.stack_order = "screen-top"
        self.stack_aspect = "auto"
        self.resolution = "1080p"
        self.fps = 30
        self.quality = "balanced"
        self.countdown_sec = 3
        self.auto_minimize = False
        self.preview_enabled = True
        self.live_while_recording = False
        self.split_minutes = 0.0

        self.sources = []
        self.selected_source = None
        self.custom_region = None
        self.floating_frame = None
        self._frame_was_visible = False
        self.cameras = []
        self.selected_cam_idx = 0
        self.audio_devices = {"mics": [], "loopbacks": []}
        self.encoder_info = {"codec": "libx264", "label": "Đang dò GPU..."}

        self.audio_mixer = AudioMixer()
        self.recorder = RecorderSession(self.audio_mixer, self.encoder_info)

        # Preview worker state
        self._preview_running = True
        self._cam_lock = threading.Lock()
        self._cam_cap = None
        self._cam_cap_idx = None
        self._cam_fail_logged = False
        self._cam_backoff_until = 0.0
        self._preview_photo = None
        self._latest_preview_pil = None
        self._preview_lock = threading.Lock()
        self._pending_hotkey = None

        # Recording timer
        self._rec_started_at = 0.0
        self._paused_accum = 0.0
        self._pause_started_at = 0.0
        self._part_start_elapsed = 0.0
        self._splitting = False
        self._countdown_active = False
        self._countdown_token = 0
        self._hotkey_tid = None

        self._build_ui()
        self.report_callback_exception = self._tk_exception_handler
        self._init_devices_async()
        self._start_hotkey_listener()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._ui_tick)

    def _tk_exception_handler(self, exc_type, exc_value, exc_tb):
        logger.error("Tkinter callback exception", exc_info=(exc_type, exc_value, exc_tb))
        messagebox.showerror(
            "Lỗi ứng dụng",
            f"Đã ghi chi tiết lỗi vào log:\n{get_log_file_path()}\n\n{exc_value}",
        )

    def _load_output_dir(self) -> str:
        default_dir = os.path.join(os.path.expanduser("~"), "Videos", "ScreenCam Lite")
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                return json.load(f).get("output_dir", default_dir)
        except Exception:
            return default_dir

    def _save_output_dir(self, path: str):
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump({"output_dir": path}, f, ensure_ascii=False, indent=2)

    # ---------- UI Layout ----------

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_rowconfigure(1, weight=1)

        # Top Header
        header = ctk.CTkFrame(self, height=48, fg_color="#111622", corner_radius=0)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        title_lbl = ctk.CTkLabel(
            header,
            text="  ScreenCam Lite",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#f1f5f9",
        )
        title_lbl.grid(row=0, column=0, padx=(10, 8), pady=8)

        self.encoder_badge = ctk.CTkLabel(
            header,
            text="Đang kiểm tra GPU...",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#1e293b",
            text_color="#93c5fd",
            corner_radius=8,
            padx=10,
            pady=3,
        )
        self.encoder_badge.grid(row=0, column=1, sticky="w", padx=6)

        self.status_badge = ctk.CTkLabel(
            header,
            text="● Sẵn sàng",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#142923",
            text_color="#34d399",
            corner_radius=12,
            padx=12,
            pady=4,
        )
        self.status_badge.grid(row=0, column=2, padx=12)

        btn_open_log = ctk.CTkButton(
            header,
            text="Xem Log",
            width=82,
            height=30,
            fg_color="#1e293b",
            hover_color="#334155",
            command=self._open_log_file,
        )
        btn_open_log.grid(row=0, column=3, padx=(0, 6))

        btn_open_dir = ctk.CTkButton(
            header,
            text="Mở thư mục",
            width=105,
            height=30,
            fg_color="#1e293b",
            hover_color="#334155",
            command=self._open_output_dir,
        )
        btn_open_dir.grid(row=0, column=4, padx=(0, 14))

        # Left Stage Column
        stage = ctk.CTkFrame(self, fg_color="transparent")
        stage.grid(row=1, column=0, sticky="nsew", padx=(14, 8), pady=12)
        stage.grid_rowconfigure(1, weight=1)
        stage.grid_columnconfigure(0, weight=1)

        # Source Picker Bar
        src_bar = ctk.CTkFrame(stage, fg_color="#131825", corner_radius=10, border_width=1, border_color="#232c40")
        src_bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        src_bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(src_bar, text="Nguồn:", text_color="#94a3b8", font=ctk.CTkFont(size=12)).grid(
            row=0, column=0, padx=(10, 4), pady=7
        )
        self.source_var = ctk.StringVar(value="Đang tải...")
        self.source_menu = ctk.CTkOptionMenu(
            src_bar,
            variable=self.source_var,
            values=["Đang tải..."],
            fg_color="#0d111c",
            button_color="#253047",
            command=self._on_source_selected,
        )
        self.source_menu.grid(row=0, column=1, sticky="ew", padx=4, pady=7)

        self.btn_select_region = ctk.CTkButton(
            src_bar,
            text="📐 Vẽ vùng",
            width=86,
            height=28,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._start_region_selection,
        )
        self.btn_select_region.grid(row=0, column=2, padx=4)

        self.btn_toggle_frame = ctk.CTkButton(
            src_bar,
            text="👁️ Khung viền",
            width=90,
            height=28,
            fg_color="#1e293b",
            hover_color="#334155",
            font=ctk.CTkFont(size=11),
            command=self._toggle_floating_frame,
        )
        self.btn_toggle_frame.grid(row=0, column=3, padx=4)

        self.preview_switch = ctk.CTkSwitch(
            src_bar,
            text="Xem trước",
            width=85,
            command=self._on_toggle_preview,
        )
        self.preview_switch.select()
        self.preview_switch.grid(row=0, column=4, padx=6)

        self.btn_refresh = ctk.CTkButton(
            src_bar,
            text="Làm mới",
            width=76,
            height=28,
            fg_color="#1e293b",
            hover_color="#334155",
            command=self._refresh_sources_and_devices,
        )
        self.btn_refresh.grid(row=0, column=5, padx=(4, 8))

        # Preview Viewport (native tk.Label + single persistent PhotoImage avoids CTkImage pyimage GC errors)
        viewport = ctk.CTkFrame(stage, fg_color="#07090f", corner_radius=14, border_width=1, border_color="#232c40")
        viewport.grid(row=1, column=0, sticky="nsew")
        viewport.grid_rowconfigure(0, weight=1)
        viewport.grid_columnconfigure(0, weight=1)

        self._tk_photo = ImageTk.PhotoImage(Image.new("RGB", (PREVIEW_W, PREVIEW_H), (9, 11, 18)))
        self.preview_label = tk.Label(
            viewport,
            text="Đang khởi tạo xem trước nhẹ...",
            font=("Segoe UI", 12),
            bg="#07090f",
            fg="#94a3b8",
            bd=0,
            highlightthickness=0,
        )
        self.preview_label.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        # Bottom Transport & Audio Mixer Bar
        transport = ctk.CTkFrame(stage, fg_color="#131825", corner_radius=12, border_width=1, border_color="#232c40")
        transport.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        transport.grid_columnconfigure(2, weight=1)

        # Mic Channel
        mic_box = ctk.CTkFrame(transport, fg_color="transparent")
        mic_box.grid(row=0, column=0, padx=(12, 10), pady=8)
        self.btn_mic = ctk.CTkButton(
            mic_box,
            text="🎤 Mic",
            width=74,
            height=30,
            fg_color="#312e81",
            hover_color="#4338ca",
            command=self._toggle_mic,
        )
        self.btn_mic.grid(row=0, column=0, rowspan=2, padx=(0, 8))
        self.mic_slider = ctk.CTkSlider(mic_box, from_=0, to=200, width=110, command=self._on_mic_gain)
        self.mic_slider.set(100)
        self.mic_slider.grid(row=0, column=1)
        self.mic_vu = ctk.CTkProgressBar(mic_box, width=110, height=6, progress_color="#10b981")
        self.mic_vu.set(0)
        self.mic_vu.grid(row=1, column=1, pady=(4, 0))

        # System Audio Channel
        sys_box = ctk.CTkFrame(transport, fg_color="transparent")
        sys_box.grid(row=0, column=1, padx=8, pady=8)
        self.btn_sys = ctk.CTkButton(
            sys_box,
            text="🔊 Máy",
            width=74,
            height=30,
            fg_color="#312e81",
            hover_color="#4338ca",
            command=self._toggle_sys,
        )
        self.btn_sys.grid(row=0, column=0, rowspan=2, padx=(0, 8))
        self.sys_slider = ctk.CTkSlider(sys_box, from_=0, to=200, width=110, command=self._on_sys_gain)
        self.sys_slider.set(100)
        self.sys_slider.grid(row=0, column=1)
        self.sys_vu = ctk.CTkProgressBar(sys_box, width=110, height=6, progress_color="#10b981")
        self.sys_vu.set(0)
        self.sys_vu.grid(row=1, column=1, pady=(4, 0))

        # Timer + Record Buttons
        rec_box = ctk.CTkFrame(transport, fg_color="transparent")
        rec_box.grid(row=0, column=3, padx=(8, 12), pady=8, sticky="e")

        timer_col = ctk.CTkFrame(rec_box, fg_color="transparent")
        timer_col.grid(row=0, column=0, padx=(0, 12))
        self.timer_lbl = ctk.CTkLabel(timer_col, text="00:00:00", font=ctk.CTkFont(size=16, weight="bold"))
        self.timer_lbl.pack(anchor="e")
        self.size_lbl = ctk.CTkLabel(timer_col, text="0.0 MB", font=ctk.CTkFont(size=11), text_color="#94a3b8")
        self.size_lbl.pack(anchor="e")

        self.btn_pause = ctk.CTkButton(
            rec_box,
            text="Tạm dừng",
            width=88,
            height=38,
            fg_color="#334155",
            hover_color="#475569",
            command=self.toggle_pause,
        )
        # Shown only while recording
        self.btn_record = ctk.CTkButton(
            rec_box,
            text="● Bắt đầu quay",
            width=142,
            height=40,
            corner_radius=20,
            fg_color="#e11d48",
            hover_color="#be123c",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.toggle_record,
        )
        self.btn_record.grid(row=0, column=2)

        # Right Sidebar (Tabs)
        sidebar = ctk.CTkTabview(self, width=340, fg_color="#131825", border_width=1, border_color="#232c40")
        sidebar.grid(row=1, column=1, sticky="nsew", padx=(4, 14), pady=12)
        tab_cfg = sidebar.add("Cài đặt")
        tab_lib = sidebar.add("Video đã quay")

        self._build_settings_tab(tab_cfg)
        self._build_library_tab(tab_lib)

    def _build_settings_tab(self, parent):
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # Modes
        ctk.CTkLabel(scroll, text="CHẾ ĐỘ GHI HÌNH", font=ctk.CTkFont(size=11, weight="bold"), text_color="#94a3b8").pack(
            anchor="w", pady=(2, 6)
        )
        mode_grid = ctk.CTkFrame(scroll, fg_color="transparent")
        mode_grid.pack(fill="x", pady=(0, 10))
        mode_grid.grid_columnconfigure((0, 1), weight=1)

        self.mode_buttons = {}
        for idx, (m_key, m_name, m_desc) in enumerate(MODES):
            btn = ctk.CTkButton(
                mode_grid,
                text=f"{m_name}\n{m_desc}",
                height=52,
                anchor="w",
                font=ctk.CTkFont(size=11),
                fg_color="#312e81" if m_key == self.mode else "#1a2133",
                hover_color="#28324d",
                border_width=1,
                border_color="#6366f1" if m_key == self.mode else "#293349",
                command=lambda k=m_key: self._set_mode(k),
            )
            btn.grid(row=idx // 2, column=idx % 2, padx=3, pady=3, sticky="ew")
            self.mode_buttons[m_key] = btn

        # Devices
        ctk.CTkLabel(scroll, text="THIẾT BỊ CAMERA & ÂM THANH", font=ctk.CTkFont(size=11, weight="bold"), text_color="#94a3b8").pack(
            anchor="w", pady=(6, 4)
        )
        self.cam_var = ctk.StringVar(value="Đang tải...")
        self.cam_menu = ctk.CTkOptionMenu(scroll, variable=self.cam_var, values=["Đang tải..."], fg_color="#0d111c", command=self._on_cam_changed)
        self.cam_menu.pack(fill="x", pady=3)

        self.mic_var = ctk.StringVar(value="Mặc định")
        self.mic_menu = ctk.CTkOptionMenu(scroll, variable=self.mic_var, values=["Mặc định"], fg_color="#0d111c", command=self._on_audio_dev_changed)
        self.mic_menu.pack(fill="x", pady=3)

        self.sys_var = ctk.StringVar(value="Âm thanh hệ thống mặc định")
        self.sys_menu = ctk.CTkOptionMenu(scroll, variable=self.sys_var, values=["Âm thanh hệ thống mặc định"], fg_color="#0d111c", command=self._on_audio_dev_changed)
        self.sys_menu.pack(fill="x", pady=(3, 8))

        # PiP Layout Options
        self.pip_frame = ctk.CTkFrame(scroll, fg_color="#171e2e", corner_radius=10)
        self.pip_frame.pack(fill="x", pady=4)
        ctk.CTkLabel(self.pip_frame, text="BỐ CỤC KHUNG LỒNG (PiP)", font=ctk.CTkFont(size=11, weight="bold"), text_color="#94a3b8").pack(
            anchor="w", padx=10, pady=(8, 4)
        )
        pos_row = ctk.CTkFrame(self.pip_frame, fg_color="transparent")
        pos_row.pack(fill="x", padx=8, pady=2)
        pos_row.grid_columnconfigure((0, 1), weight=1)
        self.pos_buttons = {}
        for i, (pk, pl) in enumerate([("tl", "Trên trái"), ("tr", "Trên phải"), ("bl", "Dưới trái"), ("br", "Dưới phải")]):
            b = ctk.CTkButton(
                pos_row,
                text=pl,
                height=28,
                fg_color="#312e81" if pk == self.pip_pos else "#1f293d",
                command=lambda k=pk: self._set_pip_pos(k),
            )
            b.grid(row=i // 2, column=i % 2, padx=3, pady=3, sticky="ew")
            self.pos_buttons[pk] = b

        shape_row = ctk.CTkFrame(self.pip_frame, fg_color="transparent")
        shape_row.pack(fill="x", padx=8, pady=4)
        shape_row.grid_columnconfigure((0, 1, 2), weight=1)
        self.shape_buttons = {}
        for i, (sk, sl) in enumerate([("rounded", "Bo góc"), ("circle", "Tròn"), ("square", "Vuông")]):
            b = ctk.CTkButton(
                shape_row,
                text=sl,
                height=28,
                fg_color="#312e81" if sk == self.pip_shape else "#1f293d",
                command=lambda k=sk: self._set_pip_shape(k),
            )
            b.grid(row=0, column=i, padx=2, sticky="ew")
            self.shape_buttons[sk] = b

        self.pip_size_lbl = ctk.CTkLabel(self.pip_frame, text="Kích thước khung lồng: 24%", font=ctk.CTkFont(size=11))
        self.pip_size_lbl.pack(anchor="w", padx=10, pady=(4, 0))
        self.pip_size_slider = ctk.CTkSlider(self.pip_frame, from_=12, to=45, number_of_steps=33, command=self._on_pip_size)
        self.pip_size_slider.set(24)
        self.pip_size_slider.pack(fill="x", padx=10, pady=4)

        self.border_switch = ctk.CTkSwitch(self.pip_frame, text="Viền sáng khung lồng", command=self._on_border_toggle)
        self.border_switch.select()
        self.border_switch.pack(anchor="w", padx=10, pady=(2, 8))

        # Stack Top/Bottom Options
        self.stack_frame = ctk.CTkFrame(scroll, fg_color="#171e2e", corner_radius=10)
        ctk.CTkLabel(self.stack_frame, text="BỐ CỤC XẾP TRÊN & DƯỚI", font=ctk.CTkFont(size=11, weight="bold"), text_color="#94a3b8").pack(
            anchor="w", padx=10, pady=(8, 4)
        )
        ord_row = ctk.CTkFrame(self.stack_frame, fg_color="transparent")
        ord_row.pack(fill="x", padx=8, pady=2)
        ord_row.grid_columnconfigure((0, 1), weight=1)
        self.stack_order_buttons = {}
        for i, (ok, ol) in enumerate([("screen-top", "Màn hình trên"), ("cam-top", "Webcam trên")]):
            b = ctk.CTkButton(
                ord_row,
                text=ol,
                height=28,
                fg_color="#312e81" if ok == self.stack_order else "#1f293d",
                command=lambda k=ok: self._set_stack_order(k),
            )
            b.grid(row=0, column=i, padx=3, sticky="ew")
            self.stack_order_buttons[ok] = b

        self.stack_aspect_var = ctk.StringVar(value="Khít 2 khung (Không cắt)")
        self.stack_aspect_menu = ctk.CTkOptionMenu(
            self.stack_frame,
            variable=self.stack_aspect_var,
            values=["Khít 2 khung (Không cắt)", "Dọc 9:16 (TikTok/Shorts)", "Ngang 16:9 (YouTube)"],
            fg_color="#0d111c",
            command=self._on_stack_aspect,
        )
        self.stack_aspect_menu.pack(fill="x", padx=10, pady=(6, 10))

        self.mirror_switch = ctk.CTkSwitch(scroll, text="Lật gương Webcam (Mirror)", command=self._on_mirror_toggle)
        self.mirror_switch.select()
        self.mirror_switch.pack(anchor="w", padx=6, pady=6)

        # Quality / Video Options
        ctk.CTkLabel(scroll, text="CHẤT LƯỢNG & ĐẦU RA", font=ctk.CTkFont(size=11, weight="bold"), text_color="#94a3b8").pack(
            anchor="w", pady=(8, 4)
        )
        q_grid = ctk.CTkFrame(scroll, fg_color="transparent")
        q_grid.pack(fill="x")
        q_grid.grid_columnconfigure((0, 1), weight=1)

        self.res_var = ctk.StringVar(value="1080p")
        self.res_menu = ctk.CTkOptionMenu(
            q_grid, variable=self.res_var, values=["1080p", "720p", "Gốc"], fg_color="#0d111c", command=self._on_res
        )
        self.res_menu.grid(row=0, column=0, padx=(0, 3), pady=3, sticky="ew")
        self.fps_var = ctk.StringVar(value="30 FPS")
        self.fps_menu = ctk.CTkOptionMenu(
            q_grid, variable=self.fps_var, values=["24 FPS", "30 FPS", "60 FPS"], fg_color="#0d111c", command=self._on_fps
        )
        self.fps_menu.grid(row=0, column=1, padx=(3, 0), pady=3, sticky="ew")

        self.qual_var = ctk.StringVar(value="Cân bằng (Khuyên dùng)")
        self.qual_menu = ctk.CTkOptionMenu(
            scroll,
            variable=self.qual_var,
            values=["Tiết kiệm (Dung lượng nhỏ)", "Cân bằng (Khuyên dùng)", "Chất lượng cao"],
            fg_color="#0d111c",
            command=self._on_quality,
        )
        self.qual_menu.pack(fill="x", pady=3)

        self.cd_var = ctk.StringVar(value="Đếm ngược: 3 giây")
        self.cd_menu = ctk.CTkOptionMenu(
            scroll,
            variable=self.cd_var,
            values=["Đếm ngược: Không", "Đếm ngược: 3 giây", "Đếm ngược: 5 giây"],
            fg_color="#0d111c",
            command=self._on_countdown,
        )
        self.cd_menu.pack(fill="x", pady=3)

        self.split_var = ctk.StringVar(value="Tự cắt file: Không")
        self.split_menu = ctk.CTkOptionMenu(
            scroll,
            variable=self.split_var,
            values=[
                "Tự cắt file: Không",
                "Cắt mỗi 1 phút",
                "Cắt mỗi 3 phút",
                "Cắt mỗi 5 phút",
                "Cắt mỗi 10 phút",
                "Cắt mỗi 15 phút",
                "Cắt mỗi 30 phút",
                "Tùy chỉnh (phút)...",
            ],
            fg_color="#0d111c",
            command=self._on_split_changed,
        )
        self.split_menu.pack(fill="x", pady=3)

        self.custom_split_row = ctk.CTkFrame(scroll, fg_color="transparent")
        self.custom_split_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.custom_split_row, text="Số phút/video:", font=ctk.CTkFont(size=11), text_color="#94a3b8").grid(
            row=0, column=0, padx=(4, 6)
        )
        self.custom_split_entry = ctk.CTkEntry(self.custom_split_row, width=80, fg_color="#0d111c", placeholder_text="vd: 5")
        self.custom_split_entry.insert(0, "5")
        self.custom_split_entry.grid(row=0, column=1, sticky="ew")

        self.live_rec_switch = ctk.CTkSwitch(
            scroll,
            text="Giữ Live Preview khi quay (~4% CPU)",
            command=self._on_live_rec_toggle,
        )
        self.live_rec_switch.pack(anchor="w", padx=6, pady=(6, 2))

        self.automin_switch = ctk.CTkSwitch(scroll, text="Tự thu nhỏ khi quay", command=self._on_automin)
        self.automin_switch.pack(anchor="w", padx=6, pady=6)

        dir_row = ctk.CTkFrame(scroll, fg_color="transparent")
        dir_row.pack(fill="x", pady=4)
        dir_row.grid_columnconfigure(0, weight=1)
        self.dir_entry = ctk.CTkEntry(dir_row, fg_color="#0d111c", border_color="#293349")
        self.dir_entry.insert(0, self.output_dir)
        self.dir_entry.configure(state="readonly")
        self.dir_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_choose_dir = ctk.CTkButton(dir_row, text="Đổi", width=55, fg_color="#1e293b", command=self._choose_dir)
        self.btn_choose_dir.grid(row=0, column=1)

        ctk.CTkLabel(
            scroll,
            text="Phím tắt: Ctrl+Shift+R (Quay/Dừng) · Ctrl+Shift+P (Tạm dừng)",
            font=ctk.CTkFont(size=11),
            text_color="#64748b",
        ).pack(pady=(8, 4))

    def _build_library_tab(self, parent):
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.pack(fill="x", pady=(0, 6))
        self.lib_count_lbl = ctk.CTkLabel(top, text="0 video", font=ctk.CTkFont(size=12, weight="bold"))
        self.lib_count_lbl.pack(side="left")
        ctk.CTkButton(top, text="Làm mới", width=74, height=26, fg_color="#1e293b", command=self._refresh_library).pack(
            side="right"
        )

        self.lib_scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        self.lib_scroll.pack(fill="both", expand=True)

    # ---------- Initialization & Device Enumeration ----------

    def _init_devices_async(self):
        def worker():
            enc = detect_best_encoder(get_ffmpeg_path())
            mons = list_monitors()
            wins = list_windows()
            cams = list_dshow_cameras(get_ffmpeg_path())
            aud = self.audio_mixer.list_devices()
            self.audio_mixer.open_streams()
            self.after(0, lambda: self._apply_discovered_devices(enc, mons + wins, cams, aud))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_discovered_devices(self, enc, sources, cams, aud):
        self.encoder_info = enc
        self.recorder.encoder_info = enc
        self.encoder_badge.configure(text=f"⚡ {enc['label']}")

        if self.custom_region:
            sources = [self.custom_region] + sources

        self.sources = sources
        if sources:
            labels = [s["label"] for s in sources]
            self.source_menu.configure(values=labels)
            if not self.selected_source or not any(s["id"] == self.selected_source.get("id") for s in sources):
                self.selected_source = sources[0]
                self.source_var.set(sources[0]["label"])
            else:
                for s in sources:
                    if s["id"] == self.selected_source["id"]:
                        self.source_var.set(s["label"])
                        break

        self.cameras = cams
        if cams:
            cam_names = [c["name"] for c in cams]
            self.cam_menu.configure(values=cam_names)
            self.cam_var.set(cam_names[0])
            self.selected_cam_idx = 0
        else:
            self.cam_menu.configure(values=["Không tìm thấy Webcam"])
            self.cam_var.set("Không tìm thấy Webcam")

        self.audio_devices = aud
        mic_names = [m["name"] for m in aud["mics"]] or ["Không tìm thấy Micro"]
        sys_names = [s["name"] for s in aud["loopbacks"]] or ["Không có Loopback"]
        self.mic_menu.configure(values=mic_names)
        self.sys_menu.configure(values=sys_names)
        self.mic_var.set(mic_names[0])
        self.sys_var.set(sys_names[0])

        self._refresh_library()
        self._start_preview_worker()

    def _refresh_sources_and_devices(self):
        self._stop_preview_cam()
        self._init_devices_async()

    # ---------- Lightweight Idle Preview (Auto-pauses when recording!) ----------

    def _start_preview_worker(self):
        if getattr(self, "_preview_thread_started", False):
            return
        self._preview_thread_started = True
        threading.Thread(target=self._preview_loop, daemon=True).start()

    def _stop_preview_cam(self):
        with self._cam_lock:
            if self._cam_cap is not None:
                try:
                    self._cam_cap.release()
                except Exception:
                    pass
                self._cam_cap = None
                self._cam_cap_idx = None

    def _read_cam_frame(self):
        if self.mode == "screen" or not self.cameras:
            self._stop_preview_cam()
            return None
        with self._cam_lock:
            if self.recorder.is_recording or self._countdown_active or not self.preview_enabled:
                if self._cam_cap is not None:
                    try:
                        self._cam_cap.release()
                    except Exception:
                        pass
                    self._cam_cap = None
                    self._cam_cap_idx = None
                return None
            if self._cam_cap is None or self._cam_cap_idx != self.selected_cam_idx:
                if time.monotonic() < self._cam_backoff_until:
                    return None
                if self._cam_cap is not None:
                    try:
                        self._cam_cap.release()
                    except Exception:
                        pass
                    self._cam_cap = None
                cap = cv2.VideoCapture(self.selected_cam_idx, cv2.CAP_DSHOW)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
                if cap.isOpened():
                    self._cam_cap = cap
                    self._cam_cap_idx = self.selected_cam_idx
                    self._cam_fail_logged = False
                else:
                    if not self._cam_fail_logged:
                        logger.warning("Preview VideoCapture failed to open camera index=%s", self.selected_cam_idx)
                        self._cam_fail_logged = True
                    self._cam_backoff_until = time.monotonic() + 2.0
                    cap.release()
                    return None
            ok, frame = self._cam_cap.read()
        if not ok or frame is None:
            return None
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        if self.mirror_cam:
            img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        return img

    def _read_screen_frame(self, sct):
        if self.mode == "webcam" or not self.selected_source:
            return None
        src = self.selected_source
        if src["kind"] in ("monitor", "region"):
            mon = {"left": src["left"], "top": src["top"], "width": src["width"], "height": src["height"]}
        elif src["kind"] == "window":
            left = src.get("left", 0)
            top = src.get("top", 0)
            w = src.get("width", 0)
            h = src.get("height", 0)
            hwnd = src.get("hwnd")
            if hwnd:
                try:
                    left, top, w, h = get_window_rect(hwnd)
                except Exception:
                    pass
            if w > 32 and h > 32:
                mon = {"left": left, "top": top, "width": w, "height": h}
            else:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        else:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        try:
            raw = sct.grab(mon)
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception:
            return None

    def _compose_preview_image(self, scr_img, cam_img) -> Image.Image:
        W, H = PREVIEW_W, PREVIEW_H
        if self.mode == "screen":
            return _fit_contain(scr_img, W, H) if scr_img else Image.new("RGB", (W, H), (9, 11, 18))

        if self.mode == "webcam":
            return _fit_cover(cam_img, W, H) if cam_img else Image.new("RGB", (W, H), (9, 11, 18))

        if self.mode == "dual":
            canvas = Image.new("RGB", (W, H), (9, 11, 18))
            half = (W - 6) // 2
            if scr_img:
                canvas.paste(_fit_contain(scr_img, half, H), (0, 0))
            if cam_img:
                canvas.paste(_fit_contain(cam_img, half, H), (half + 6, 0))
            return canvas

        if self.mode == "stack-tb":
            gap = 3
            if self.stack_aspect == "9:16":
                box_w = int(H * 9 / 16)
                canvas = Image.new("RGB", (W, H), (7, 9, 14))
                inner = Image.new("RGB", (box_w, H), (18, 24, 38))
                top_h = (H - gap) // 2
                bot_h = H - gap - top_h
                top_img = scr_img if self.stack_order == "screen-top" else cam_img
                bot_img = cam_img if self.stack_order == "screen-top" else scr_img
                if top_img:
                    fn = _fit_contain if self.stack_order == "screen-top" else _fit_cover
                    inner.paste(fn(top_img, box_w, top_h), (0, 0))
                if bot_img:
                    fn = _fit_cover if self.stack_order == "screen-top" else _fit_contain
                    inner.paste(fn(bot_img, box_w, bot_h), (0, top_h + gap))
                canvas.paste(inner, ((W - box_w) // 2, 0))
                return canvas
            canvas = Image.new("RGB", (W, H), (18, 24, 38))
            top_h = (H - gap) // 2
            bot_h = H - gap - top_h
            top_img = scr_img if self.stack_order == "screen-top" else cam_img
            bot_img = cam_img if self.stack_order == "screen-top" else scr_img
            if top_img:
                canvas.paste(_fit_contain(top_img, W, top_h), (0, 0))
            if bot_img:
                canvas.paste(_fit_contain(bot_img, W, bot_h), (0, top_h + gap))
            return canvas

        # pip or pip-screen
        if self.mode == "pip":
            bg = _fit_contain(scr_img, W, H) if scr_img else Image.new("RGB", (W, H), (9, 11, 18))
            fg_src = cam_img
        else:
            bg = _fit_cover(cam_img, W, H) if cam_img else Image.new("RGB", (W, H), (9, 11, 18))
            fg_src = scr_img

        if fg_src is None:
            return bg

        pw = max(40, int(W * self.pip_size_pct / 100))
        ph = pw if self.pip_shape in ("circle", "square") else max(30, int(pw * 9 / 16))
        margin = 12
        px = margin if self.pip_pos in ("tl", "bl") else W - pw - margin
        py = margin if self.pip_pos in ("tl", "tr") else H - ph - margin

        fg = _fit_cover(fg_src, pw, ph) if self.mode == "pip" else _fit_contain(fg_src, pw, ph, (15, 23, 42))
        mask = Image.new("L", (pw, ph), 0)
        d = ImageDraw.Draw(mask)
        if self.pip_shape == "circle":
            d.ellipse((0, 0, pw - 1, ph - 1), fill=255)
        elif self.pip_shape == "rounded":
            d.rounded_rectangle((0, 0, pw - 1, ph - 1), radius=max(4, int(pw * 0.09)), fill=255)
        else:
            d.rectangle((0, 0, pw - 1, ph - 1), fill=255)

        bg.paste(fg, (px, py), mask)
        if self.cam_border:
            bd = ImageDraw.Draw(bg)
            col = (129, 140, 248)
            if self.pip_shape == "circle":
                bd.ellipse((px, py, px + pw - 1, py + ph - 1), outline=col, width=2)
            elif self.pip_shape == "rounded":
                bd.rounded_rectangle((px, py, px + pw - 1, py + ph - 1), radius=max(4, int(pw * 0.09)), outline=col, width=2)
            else:
                bd.rectangle((px, py, px + pw - 1, py + ph - 1), outline=col, width=2)
        return bg

    def _preview_loop(self):
        with mss.MSS() as sct:
            while self._preview_running:
                if self.recorder.is_recording or self._countdown_active or not self.preview_enabled:
                    self._stop_preview_cam()
                    time.sleep(0.25)
                    continue
                t0 = time.perf_counter()
                scr = self._read_screen_frame(sct)
                cam = self._read_cam_frame()
                comp = self._compose_preview_image(scr, cam)
                with self._preview_lock:
                    self._latest_preview_pil = comp
                elapsed = time.perf_counter() - t0
                time.sleep(max(0.03, 0.1 - elapsed))  # ~10 FPS lightweight preview

    def _show_preview_image(self, pil_img: Image.Image):
        if pil_img.size != (PREVIEW_W, PREVIEW_H):
            pil_img = pil_img.resize((PREVIEW_W, PREVIEW_H), Image.Resampling.BILINEAR)
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        self._tk_photo.paste(pil_img)
        self.preview_label.configure(image=self._tk_photo, text="")

    def _show_preview_text(self, text: str):
        self.preview_label.configure(image="", text=text)

    # ---------- Periodic UI Tick ----------

    def _ui_tick(self):
        # Handle global hotkey safely on main Tkinter thread
        hk = self._pending_hotkey
        if hk is not None:
            self._pending_hotkey = None
            if hk == "record":
                self.toggle_record()
            elif hk == "pause":
                self.toggle_pause()

        # Update audio VU meters
        self.mic_vu.set(self.audio_mixer.mic_level)
        self.sys_vu.set(self.audio_mixer.sys_level)

        # Update lightweight preview image when idle
        if not self.recorder.is_recording and not self._countdown_active and self.preview_enabled:
            img = None
            with self._preview_lock:
                if self._latest_preview_pil is not None:
                    img = self._latest_preview_pil
                    self._latest_preview_pil = None
            if img is not None:
                self._show_preview_image(img)

        # Update recording timer, live recording preview & auto-split check
        if self.recorder.is_recording:
            alive, err_msg = self.recorder.check_alive()
            if not alive and not self._splitting:
                try:
                    self.recorder.stop()
                except Exception:
                    logger.exception("Error during recorder.stop() after unexpected exit")
                    self.recorder.is_recording = False
                    self.recorder.is_paused = False
                self._finish_stop_ui()
                hint = ""
                if "already in use" in err_msg or "Could not run graph" in err_msg:
                    hint = (
                        "Gợi ý: Webcam đang được ứng dụng khác (Chrome/Zoom/Teams/tab web...) sử dụng "
                        "hoặc driver USB chưa kịp nhả kết nối. Hãy đóng ứng dụng đang dùng camera hoặc chuyển sang chế độ 'Chỉ Màn hình'.\n\n"
                    )
                messagebox.showerror(
                    "Lỗi FFmpeg khi quay",
                    f"{hint}Chi tiết kỹ thuật:\n{err_msg}\n\nLog: {get_log_file_path()}",
                )
            elif not self.recorder.is_paused:
                if self.live_while_recording:
                    live_img = self.recorder.pop_live_frame()
                    if live_img is not None:
                        self._show_preview_image(live_img)

                elapsed = time.perf_counter() - self._rec_started_at - self._paused_accum
                part_elapsed = max(0.0, elapsed - self._part_start_elapsed)
                split_sec = self._get_split_seconds()
                txt = format_duration(elapsed)
                self.timer_lbl.configure(text=txt)

                if split_sec > 0:
                    rem = max(0.0, split_sec - part_elapsed)
                    self.status_badge.configure(
                        text=f"● Phần {self.recorder.part_index} · {format_duration(part_elapsed)} (cắt sau {format_duration(rem)})"
                    )
                    if part_elapsed >= split_sec and not self._splitting:
                        self._trigger_auto_split(elapsed)
                else:
                    self.status_badge.configure(text=f"● Đang ghi {txt}")

                self.size_lbl.configure(text=format_bytes(self.recorder.active_size_bytes()))

        self.after(90, self._ui_tick)

    # ---------- Recording Controls ----------

    def toggle_record(self):
        if self._countdown_active:
            return
        if self.recorder.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        if self._countdown_active or self.recorder.is_recording:
            return
        if not self.selected_source and self.mode != "webcam":
            messagebox.showwarning("Thiếu nguồn", "Chưa tìm thấy màn hình hoặc cửa sổ cần quay.")
            return
        if self.mode != "screen" and not self.cameras:
            messagebox.showwarning("Thiếu Webcam", "Không tìm thấy Webcam cho chế độ đã chọn.")
            return

        # Hide floating bounding frame during recording to avoid recording the border
        if self.floating_frame and self.floating_frame.visible:
            self._frame_was_visible = True
            self.floating_frame.hide()
            self.btn_toggle_frame.configure(fg_color="#1e293b", hover_color="#334155", text="👁️ Khung viền")

        # Release preview webcam immediately so FFmpeg dshow owns it exclusively
        self._countdown_active = True
        self._countdown_token += 1
        token = self._countdown_token
        self._set_controls_locked(True)
        self.btn_record.configure(state="disabled")
        self._stop_preview_cam()
        self._show_preview_text("Đang chuẩn bị quay...")

        if self.countdown_sec > 0:
            self._run_countdown_step(self.countdown_sec, token)
        elif self.mode != "screen":
            # Give Windows USB DirectShow driver ~450ms to release preview handle cleanly
            self.after(450, lambda: self._launch_recorder_now(token))
        else:
            self._launch_recorder_now(token)

    def _run_countdown_step(self, remaining: int, token: int):
        if token != self._countdown_token or not self._countdown_active:
            return
        if remaining <= 0:
            self._launch_recorder_now(token)
            return
        self._show_preview_text(f"Bắt đầu sau {remaining}...")
        self.after(1000, lambda: self._run_countdown_step(remaining - 1, token))

    def _launch_recorder_now(self, token: int = None):
        if token is not None and token != self._countdown_token:
            return
        split_sec = self._get_split_seconds()
        cfg = {
            "mode": self.mode,
            "source": self.selected_source,
            "camera_name": self.cameras[self.selected_cam_idx]["name"] if self.cameras else "",
            "pip_pos": self.pip_pos,
            "pip_shape": self.pip_shape,
            "pip_size_pct": self.pip_size_pct,
            "mirror_cam": self.mirror_cam,
            "cam_border": self.cam_border,
            "stack_order": self.stack_order,
            "stack_aspect": self.stack_aspect,
            "resolution": self.resolution,
            "fps": self.fps,
            "quality": self.quality,
            "output_dir": self.output_dir,
            "live_while_recording": self.live_while_recording,
            "split_minutes": split_sec / 60.0 if split_sec > 0 else 0,
        }
        try:
            self._stop_preview_cam()
            self.recorder.start(cfg)
        except Exception as exc:
            logger.exception("Failed to start recorder")
            self._countdown_active = False
            self._finish_stop_ui()
            messagebox.showerror("Không thể bắt đầu quay", f"{exc}\n\nLog: {get_log_file_path()}")
            return
        finally:
            self._countdown_active = False

        self._rec_started_at = time.perf_counter()
        self._paused_accum = 0.0
        self._pause_started_at = 0.0
        self._part_start_elapsed = 0.0
        self._splitting = False

        self.btn_record.configure(state="normal", text="■ Dừng quay", fg_color="#334155", hover_color="#475569")
        self.btn_pause.configure(text="Tạm dừng")
        self.btn_pause.grid(row=0, column=1, padx=(0, 8))
        self.status_badge.configure(text="● Đang ghi 00:00:00", fg_color="#3b1219", text_color="#fca5a5")
        if not self.live_while_recording:
            self._show_preview_text("● ĐANG GHI HÌNH TRỰC TIẾP RA MP4\n(Đã tạm tắt xem trước để tiết kiệm tối đa CPU/RAM)")
        if self.auto_minimize:
            self.iconify()

    def _trigger_auto_split(self, current_elapsed: float):
        self._splitting = True
        self._part_start_elapsed = current_elapsed
        self.status_badge.configure(text="✂ Đang cắt phần video...")

        def worker():
            try:
                saved = self.recorder.split_part()
                self.after(0, lambda: self._on_split_done(saved))
            except Exception as exc:
                self.after(0, lambda: self._on_split_error(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_split_done(self, _saved):
        self._splitting = False
        self._refresh_library()

    def _on_split_error(self, err_msg):
        self._splitting = False
        messagebox.showerror("Lỗi tự cắt video", err_msg)

    def toggle_pause(self):
        if not self.recorder.is_recording:
            return
        if not self.recorder.is_paused:
            self._pause_started_at = time.perf_counter()
            self.recorder.pause()
            self.btn_pause.configure(text="Tiếp tục")
            self.status_badge.configure(text="❚❚ Tạm dừng", fg_color="#3b280c", text_color="#fde68a")
        else:
            self._paused_accum += time.perf_counter() - self._pause_started_at
            self.recorder.resume()
            self.btn_pause.configure(text="Tạm dừng")
            self.status_badge.configure(text="● Đang ghi", fg_color="#3b1219", text_color="#fca5a5")

    def _stop_recording(self):
        self.status_badge.configure(text="Đang đóng gói MP4...", fg_color="#1e293b", text_color="#93c5fd")
        self.btn_record.configure(state="disabled")
        self.btn_pause.configure(state="disabled")

        def worker():
            try:
                saved = self.recorder.stop()
                self.after(0, lambda: self._on_stop_success(saved))
            except Exception as exc:
                self.after(0, lambda: self._on_stop_error(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_stop_ui(self):
        self.btn_record.configure(state="normal", text="● Bắt đầu quay", fg_color="#e11d48", hover_color="#be123c")
        self.btn_pause.configure(state="normal")
        self.btn_pause.grid_forget()
        self.status_badge.configure(text="● Sẵn sàng", fg_color="#142923", text_color="#34d399")
        self.timer_lbl.configure(text="00:00:00")
        self.size_lbl.configure(text="0.0 MB")
        self._set_controls_locked(False)
        if not self.preview_enabled:
            self._show_preview_text("Xem trước đang tắt (Tiết kiệm CPU)")
        if self.auto_minimize and self.state() == "iconic":
            self.deiconify()

        # Restore floating frame if it was active before recording
        if getattr(self, "_frame_was_visible", False):
            self._frame_was_visible = False
            if self.custom_region and self.floating_frame:
                self.floating_frame.show(
                    self.custom_region["left"],
                    self.custom_region["top"],
                    self.custom_region["width"],
                    self.custom_region["height"],
                )
                self.btn_toggle_frame.configure(fg_color="#059669", hover_color="#047857", text="👁️ Khung: BẬT")

    def _on_stop_success(self, saved_paths):
        self._finish_stop_ui()
        self._refresh_library()
        names = "\n".join(os.path.basename(p) for p in saved_paths)
        if saved_paths:
            messagebox.showinfo("Đã lưu video MP4", f"Hoàn tất tức thì:\n{names}")

    def _on_stop_error(self, err_msg):
        logger.error("Stop error: %s", err_msg)
        self._finish_stop_ui()
        messagebox.showerror("Lỗi lưu video", f"{err_msg}\n\nLog: {get_log_file_path()}")

    def _set_controls_locked(self, locked: bool):
        state = "disabled" if locked else "normal"
        for widget in (
            self.source_menu,
            self.btn_select_region,
            self.btn_toggle_frame,
            self.cam_menu,
            self.mic_menu,
            self.sys_menu,
            self.split_menu,
            self.custom_split_entry,
            self.live_rec_switch,
            self.res_menu,
            self.fps_menu,
            self.qual_menu,
            self.cd_menu,
            self.btn_refresh,
            self.btn_choose_dir,
            self.preview_switch,
            self.automin_switch,
        ):
            widget.configure(state=state)
        for b in self.mode_buttons.values():
            b.configure(state=state)

    # ---------- UI Callbacks ----------

    def _set_mode(self, mode_key: str):
        if self.recorder.is_recording:
            return
        self.mode = mode_key
        for k, btn in self.mode_buttons.items():
            btn.configure(
                fg_color="#312e81" if k == mode_key else "#1a2133",
                border_color="#6366f1" if k == mode_key else "#293349",
            )
        if mode_key in ("pip", "pip-screen"):
            self.pip_frame.pack(fill="x", pady=4, before=self.mirror_switch)
        else:
            self.pip_frame.pack_forget()

        if mode_key == "stack-tb":
            self.stack_frame.pack(fill="x", pady=4, before=self.mirror_switch)
        else:
            self.stack_frame.pack_forget()

    def _set_pip_pos(self, pos: str):
        self.pip_pos = pos
        for k, b in self.pos_buttons.items():
            b.configure(fg_color="#312e81" if k == pos else "#1f293d")

    def _set_pip_shape(self, shape: str):
        self.pip_shape = shape
        for k, b in self.shape_buttons.items():
            b.configure(fg_color="#312e81" if k == shape else "#1f293d")

    def _on_pip_size(self, val):
        self.pip_size_pct = int(round(val))
        self.pip_size_lbl.configure(text=f"Kích thước khung lồng: {self.pip_size_pct}%")

    def _on_border_toggle(self):
        self.cam_border = bool(self.border_switch.get())

    def _on_mirror_toggle(self):
        self.mirror_cam = bool(self.mirror_switch.get())

    def _set_stack_order(self, order: str):
        self.stack_order = order
        for k, b in self.stack_order_buttons.items():
            b.configure(fg_color="#312e81" if k == order else "#1f293d")

    def _on_stack_aspect(self, label: str):
        if "9:16" in label:
            self.stack_aspect = "9:16"
        elif "16:9" in label:
            self.stack_aspect = "16:9"
        else:
            self.stack_aspect = "auto"

    def _on_source_selected(self, label: str):
        for s in self.sources:
            if s["label"] == label:
                self.selected_source = s
                if s.get("kind") != "region" and self.floating_frame:
                    self.floating_frame.hide()
                    self.btn_toggle_frame.configure(fg_color="#1e293b", hover_color="#334155", text="👁️ Khung viền")
                elif s.get("kind") == "region" and self.floating_frame and self.custom_region:
                    self.floating_frame.show(
                        self.custom_region["left"],
                        self.custom_region["top"],
                        self.custom_region["width"],
                        self.custom_region["height"],
                    )
                    self.btn_toggle_frame.configure(fg_color="#059669", hover_color="#047857", text="👁️ Khung: BẬT")
                break

    # ---------- Region Selection & Floating Frame ----------

    def _start_region_selection(self):
        if self.recorder.is_recording:
            messagebox.showwarning("Đang ghi hình", "Không thể đổi vùng quay khi đang quay video.")
            return
        if self.floating_frame:
            self.floating_frame.hide()

        self.withdraw()
        self.after(200, self._launch_region_selector)

    def _launch_region_selector(self):
        try:
            RegionSelector(
                self,
                initial_region=self.custom_region,
                on_selected=self._on_region_selected,
            )
        except Exception as exc:
            logger.exception("Failed to launch region selector")
            self.deiconify()
            messagebox.showerror("Lỗi chọn vùng", f"Không thể mở công cụ vẽ vùng:\n{exc}")

    def _on_region_selected(self, result):
        self.deiconify()
        self.lift()
        self.focus_force()

        if not result:
            if self.floating_frame and self.custom_region and self.selected_source and self.selected_source.get("kind") == "region":
                self.floating_frame.show(
                    self.custom_region["left"],
                    self.custom_region["top"],
                    self.custom_region["width"],
                    self.custom_region["height"],
                )
            return

        self.custom_region = result
        self.sources = [s for s in self.sources if s.get("kind") != "region"]
        self.sources.insert(0, result)

        labels = [s["label"] for s in self.sources]
        self.source_menu.configure(values=labels)
        self.selected_source = result
        self.source_var.set(result["label"])
        self.btn_select_region.configure(text="📐 Đổi vùng")

        if not self.floating_frame:
            self.floating_frame = FloatingRegionFrame(
                self,
                on_region_changed=self._on_floating_region_moved,
                on_close=self._on_floating_frame_closed,
            )
        self.floating_frame.show(result["left"], result["top"], result["width"], result["height"])
        self.btn_toggle_frame.configure(fg_color="#059669", hover_color="#047857", text="👁️ Khung: BẬT")

    def _on_floating_region_moved(self, left, top, width, height):
        if self.custom_region:
            self.custom_region["left"] = left
            self.custom_region["top"] = top
            self.custom_region["width"] = width
            self.custom_region["height"] = height
            new_label = f"📐 Vùng quay: {width}×{height} (tại {left}, {top})"
            self.custom_region["label"] = new_label

            if self.selected_source and self.selected_source.get("kind") == "region":
                self.selected_source = self.custom_region
                self.source_var.set(new_label)
                for s in self.sources:
                    if s.get("kind") == "region":
                        s.update(self.custom_region)
                self.source_menu.configure(values=[s["label"] for s in self.sources])

    def _toggle_floating_frame(self):
        if self.recorder.is_recording:
            return
        if not self.custom_region:
            self._start_region_selection()
            return

        if self.floating_frame and self.floating_frame.visible:
            self.floating_frame.hide()
            self.btn_toggle_frame.configure(fg_color="#1e293b", hover_color="#334155", text="👁️ Khung viền")
        else:
            if not self.floating_frame:
                self.floating_frame = FloatingRegionFrame(
                    self,
                    on_region_changed=self._on_floating_region_moved,
                    on_close=self._on_floating_frame_closed,
                )
            self.floating_frame.show(
                self.custom_region["left"],
                self.custom_region["top"],
                self.custom_region["width"],
                self.custom_region["height"],
            )
            self.btn_toggle_frame.configure(fg_color="#059669", hover_color="#047857", text="👁️ Khung: BẬT")

    def _on_floating_frame_closed(self):
        self.btn_toggle_frame.configure(fg_color="#1e293b", hover_color="#334155", text="👁️ Khung viền")

    def _on_cam_changed(self, name: str):
        for idx, c in enumerate(self.cameras):
            if c["name"] == name:
                self.selected_cam_idx = idx
                break

    def _on_audio_dev_changed(self, _val):
        mic_idx = None
        sys_idx = None
        for m in self.audio_devices["mics"]:
            if m["name"] == self.mic_var.get():
                mic_idx = m["index"]
                break
        for s in self.audio_devices["loopbacks"]:
            if s["name"] == self.sys_var.get():
                sys_idx = s["index"]
                break
        self.audio_mixer.open_streams(mic_idx, sys_idx)

    def _on_toggle_preview(self):
        self.preview_enabled = bool(self.preview_switch.get())
        if not self.preview_enabled:
            self._stop_preview_cam()
            self._show_preview_text("Xem trước đang tắt (Tiết kiệm CPU)")

    def _toggle_mic(self):
        self.audio_mixer.mic_enabled = not self.audio_mixer.mic_enabled
        on = self.audio_mixer.mic_enabled
        self.btn_mic.configure(fg_color="#312e81" if on else "#450a0a", text="🎤 Mic" if on else "🎤 Tắt")

    def _toggle_sys(self):
        self.audio_mixer.sys_enabled = not self.audio_mixer.sys_enabled
        on = self.audio_mixer.sys_enabled
        self.btn_sys.configure(fg_color="#312e81" if on else "#450a0a", text="🔊 Máy" if on else "🔊 Tắt")

    def _on_mic_gain(self, val):
        self.audio_mixer.mic_gain = float(val) / 100.0

    def _on_sys_gain(self, val):
        self.audio_mixer.sys_gain = float(val) / 100.0

    def _on_res(self, val):
        self.resolution = "native" if val == "Gốc" else val

    def _on_fps(self, val):
        self.fps = int(val.split()[0])

    def _on_quality(self, val):
        if "Tiết kiệm" in val:
            self.quality = "eco"
        elif "cao" in val:
            self.quality = "high"
        else:
            self.quality = "balanced"

    def _on_countdown(self, val):
        if "3" in val:
            self.countdown_sec = 3
        elif "5" in val:
            self.countdown_sec = 5
        else:
            self.countdown_sec = 0

    def _on_split_changed(self, val: str):
        if "Tùy chỉnh" in val:
            self.custom_split_row.pack(fill="x", pady=(2, 4), before=self.live_rec_switch)
            self.split_minutes = -1.0  # read from entry
        else:
            self.custom_split_row.pack_forget()
            if "Không" in val:
                self.split_minutes = 0.0
            else:
                for token in val.split():
                    if token.isdigit():
                        self.split_minutes = float(token)
                        break

    def _get_split_seconds(self) -> float:
        if self.split_minutes < 0:
            try:
                mins = float(self.custom_split_entry.get().strip().replace(",", "."))
                return max(0.0, mins * 60.0)
            except Exception:
                return 0.0
        return max(0.0, self.split_minutes * 60.0)

    def _on_live_rec_toggle(self):
        self.live_while_recording = bool(self.live_rec_switch.get())

    def _open_log_file(self):
        p = get_log_file_path()
        if os.path.exists(p):
            os.startfile(p)
        else:
            os.startfile(os.path.dirname(p))

    def _open_output_dir(self):
        os.makedirs(self.output_dir, exist_ok=True)
        os.startfile(self.output_dir)

    def _on_automin(self):
        self.auto_minimize = bool(self.automin_switch.get())

    def _choose_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.output_dir, title="Chọn thư mục lưu video")
        if chosen:
            self.output_dir = chosen
            os.makedirs(chosen, exist_ok=True)
            self._save_output_dir(chosen)
            self.dir_entry.configure(state="normal")
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, chosen)
            self.dir_entry.configure(state="readonly")
            self._refresh_library()

    # ---------- Recordings Library ----------

    def _refresh_library(self):
        for w in self.lib_scroll.winfo_children():
            w.destroy()
        os.makedirs(self.output_dir, exist_ok=True)
        files = []
        for fn in os.listdir(self.output_dir):
            if fn.lower().endswith((".mp4", ".mkv", ".webm")):
                fp = os.path.join(self.output_dir, fn)
                if os.path.isfile(fp):
                    st = os.stat(fp)
                    files.append((fn, fp, st.st_size, st.st_mtime))
        files.sort(key=lambda x: x[3], reverse=True)
        self.lib_count_lbl.configure(text=f"{len(files)} video")

        if not files:
            ctk.CTkLabel(self.lib_scroll, text="Chưa có video nào.", text_color="#64748b").pack(pady=30)
            return

        for fn, fp, size, mtime in files:
            card = ctk.CTkFrame(self.lib_scroll, fg_color="#1a2133", corner_radius=10)
            card.pack(fill="x", pady=4)
            ctk.CTkLabel(card, text=fn, font=ctk.CTkFont(size=12, weight="bold"), anchor="w", wraplength=280).pack(
                fill="x", padx=10, pady=(8, 2)
            )
            dt = datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M")
            ctk.CTkLabel(
                card,
                text=f"{format_bytes(size)}  ·  {dt}",
                font=ctk.CTkFont(size=11),
                text_color="#94a3b8",
                anchor="w",
            ).pack(fill="x", padx=10)

            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=(6, 8))
            row.grid_columnconfigure((0, 1, 2), weight=1)
            ctk.CTkButton(row, text="Phát", height=26, fg_color="#312e81", command=lambda p=fp: os.startfile(p)).grid(
                row=0, column=0, padx=2, sticky="ew"
            )
            ctk.CTkButton(
                row,
                text="Thư mục",
                height=26,
                fg_color="#253047",
                command=lambda p=fp: subprocess.Popen(["explorer", "/select,", os.path.normpath(p)]),
            ).grid(row=0, column=1, padx=2, sticky="ew")
            ctk.CTkButton(
                row,
                text="Xóa",
                height=26,
                fg_color="#450a0a",
                hover_color="#7f1d1d",
                command=lambda p=fp: self._delete_file(p),
            ).grid(row=0, column=2, padx=2, sticky="ew")

    def _delete_file(self, path: str):
        try:
            os.remove(path)
            self._refresh_library()
        except OSError as exc:
            messagebox.showerror("Không thể xóa", str(exc))

    # ---------- Global Hotkeys (Ctrl+Shift+R / Ctrl+Shift+P) ----------

    def _start_hotkey_listener(self):
        def loop():
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            self._hotkey_tid = kernel32.GetCurrentThreadId()
            MOD_CONTROL = 0x0002
            MOD_SHIFT = 0x0004
            WM_HOTKEY = 0x0312
            user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_SHIFT, ord("R"))
            user32.RegisterHotKey(None, 2, MOD_CONTROL | MOD_SHIFT, ord("P"))
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY:
                    if msg.wParam == 1:
                        self._pending_hotkey = "record"
                    elif msg.wParam == 2:
                        self._pending_hotkey = "pause"
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            user32.UnregisterHotKey(None, 1)
            user32.UnregisterHotKey(None, 2)

        threading.Thread(target=loop, daemon=True).start()

    def _on_close(self):
        if self.recorder.is_recording:
            if not messagebox.askyesno("Đang ghi hình", "Bạn đang quay video. Dừng ghi và thoát?"):
                return
            try:
                self.recorder.stop()
            except Exception:
                logger.exception("Error stopping recorder on close")
        self._countdown_token += 1
        self._countdown_active = False
        self._preview_running = False
        self._stop_preview_cam()
        if self._hotkey_tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._hotkey_tid, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass
        if self.floating_frame:
            try:
                self.floating_frame.destroy()
            except Exception:
                pass
        self.audio_mixer.shutdown()
        self.destroy()


if __name__ == "__main__":
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "ScreenCamLite_SingleInstance_Mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showinfo(APP_NAME, "ScreenCam Lite đang chạy rồi! Vui lòng kiểm tra cửa sổ đang mở.")
        _r.destroy()
        sys.exit(0)
    app = ScreenCamLiteApp()
    app.mainloop()
