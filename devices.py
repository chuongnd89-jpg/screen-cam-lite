import ctypes
import os
import re
import subprocess
import sys
import mss


def get_base_dir() -> str:
    if getattr(sys, "frozen", False):
        # PyInstaller --onedir places bundled files in _MEIPASS or executable dir
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_ffmpeg_path() -> str:
    base = get_base_dir()
    candidates = [
        os.path.join(base, "bin", "ffmpeg.exe"),
        os.path.join(os.path.dirname(sys.executable), "bin", "ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "ffmpeg.exe"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return "ffmpeg"


def get_asset_path(name: str) -> str:
    return os.path.join(get_base_dir(), "assets", name)


def list_monitors():
    results = []
    with mss.MSS() as sct:
        mons = sct.monitors
        # Individual monitors first (index 1..N)
        for idx in range(1, len(mons)):
            m = mons[idx]
            tag = " (Chính)" if m.get("is_primary") else ""
            results.append(
                {
                    "id": f"monitor:{idx}",
                    "kind": "monitor",
                    "label": f"Màn hình {idx}{tag} — {m['width']}×{m['height']}",
                    "left": m["left"],
                    "top": m["top"],
                    "width": m["width"],
                    "height": m["height"],
                }
            )
        if len(mons) > 2:
            m0 = mons[0]
            results.append(
                {
                    "id": "monitor:0",
                    "kind": "monitor",
                    "label": f"Tất cả màn hình — {m0['width']}×{m0['height']}",
                    "left": m0["left"],
                    "top": m0["top"],
                    "width": m0["width"],
                    "height": m0["height"],
                }
            )
    return results


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def get_window_rect(hwnd):
    rect = RECT()
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    try:
        dwmapi = ctypes.windll.dwmapi
        hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
        if hr == 0:
            w = max(2, ((rect.right - rect.left) // 2) * 2)
            h = max(2, ((rect.bottom - rect.top) // 2) * 2)
            return rect.left, rect.top, w, h
    except Exception:
        pass
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = max(2, ((rect.right - rect.left) // 2) * 2)
    h = max(2, ((rect.bottom - rect.top) // 2) * 2)
    return rect.left, rect.top, w, h


def list_windows():
    user32 = ctypes.windll.user32
    windows = []
    seen = set()

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or title in ("Program Manager", "Settings", "Windows Input Experience", "ScreenCam Lite"):
            return True
        if title not in seen:
            seen.add(title)
            left, top, w, h = get_window_rect(hwnd)
            if w >= 120 and h >= 120:
                windows.append({
                    "id": f"window:{title}",
                    "kind": "window",
                    "label": f"Cửa sổ: {title} ({w}×{h})",
                    "title": title,
                    "hwnd": hwnd,
                    "left": left,
                    "top": top,
                    "width": w,
                    "height": h,
                })
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return windows


def list_dshow_cameras(ffmpeg_path: str = None):
    ff = ffmpeg_path or get_ffmpeg_path()
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = subprocess.run(
            [ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
            creationflags=creationflags,
        )
        lines = (res.stderr or "").splitlines()
    except Exception:
        return []

    cameras = []
    for line in lines:
        m = re.search(r'"([^"]+)"\s+\(video\)', line)
        if m:
            cameras.append({"index": len(cameras), "name": m.group(1)})
    return cameras


def detect_best_encoder(ffmpeg_path: str = None) -> dict:
    ff = ffmpeg_path or get_ffmpeg_path()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    tests = [
        {
            "name": "h264_qsv",
            "label": "Intel Quick Sync (GPU)",
            "args": ["-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2:r=30", "-pix_fmt", "nv12", "-c:v", "h264_qsv", "-f", "null", "-"],
        },
        {
            "name": "h264_mf",
            "label": "MediaFoundation (GPU)",
            "args": ["-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2:r=30", "-pix_fmt", "nv12", "-c:v", "h264_mf", "-f", "null", "-"],
        },
    ]
    for t in tests:
        try:
            r = subprocess.run(
                [ff, "-hide_banner", "-y", *t["args"]],
                capture_output=True,
                timeout=5,
                creationflags=creationflags,
            )
            if r.returncode == 0:
                return {"codec": t["name"], "label": t["label"]}
        except Exception:
            pass
    return {"codec": "libx264", "label": "x264 (CPU nhẹ)"}
