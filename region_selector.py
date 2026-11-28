"""Region selection modal & floating border frame for ScreenCam Lite."""

import ctypes
from ctypes import wintypes
import logging
import tkinter as tk
from PIL import Image, ImageDraw, ImageTk
import mss

logger = logging.getLogger("ScreenCamLite.region")


def _even(v: int) -> int:
    return max(2, (int(v) // 2) * 2)


def get_virtual_bounds() -> tuple:
    """Returns (left, top, width, height) of the Windows virtual desktop."""
    with mss.MSS() as sct:
        vmon = sct.monitors[0]
        return int(vmon["left"]), int(vmon["top"]), int(vmon["width"]), int(vmon["height"])


def clamp_region_to_desktop(left: int, top: int, width: int, height: int, bounds: tuple = None) -> tuple:
    """Clamps a capture rectangle cleanly inside virtual desktop bounds with even dimensions."""
    vx, vy, vw, vh = bounds or get_virtual_bounds()
    w = min(_even(vw), max(32, _even(width)))
    h = min(_even(vh), max(32, _even(height)))
    x = max(vx, min(vx + vw - w, int(left)))
    y = max(vy, min(vy + vh - h, int(top)))
    return x, y, w, h


class RegionSelector:
    """Full-screen interactive desktop overlay to draw, move, and resize a recording rectangle."""

    HANDLE_SIZE = 8

    def __init__(self, parent, initial_region=None, on_selected=None):
        self.parent = parent
        self.on_selected = on_selected
        self.initial_region = initial_region

        # Capture virtual desktop bounding box
        with mss.MSS() as sct:
            vmon = sct.monitors[0]
            self.vx = vmon["left"]
            self.vy = vmon["top"]
            self.vw = vmon["width"]
            self.vh = vmon["height"]
            raw = sct.grab(vmon)
            self.full_img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        # Create dimmed background
        dim_color = Image.new("RGB", self.full_img.size, (15, 23, 42))
        self.dark_img = Image.blend(self.full_img, dim_color, 0.65)

        # Full-screen borderless window spanning virtual desktop
        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{self.vw}x{self.vh}+{self.vx}+{self.vy}")
        self.win.focus_force()

        # Canvas
        self.canvas = tk.Canvas(
            self.win,
            width=self.vw,
            height=self.vh,
            cursor="crosshair",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.tk_dark_bg = ImageTk.PhotoImage(self.dark_img)
        self.bg_item = self.canvas.create_image(0, 0, anchor="nw", image=self.tk_dark_bg)

        # Region coordinates in canvas space
        self.x1 = 0
        self.y1 = 0
        self.x2 = 0
        self.y2 = 0
        self.has_region = False

        # Canvas items
        self.cutout_img_item = None
        self.tk_cutout_img = None
        self.rect_item = None
        self.hud_rect = None
        self.hud_text = None
        self.handles = {}  # name -> item_id

        # Interaction state
        self.drag_mode = None  # 'draw', 'move', or handle name ('nw', 'se', etc.)
        self.start_x = 0
        self.start_y = 0
        self.orig_box = None

        # Floating instruction banner at top center
        self._build_top_banner()

        # Action toolbar (Confirm / Cancel buttons)
        self.toolbar_window = None

        # Bindings
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)

        self.win.bind("<Return>", lambda e: self._confirm())
        self.win.bind("<KP_Enter>", lambda e: self._confirm())
        self.win.bind("<Escape>", lambda e: self._cancel())

        # Arrow keys to nudge selection
        self.win.bind("<Left>", lambda e: self._nudge(-1, 0, e))
        self.win.bind("<Right>", lambda e: self._nudge(1, 0, e))
        self.win.bind("<Up>", lambda e: self._nudge(0, -1, e))
        self.win.bind("<Down>", lambda e: self._nudge(0, 1, e))

        # Initialize with previous region if provided
        if self.initial_region:
            ix = self.initial_region["left"] - self.vx
            iy = self.initial_region["top"] - self.vy
            iw = self.initial_region["width"]
            ih = self.initial_region["height"]
            self._set_box(ix, iy, ix + iw, iy + ih)
            self._render_cutout()

    def _build_top_banner(self):
        banner_w = 640
        banner_h = 42
        bx = (self.vw - banner_w) // 2
        by = 16
        # Background pill
        self.canvas.create_rectangle(
            bx, by, bx + banner_w, by + banner_h,
            fill="#0f172a", outline="#3b82f6", width=1.5
        )
        self.canvas.create_text(
            bx + banner_w // 2, by + banner_h // 2,
            text="📐 Kéo chuột để vẽ vùng quay  •  Kéo bên trong để di chuyển  •  Kéo góc để đổi cỡ\n"
                 "Nhấn Enter / Click đúp để Xác nhận  •  Nhấn Esc để Hủy",
            fill="#f8fafc", font=("Segoe UI", 10, "bold"), justify="center"
        )

    def _box_coords(self):
        x1 = min(self.x1, self.x2)
        y1 = min(self.y1, self.y2)
        x2 = max(self.x1, self.x2)
        y2 = max(self.y1, self.y2)
        return x1, y1, x2, y2

    def _set_box(self, x1, y1, x2, y2):
        self.x1 = max(0, min(self.vw, x1))
        self.y1 = max(0, min(self.vh, y1))
        self.x2 = max(0, min(self.vw, x2))
        self.y2 = max(0, min(self.vh, y2))
        self.has_region = True
        self._update_shapes()

    def _update_shapes(self):
        bx1, by1, bx2, by2 = self._box_coords()
        w = _even(bx2 - bx1)
        h = _even(by2 - by1)

        if w < 16 or h < 16:
            self._clear_shapes()
            return

        # Main rectangle border
        if not self.rect_item:
            self.rect_item = self.canvas.create_rectangle(
                bx1, by1, bx2, by2,
                outline="#00e5ff", width=2
            )
        else:
            self.canvas.coords(self.rect_item, bx1, by1, bx2, by2)
            self.canvas.tag_raise(self.rect_item)

        # Handles (NW, N, NE, E, SE, S, SW, W)
        hs = self.HANDLE_SIZE
        mid_x = (bx1 + bx2) // 2
        mid_y = (by1 + by2) // 2
        coords_map = {
            "nw": (bx1 - hs, by1 - hs, bx1 + hs, by1 + hs),
            "n": (mid_x - hs, by1 - hs, mid_x + hs, by1 + hs),
            "ne": (bx2 - hs, by1 - hs, bx2 + hs, by1 + hs),
            "e": (bx2 - hs, mid_y - hs, bx2 + hs, mid_y + hs),
            "se": (bx2 - hs, by2 - hs, bx2 + hs, by2 + hs),
            "s": (mid_x - hs, by2 - hs, mid_x + hs, by2 + hs),
            "sw": (bx1 - hs, by2 - hs, bx1 + hs, by2 + hs),
            "w": (bx1 - hs, mid_y - hs, bx1 + hs, mid_y + hs),
        }

        for name, c in coords_map.items():
            if name not in self.handles:
                self.handles[name] = self.canvas.create_rectangle(
                    *c, fill="#00e5ff", outline="#090b12", width=1.5
                )
            else:
                self.canvas.coords(self.handles[name], *c)
                self.canvas.tag_raise(self.handles[name])

        # HUD Label
        real_x = self.vx + bx1
        real_y = self.vy + by1
        hud_str = f"📐 {w} × {h}  (X: {real_x}, Y: {real_y})"

        hud_y = by1 - 32 if by1 >= 40 else by2 + 10
        hud_w = 230
        hud_h = 24
        hx1 = max(10, min(self.vw - hud_w - 10, bx1))
        hx2 = hx1 + hud_w
        hy1 = hud_y
        hy2 = hy1 + hud_h

        if not self.hud_rect:
            self.hud_rect = self.canvas.create_rectangle(
                hx1, hy1, hx2, hy2, fill="#0f172a", outline="#00e5ff", width=1.2
            )
            self.hud_text = self.canvas.create_text(
                (hx1 + hx2) // 2, (hy1 + hy2) // 2,
                text=hud_str, fill="#38bdf8", font=("Segoe UI", 9, "bold")
            )
        else:
            self.canvas.coords(self.hud_rect, hx1, hy1, hx2, hy2)
            self.canvas.coords(self.hud_text, (hx1 + hx2) // 2, (hy1 + hy2) // 2)
            self.canvas.itemconfigure(self.hud_text, text=hud_str)
            self.canvas.tag_raise(self.hud_rect)
            self.canvas.tag_raise(self.hud_text)

        self._update_toolbar(bx1, by1, bx2, by2)

    def _update_toolbar(self, bx1, by1, bx2, by2):
        if not self.toolbar_window:
            frame = tk.Frame(self.canvas, bg="#0f172a", bd=1, relief="ridge")
            btn_ok = tk.Button(
                frame, text="✓ Xác nhận", bg="#2563eb", fg="#ffffff",
                activebackground="#1d4ed8", activeforeground="#ffffff",
                font=("Segoe UI", 9, "bold"), bd=0, padx=10, pady=4,
                cursor="hand2", command=self._confirm
            )
            btn_ok.pack(side="left", padx=4, pady=4)

            btn_cancel = tk.Button(
                frame, text="✕ Hủy", bg="#334155", fg="#cbd5e1",
                activebackground="#475569", activeforeground="#ffffff",
                font=("Segoe UI", 9), bd=0, padx=8, pady=4,
                cursor="hand2", command=self._cancel
            )
            btn_cancel.pack(side="left", padx=4, pady=4)

            self.toolbar_frame = frame
            self.toolbar_window = self.canvas.create_window(0, 0, window=frame, anchor="nw")

        tb_x = bx2 - 165
        tb_y = by2 + 10 if by2 + 50 <= self.vh else by2 - 45
        tb_x = max(10, min(self.vw - 180, tb_x))
        self.canvas.coords(self.toolbar_window, tb_x, tb_y)
        self.canvas.tag_raise(self.toolbar_window)

    def _render_cutout(self):
        bx1, by1, bx2, by2 = self._box_coords()
        w = bx2 - bx1
        h = by2 - by1
        if w < 16 or h < 16:
            if self.cutout_img_item:
                self.canvas.delete(self.cutout_img_item)
                self.cutout_img_item = None
            return

        cropped = self.full_img.crop((bx1, by1, bx2, by2))
        self.tk_cutout_img = ImageTk.PhotoImage(cropped)

        if not self.cutout_img_item:
            self.cutout_img_item = self.canvas.create_image(
                bx1, by1, anchor="nw", image=self.tk_cutout_img
            )
        else:
            self.canvas.coords(self.cutout_img_item, bx1, by1)
            self.canvas.itemconfigure(self.cutout_img_item, image=self.tk_cutout_img)

        # Ensure cutout sits right above dark background but below border & handles
        self.canvas.tag_lower(self.cutout_img_item)
        self.canvas.tag_raise(self.cutout_img_item, self.bg_item)
        if self.rect_item:
            self.canvas.tag_raise(self.rect_item)
        for h_id in self.handles.values():
            self.canvas.tag_raise(h_id)
        if self.hud_rect:
            self.canvas.tag_raise(self.hud_rect)
            self.canvas.tag_raise(self.hud_text)
        if self.toolbar_window:
            self.canvas.tag_raise(self.toolbar_window)

    def _clear_shapes(self):
        if self.rect_item:
            self.canvas.delete(self.rect_item)
            self.rect_item = None
        for h_id in self.handles.values():
            self.canvas.delete(h_id)
        self.handles.clear()
        if self.hud_rect:
            self.canvas.delete(self.hud_rect)
            self.canvas.delete(self.hud_text)
            self.hud_rect = None
            self.hud_text = None
        if self.cutout_img_item:
            self.canvas.delete(self.cutout_img_item)
            self.cutout_img_item = None
        if self.toolbar_window:
            self.canvas.delete(self.toolbar_window)
            self.toolbar_window = None

    def _get_hit_target(self, x, y):
        if not self.has_region:
            return None
        bx1, by1, bx2, by2 = self._box_coords()
        hs = self.HANDLE_SIZE + 4

        # Check handles first
        mid_x = (bx1 + bx2) // 2
        mid_y = (by1 + by2) // 2
        targets = {
            "nw": (bx1, by1), "n": (mid_x, by1), "ne": (bx2, by1),
            "e": (bx2, mid_y), "se": (bx2, by2), "s": (mid_x, by2),
            "sw": (bx1, by2), "w": (bx1, mid_y),
        }
        for name, (hx, hy) in targets.items():
            if abs(x - hx) <= hs and abs(y - hy) <= hs:
                return name

        # Check inside rectangle for move
        if bx1 + hs <= x <= bx2 - hs and by1 + hs <= y <= by2 - hs:
            return "move"

        return None

    def _on_hover(self, event):
        target = self._get_hit_target(event.x, event.y)
        if target == "move":
            self.canvas.configure(cursor="fleur")
        elif target in ("nw", "se"):
            self.canvas.configure(cursor="size_nw_se")
        elif target in ("ne", "sw"):
            self.canvas.configure(cursor="size_ne_sw")
        elif target in ("n", "s"):
            self.canvas.configure(cursor="size_ns")
        elif target in ("e", "w"):
            self.canvas.configure(cursor="size_we")
        else:
            self.canvas.configure(cursor="crosshair")

    def _on_press(self, event):
        target = self._get_hit_target(event.x, event.y)
        self.start_x = event.x
        self.start_y = event.y

        if target:
            self.drag_mode = target
            self.orig_box = self._box_coords()
        else:
            self.drag_mode = "draw"
            self.x1 = event.x
            self.y1 = event.y
            self.x2 = event.x
            self.y2 = event.y
            self.has_region = True
            self._update_shapes()

    def _on_motion(self, event):
        if not self.drag_mode:
            return

        cur_x = max(0, min(self.vw, event.x))
        cur_y = max(0, min(self.vh, event.y))
        dx = cur_x - self.start_x
        dy = cur_y - self.start_y

        if self.drag_mode == "draw":
            self.x2 = cur_x
            self.y2 = cur_y
            self._update_shapes()

        elif self.drag_mode == "move" and self.orig_box:
            ox1, oy1, ox2, oy2 = self.orig_box
            w = ox2 - ox1
            h = oy2 - oy1
            nx1 = max(0, min(self.vw - w, ox1 + dx))
            ny1 = max(0, min(self.vh - h, oy1 + dy))
            self.x1, self.y1, self.x2, self.y2 = nx1, ny1, nx1 + w, ny1 + h
            self._update_shapes()

        elif self.orig_box:
            ox1, oy1, ox2, oy2 = self.orig_box
            nx1, ny1, nx2, ny2 = ox1, oy1, ox2, oy2
            m = self.drag_mode
            if "w" in m:
                nx1 = max(0, min(ox2 - 32, ox1 + dx))
            if "e" in m:
                nx2 = min(self.vw, max(ox1 + 32, ox2 + dx))
            if "n" in m:
                ny1 = max(0, min(oy2 - 32, oy1 + dy))
            if "s" in m:
                ny2 = min(self.vh, max(oy1 + 32, oy2 + dy))
            self.x1, self.y1, self.x2, self.y2 = nx1, ny1, nx2, ny2
            self._update_shapes()

    def _on_release(self, _event):
        self.drag_mode = None
        self.orig_box = None
        # Normalize coordinates so x1 <= x2 and y1 <= y2
        bx1, by1, bx2, by2 = self._box_coords()
        self.x1, self.y1, self.x2, self.y2 = bx1, by1, bx2, by2
        self._render_cutout()

    def _nudge(self, dx, dy, event):
        if not self.has_region:
            return
        step = 10 if (event.state & 0x0001) else 1  # Shift for 10px
        bx1, by1, bx2, by2 = self._box_coords()
        w = bx2 - bx1
        h = by2 - by1
        nx1 = max(0, min(self.vw - w, bx1 + dx * step))
        ny1 = max(0, min(self.vh - h, by1 + dy * step))
        self.x1, self.y1, self.x2, self.y2 = nx1, ny1, nx1 + w, ny1 + h
        self._update_shapes()
        self._render_cutout()

    def _on_double_click(self, event):
        target = self._get_hit_target(event.x, event.y)
        if target == "move":
            self._confirm()

    def _confirm(self):
        if not self.has_region:
            self._cancel()
            return
        bx1, by1, bx2, by2 = self._box_coords()
        raw_w = bx2 - bx1
        raw_h = by2 - by1
        if raw_w < 32 or raw_h < 32:
            self._cancel()
            return

        real_x, real_y, w, h = clamp_region_to_desktop(
            self.vx + bx1,
            self.vy + by1,
            raw_w,
            raw_h,
            bounds=(self.vx, self.vy, self.vw, self.vh),
        )

        result = {
            "id": "region:custom",
            "kind": "region",
            "label": f"📐 Vùng quay: {w}×{h} (tại {real_x}, {real_y})",
            "left": real_x,
            "top": real_y,
            "width": w,
            "height": h,
        }

        self.win.destroy()
        if self.on_selected:
            self.on_selected(result)

    def _cancel(self):
        self.win.destroy()
        if self.on_selected:
            self.on_selected(None)


class FloatingRegionFrame:
    """A floating, transparent desktop bounding box with a draggable handle bar."""

    def __init__(self, parent, on_region_changed=None, on_close=None):
        self.parent = parent
        self.on_region_changed = on_region_changed
        self.on_close = on_close
        self.win = None
        self.vx, self.vy, self.vw, self.vh = get_virtual_bounds()
        self.left = 100
        self.top = 100
        self.width = 800
        self.height = 600
        self.bar_height = 28
        self._bar_inside = False
        self.visible = False

        self._dragging = False
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._orig_x = 0
        self._orig_y = 0

    def show(self, left: int, top: int, width: int, height: int):
        self.vx, self.vy, self.vw, self.vh = get_virtual_bounds()
        self.left, self.top, self.width, self.height = clamp_region_to_desktop(
            left, top, width, height, bounds=(self.vx, self.vy, self.vw, self.vh)
        )
        self._bar_inside = self.top < (self.vy + self.bar_height)
        self.visible = True

        if not self.win or not self.win.winfo_exists():
            self._create_window()
        else:
            self._update_geometry()
            self._render_elements()
            self.win.deiconify()

    def hide(self):
        self.visible = False
        self._dragging = False
        if self.win and self.win.winfo_exists():
            self.win.withdraw()

    def destroy(self):
        self.visible = False
        self._dragging = False
        if self.win and self.win.winfo_exists():
            self.win.destroy()
            self.win = None

    def _create_window(self):
        self.win = tk.Toplevel(self.parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        # Transparent key color so user can interact through the center
        self.win.attributes("-transparentcolor", "#010101")
        self.win.configure(bg="#010101")

        self._update_geometry()

        # Canvas with transparent center
        self.canvas = tk.Canvas(
            self.win, bg="#010101", highlightthickness=0, bd=0
        )
        self.canvas.pack(fill="both", expand=True)

        self._render_elements()

    def _update_geometry(self):
        if self._bar_inside:
            self.win.geometry(f"{self.width}x{self.height}+{self.left}+{self.top}")
        else:
            tot_h = self.height + self.bar_height
            self.win.geometry(f"{self.width}x{tot_h}+{self.left}+{self.top - self.bar_height}")

    def _render_elements(self):
        self.canvas.delete("all")
        w = self.width
        h = self.height
        bh = self.bar_height

        bar_w = min(w, 340) if self._bar_inside else w
        rect_top = 0 if self._bar_inside else bh
        rect_bottom = (h - 1) if self._bar_inside else (bh + h - 1)

        # 2px Glowing border around recording box
        self.canvas.create_rectangle(
            1, rect_top, w - 1, rect_bottom,
            outline="#00e5ff", width=2, tags="border"
        )

        # Title / drag handle bar
        self.canvas.create_rectangle(
            0, 0, bar_w, bh, fill="#0f172a", outline="#00e5ff", width=1.5, tags="bar"
        )
        if bar_w < 260:
            title_text = f" ✥ {w}×{h}"
        else:
            title_text = f" ✥ Vùng quay {w}×{h} (Kéo để di chuyển)"
        self.canvas.create_text(
            8, bh // 2, anchor="w",
            text=title_text, fill="#38bdf8", font=("Segoe UI", 9, "bold"), tags="bar"
        )

        # Close button in top-right of bar
        close_x = bar_w - 18
        self.canvas.create_text(
            close_x, bh // 2, anchor="center",
            text="✕", fill="#ef4444", font=("Segoe UI", 10, "bold"), tags="close"
        )

        self.canvas.tag_bind("bar", "<Enter>", lambda _e: self.canvas.configure(cursor="fleur"))
        self.canvas.tag_bind("bar", "<Leave>", lambda _e: self.canvas.configure(cursor="arrow"))
        self.canvas.tag_bind("border", "<Enter>", lambda _e: self.canvas.configure(cursor="fleur"))
        self.canvas.tag_bind("border", "<Leave>", lambda _e: self.canvas.configure(cursor="arrow"))

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

    def _on_press(self, event):
        bar_w = min(self.width, 340) if self._bar_inside else self.width
        # Check if close clicked
        if event.y <= self.bar_height and (bar_w - 32) <= event.x <= bar_w:
            self.hide()
            if self.on_close:
                self.on_close()
            return

        in_bar = event.y <= self.bar_height and event.x <= bar_w
        on_border = (
            event.x <= 8
            or event.x >= self.width - 8
            or event.y >= (self.height if self._bar_inside else self.height + self.bar_height) - 8
        )
        if in_bar or on_border:
            self._dragging = True
            self._drag_start_x = event.x_root
            self._drag_start_y = event.y_root
            self._orig_x = self.left
            self._orig_y = self.top
        else:
            self._dragging = False

    def _on_drag(self, event):
        if not self._dragging:
            return
        dx = event.x_root - self._drag_start_x
        dy = event.y_root - self._drag_start_y

        nx = self._orig_x + dx
        ny = self._orig_y + dy
        self.left = max(self.vx, min(self.vx + self.vw - self.width, int(nx)))
        self.top = max(self.vy, min(self.vy + self.vh - self.height, int(ny)))
        self._update_geometry()

        if self.on_region_changed:
            self.on_region_changed(self.left, self.top, self.width, self.height, final=False)

    def _on_release(self, _event):
        if not self._dragging:
            return
        self._dragging = False
        new_inside = self.top < (self.vy + self.bar_height)
        if new_inside != self._bar_inside:
            self._bar_inside = new_inside
            self._update_geometry()
            self._render_elements()
        if self.on_region_changed:
            self.on_region_changed(self.left, self.top, self.width, self.height, final=True)
