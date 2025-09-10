#!/usr/bin/env python3
import sys,os,io,time,math,traceback,glob
from typing import Optional,Tuple,List
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QSpinBox, QComboBox, QTextEdit,
    QMessageBox, QProgressBar, QPlainTextEdit, QGroupBox, QLineEdit,
    QFormLayout, QColorDialog, QCheckBox, QDoubleSpinBox, QGridLayout,
    QSplitter, QListWidget, QSizePolicy
)
from PyQt6.QtGui import QPixmap, QImage, QIcon, QAction, QColor
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PIL import Image, ImageDraw, ImageFont
import pandas as pd

# ======================================================================
# LRD Image Creator Pro – Updated
# - Auto-fit option added (checkbox + logic) to use background native size
# - Batch/ranges removed completely
# - Layout stretching and size policies fixed so UI behaves responsively
# - Minor bugs fixed (correct crop coordinates in preview)
# - Backward compatible API for CreatorWorker
#
# ======================================================================


def sanitize_filename(name: str) -> str:
    """Remove path-unfriendly characters from a filename-string.

    Keeps alphanumeric, dash, underscore, dot, and space removed to safe
    characters. This ensures files saved by the app are portable.
    """
    return "".join(c for c in name if c not in r'\\/:*?"<>|')


def pil_to_qpixmap(img: Image.Image, max_w: int, max_h: int) -> QPixmap:
    """Convert a PIL image to a QPixmap scaled to fit max_w x max_h.

    Keeps aspect ratio and does an in-memory PNG round-trip.
    """
    buf = io.BytesIO()
    tmp = img.copy()
    tmp.thumbnail((max_w, max_h), Image.LANCZOS)
    tmp.save(buf, format="PNG")
    qimg = QImage.fromData(buf.getvalue(), "PNG")
    return QPixmap.fromImage(qimg)


def inches_from_unit(value: float, unit: str) -> float:
    if unit == "inches":
        return value
    if unit == "cm":
        return value / 2.54
    if unit == "mm":
        return value / 25.4
    return value


# ----------------------------------------------------------------------
# Worker thread that performs the image creation. No batch/range logic
# inside anymore — the worker accepts either a numeric range (start..end)
# or single-text mode and iterates accordingly.
# ----------------------------------------------------------------------
class CreatorWorker(QThread):
    progress = pyqtSignal(int, int)
    preview = pyqtSignal(object)
    done = pyqtSignal(str)
    error = pyqtSignal(str)
    log = pyqtSignal(str)
    start_estimate = pyqtSignal(int)

    def __init__(
        self,
        background_path: str,
        output_folder: str,
        unit: str,
        width_val: float,
        height_val: float,
        dpi: int,
        scale_bg: bool,
        font_path: Optional[str],
        font_size: int,
        font_color: Tuple[int, int, int],
        horiz_align: str,
        vert_align: str,
        padding_left: int,
        padding_right: int,
        padding_top: int,
        padding_bottom: int,
        mode: str,
        custom_text: str,
        start_count: int,
        end_count: int,
        step: int,
        out_ext: str,
        outline: bool,
        base_name: str
    ):
        super().__init__()
        self.background_path = background_path
        self.output_folder = output_folder
        self.unit = unit
        self.width_val = width_val
        self.height_val = height_val
        self.dpi = max(1, int(dpi))
        self.scale_bg = scale_bg
        self.font_path = font_path
        self.font_size = max(4, font_size)
        self.font_color = font_color
        self.halign = horiz_align
        self.valign = vert_align
        self.pl = max(0, int(padding_left))
        self.pr = max(0, int(padding_right))
        self.pt = max(0, int(padding_top))
        self.pb = max(0, int(padding_bottom))
        self.mode = mode
        self.custom_text = custom_text
        self.start_count = start_count
        self.end_count = end_count
        self.step = max(1, int(step))
        self.out_ext = out_ext.lower()
        self.outline = outline
        self.base_name = base_name or "created"
        self._stop = False

    def request_stop(self):
        self._stop = True

    def _load_font(self, size: int):
        try:
            if self.font_path and os.path.isfile(self.font_path):
                return ImageFont.truetype(self.font_path, size)
            for p in [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
                "C:\\Windows\\Fonts\\arial.ttf",
                "/Library/Fonts/Arial.ttf",
            ]:
                if os.path.exists(p):
                    return ImageFont.truetype(p, size)
            return ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()

    def run(self):
        try:
            if not self.background_path or not os.path.isfile(self.background_path):
                self.error.emit("Background image not found.")
                return

            # convert measurement units to inches then to pixels; 0 triggers auto-fit
            w_in = inches_from_unit(self.width_val, self.unit)
            h_in = inches_from_unit(self.height_val, self.unit)
            target_w = max(1, int(round(w_in * self.dpi)))
            target_h = max(1, int(round(h_in * self.dpi)))

            try:
                bg = Image.open(self.background_path).convert("RGBA")
            except Exception as e:
                self.error.emit(f"Cannot open background: {e}")
                return

            # If the user requested auto-fit (width or height effectively zero) then
            # use the background's native pixel dimensions so there is no extra
            # padding/whitespace introduced by using a larger canvas.
            if target_w <= 1 or target_h <= 1:
                target_w = bg.width
                target_h = bg.height
                self.log.emit(f"Auto-fit used background size {target_w}x{target_h}")

            font = self._load_font(self.font_size)

            # Build the list of values/texts to render
            if self.mode == "Text":
                count_values = [self.custom_text]
            else:
                if self.start_count <= self.end_count:
                    count_values = list(range(self.start_count, self.end_count + 1, self.step))
                else:
                    count_values = list(range(self.start_count, self.end_count - 1, -self.step))

            total = len(count_values)
            self.start_estimate.emit(total)
            processed = 0

            for val in count_values:
                if self._stop:
                    self.log.emit("Stop requested.")
                    break

                text = str(val) if self.mode == "Numbers" else self.custom_text

                canvas = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 255))

                # Background placement and scaling
                if self.scale_bg:
                    bg_ratio = bg.width / bg.height
                    tgt_ratio = target_w / target_h
                    if bg_ratio > tgt_ratio:
                        # background is wider: scale height to target and crop sides
                        scale_h = target_h
                        scale_w = int(round(bg.width * (scale_h / bg.height)))
                    else:
                        scale_w = target_w
                        scale_h = int(round(bg.height * (scale_w / bg.width)))

                    bg_resized = bg.resize((scale_w, scale_h), Image.LANCZOS)
                    left = (bg_resized.width - target_w) // 2
                    top = (bg_resized.height - target_h) // 2
                    bg_crop = bg_resized.crop((left, top, left + target_w, top + target_h))
                    canvas.paste(bg_crop, (0, 0))
                else:
                    bg_thumb = bg.copy()
                    bg_thumb.thumbnail((target_w, target_h), Image.LANCZOS)
                    x = (target_w - bg_thumb.width) // 2
                    y = (target_h - bg_thumb.height) // 2
                    canvas.paste(bg_thumb, (x, y), mask=bg_thumb)

                draw = ImageDraw.Draw(canvas)

                bbox = draw.textbbox((0, 0), text, font=font)
                t_w = bbox[2] - bbox[0]
                t_h = bbox[3] - bbox[1]

                x0 = self.pl
                y0 = self.pt
                x1 = target_w - self.pr
                y1 = target_h - self.pb
                avail_w = max(1, x1 - x0)
                avail_h = max(1, y1 - y0)

                # smart downscale font to fit area if necessary
                if (t_w > avail_w or t_h > avail_h) and isinstance(font, ImageFont.FreeTypeFont):
                    cur = self.font_size
                    while (t_w > avail_w or t_h > avail_h) and cur > 6:
                        cur -= 2
                        try:
                            font = self._load_font(cur)
                        except Exception:
                            font = ImageFont.load_default()
                        bbox = draw.textbbox((0, 0), text, font=font)
                        t_w = bbox[2] - bbox[0]
                        t_h = bbox[3] - bbox[1]

                if self.halign == "left":
                    tx = x0
                elif self.halign == "right":
                    tx = x1 - t_w
                else:
                    tx = x0 + (avail_w - t_w) / 2

                if self.valign == "top":
                    ty = y0
                elif self.valign == "bottom":
                    ty = y1 - t_h
                else:
                    ty = y0 + (avail_h - t_h) / 2

                tx = int(round(max(0, tx)))
                ty = int(round(max(0, ty)))

                fill = tuple(self.font_color) + (255,)

                if self.outline:
                    oc = (0, 0, 0, 255)
                    for ox, oy in [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)]:
                        draw.text((tx + ox, ty + oy), text, font=font, fill=oc)

                draw.text((tx, ty), text, font=font, fill=fill)

                safe_val = sanitize_filename(str(val))
                on = f"{sanitize_filename(self.base_name)}_{safe_val}.{self.out_ext}"
                op = os.path.join(self.output_folder, sanitize_filename(on))
                fmt = "PNG" if self.out_ext == "png" else ("TIFF" if self.out_ext == "tiff" else "JPEG")

                try:
                    if fmt == "JPEG":
                        canvas.convert("RGB").save(op, fmt, quality=95, optimize=True)
                    else:
                        canvas.save(op, fmt)
                except Exception as e:
                    self.log.emit(f"Save failed {op}: {e}")
                    self.error.emit(f"Save failed: {e}")
                    return

                processed += 1
                self.progress.emit(processed, total)

                try:
                    self.preview.emit(canvas.copy())
                except Exception:
                    pass

                self.msleep(15)

            # write small excel report if anything was created
            try:
                rows = []
                for v in (count_values[:processed]):
                    safe_v = sanitize_filename(str(v))
                    on = f"{sanitize_filename(self.base_name)}_{safe_v}.{self.out_ext}"
                    full = os.path.join(self.output_folder, sanitize_filename(on))
                    rows.append({"Value": v, "File Name": on, "Full Path": full, "Extension": self.out_ext})
                if rows:
                    excel_path = os.path.join(self.output_folder, "created_images_report.xlsx")
                    pd.DataFrame(rows).to_excel(excel_path, index=False)
                    self.log.emit(f"Excel: {excel_path}")
            except Exception as e:
                self.log.emit(f"Excel error: {e}")

            self.done.emit(self.output_folder)

        except Exception as e:
            tb = traceback.format_exc()
            self.error.emit(f"Worker exception: {e}\n{tb}")


# ----------------------------------------------------------------------
# Main application window
# ----------------------------------------------------------------------
class CreatorApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LRD Image Creator Pro – Pro Edition")
        self.resize(1200, 860)
        try:
            self.setWindowIcon(QIcon("icreat.ico"))
        except Exception:
            pass

        # ---------- Preview label ----------
        self.lbl_preview = QLabel()
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview.setMinimumSize(720, 540)
        self.lbl_preview.setStyleSheet("border:1px solid #bbb;background:#fff;border-radius:8px;")
        self.lbl_preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # ---------- Top controls ----------
        self.load_bg_btn = QPushButton("Load Background")
        self.bg_path_line = QLineEdit()
        self.bg_path_line.setReadOnly(True)

        self.output_folder_btn = QPushButton("Choose Output Folder")
        self.output_folder_line = QLineEdit()
        self.output_folder_line.setReadOnly(True)

        # ---------- Measurement controls ----------
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["inches", "cm", "mm"])

        self.width_input = QDoubleSpinBox()
        self.width_input.setRange(0.0, 1000.0)
        self.width_input.setDecimals(3)
        self.width_input.setValue(2.0)

        self.height_input = QDoubleSpinBox()
        self.height_input.setRange(0.0, 1000.0)
        self.height_input.setDecimals(3)
        self.height_input.setValue(3.0)

        self.dpi_input = QSpinBox()
        self.dpi_input.setRange(72, 1200)
        self.dpi_input.setValue(300)

        self.scale_bg_chk = QCheckBox("Scale background to cover")
        self.scale_bg_chk.setChecked(True)

        # NEW: Auto-fit checkbox — when checked, width/height are ignored and the
        # image native size is used.
        self.auto_fit_chk = QCheckBox("Auto-fit to background (use image size)")
        self.auto_fit_chk.setChecked(False)
        self.auto_fit_chk.stateChanged.connect(lambda s: self._on_auto_fit_changed())

        # ---------- Filename and font controls ----------
        self.base_name_input = QLineEdit()
        self.base_name_input.setPlaceholderText("Base filename, e.g. gold")
        self.base_name_input.setText("created")

        self.font_combo = QComboBox()
        self.font_combo.setEditable(False)

        self.scan_fonts_btn = QPushButton("Scan Fonts")
        self.font_path_line = QLineEdit()
        self.font_path_line.setReadOnly(True)
        self.font_pick_btn = QPushButton("Choose Font File")

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(6, 500)
        self.font_size_spin.setValue(48)

        self.color_btn = QPushButton("Font Color")
        self.font_color_display = QLabel()
        self.font_color_display.setFixedSize(36, 18)
        self.font_color = (0, 0, 0)

        self.h_align_combo = QComboBox()
        self.h_align_combo.addItems(["left", "center", "right"])

        self.v_align_combo = QComboBox()
        self.v_align_combo.addItems(["top", "center", "bottom"])

        # ---------- Padding ----------
        self.pad_left = QSpinBox()
        self.pad_left.setRange(0, 2000)
        self.pad_left.setValue(10)
        self.pad_right = QSpinBox()
        self.pad_right.setRange(0, 2000)
        self.pad_right.setValue(10)
        self.pad_top = QSpinBox()
        self.pad_top.setRange(0, 2000)
        self.pad_top.setValue(10)
        self.pad_bottom = QSpinBox()
        self.pad_bottom.setRange(0, 2000)
        self.pad_bottom.setValue(10)

        # ---------- Mode (numbers or text) ----------
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Numbers", "Text"])

        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("Enter your text here")

        self.start_spin = QSpinBox()
        self.start_spin.setRange(-1000000, 1000000)
        self.start_spin.setValue(1)

        self.end_spin = QSpinBox()
        self.end_spin.setRange(-1000000, 1000000)
        self.end_spin.setValue(10)

        self.step_spin = QSpinBox()
        self.step_spin.setRange(1, 1000000)
        self.step_spin.setValue(1)

        # ---------- Output format and options ----------
        self.format_combo = QComboBox()
        self.format_combo.addItems(["JPG", "PNG", "TIFF"])
        self.outline_chk = QCheckBox("Outline for legibility")
        self.outline_chk.setChecked(True)

        self.create_btn = QPushButton("Create & Generate")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.update_preview_btn = QPushButton("Update Preview")

        self.progress_bar = QProgressBar()
        self.progress_label = QLabel("Progress: 0/0")

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)

        # ---------------- Layout construction ----------------
        top_row = QHBoxLayout()
        top_row.addWidget(self.load_bg_btn)
        top_row.addWidget(self.bg_path_line)
        top_row.addWidget(self.output_folder_btn)
        top_row.addWidget(self.output_folder_line)

        # Measurement box - include auto-fit checkbox
        meas_box = QGroupBox("Measurement")
        meas_layout = QFormLayout()
        meas_layout.addRow("Unit", self.unit_combo)
        meas_layout.addRow("Width (0=auto or use Auto-fit)", self.width_input)
        meas_layout.addRow("Height (0=auto or use Auto-fit)", self.height_input)
        meas_layout.addRow("DPI", self.dpi_input)
        meas_layout.addRow("", self.scale_bg_chk)
        meas_layout.addRow("", self.auto_fit_chk)
        meas_box.setLayout(meas_layout)

        # Font & placement
        font_box = QGroupBox("Font & Placement")
        font_layout = QGridLayout()
        font_layout.addWidget(QLabel("System Fonts"), 0, 0)
        font_layout.addWidget(self.font_combo, 0, 1)
        font_layout.addWidget(self.scan_fonts_btn, 0, 2)
        font_layout.addWidget(QLabel("Font File"), 1, 0)
        font_layout.addWidget(self.font_path_line, 1, 1)
        font_layout.addWidget(self.font_pick_btn, 1, 2)
        font_layout.addWidget(QLabel("Size"), 2, 0)
        font_layout.addWidget(self.font_size_spin, 2, 1)
        font_layout.addWidget(self.color_btn, 2, 2)
        font_layout.addWidget(self.font_color_display, 2, 3)
        font_layout.addWidget(QLabel("H Align"), 3, 0)
        font_layout.addWidget(self.h_align_combo, 3, 1)
        font_layout.addWidget(QLabel("V Align"), 3, 2)
        font_layout.addWidget(self.v_align_combo, 3, 3)
        font_box.setLayout(font_layout)

        # Padding box
        pad_box = QGroupBox("Padding (px)")
        pad_layout = QHBoxLayout()
        for w in [("Left", self.pad_left), ("Right", self.pad_right), ("Top", self.pad_top), ("Bottom", self.pad_bottom)]:
            pad_layout.addWidget(QLabel(w[0]))
            pad_layout.addWidget(w[1])
        pad_box.setLayout(pad_layout)

        # Counter / text box
        counter_box = QGroupBox("Text & Counters")
        counter_layout = QFormLayout()
        counter_layout.addRow("Mode", self.mode_combo)
        counter_layout.addRow("Text", self.text_input)
        hcnt = QHBoxLayout()
        hcnt.addWidget(QLabel("Start"))
        hcnt.addWidget(self.start_spin)
        hcnt.addWidget(QLabel("End"))
        hcnt.addWidget(self.end_spin)
        hcnt.addWidget(QLabel("Step"))
        hcnt.addWidget(self.step_spin)
        counter_layout.addRow(hcnt)
        counter_layout.addRow("Base Filename", self.base_name_input)
        counter_box.setLayout(counter_layout)

        # Output / action row
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Save as"))
        out_row.addWidget(self.format_combo)
        out_row.addStretch()
        out_row.addWidget(self.outline_chk)
        out_row.addWidget(self.update_preview_btn)
        out_row.addWidget(self.create_btn)
        out_row.addWidget(self.cancel_btn)

        # Right column: stack of boxes
        right_col = QVBoxLayout()
        right_col.addWidget(meas_box)
        right_col.addWidget(font_box)
        right_col.addWidget(pad_box)
        right_col.addWidget(counter_box)
        right_col.addLayout(out_row)
        right_col.addWidget(self.progress_bar)
        right_col.addWidget(self.progress_label)
        right_col.addWidget(QLabel("Log"))
        right_col.addWidget(self.log, 1)

        # Left column: controls + preview
        left_col = QVBoxLayout()
        left_col.addLayout(top_row)
        left_col.addWidget(self.lbl_preview, 1)

        # Main split: give explicit stretch factors to prevent weird stretching
        main = QHBoxLayout()
        main.addLayout(left_col, 3)
        main.addLayout(right_col, 2)
        main.setStretch(0, 3)
        main.setStretch(1, 2)

        central = QWidget()
        central.setLayout(main)
        self.setCentralWidget(central)

        # Menubar
        menubar = self.menuBar()
        help_menu = menubar.addMenu("Help")
        about_act = QAction("About / Contact", self)
        about_act.triggered.connect(self.show_about)
        help_menu.addAction(about_act)

        # State
        self.background_path = None
        self.output_folder = None
        self.worker = None
        self.start_time = None
        self.total_est = 0
        self.processed_count = 0
        self.font_map = []

        # ETA and preview timers
        self.eta_timer = QTimer(self)
        self.eta_timer.setInterval(1000)
        self.eta_timer.timeout.connect(self._update_eta)

        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(250)
        self.preview_timer.timeout.connect(self.update_preview)

        # Signals
        self.load_bg_btn.clicked.connect(self.load_background)
        self.output_folder_btn.clicked.connect(self.choose_output_folder)
        self.font_pick_btn.clicked.connect(self.choose_font)
        self.scan_fonts_btn.clicked.connect(self.scan_system_fonts)
        self.font_combo.currentIndexChanged.connect(self._choose_font_from_combo)
        self.color_btn.clicked.connect(self.pick_color)
        self.create_btn.clicked.connect(self.start_create)
        self.cancel_btn.clicked.connect(self.cancel_create)
        self.update_preview_btn.clicked.connect(self.update_preview)
        self.mode_combo.currentIndexChanged.connect(lambda *_: self._on_mode_change())

        # Connect many UI changes to a delayed preview update to keep UI responsive
        for w in [
            self.unit_combo,
            self.width_input,
            self.height_input,
            self.dpi_input,
            self.scale_bg_chk,
            self.font_size_spin,
            self.h_align_combo,
            self.v_align_combo,
            self.pad_left,
            self.pad_right,
            self.pad_top,
            self.pad_bottom,
            self.mode_combo,
            self.text_input,
            self.start_spin,
            self.end_spin,
            self.step_spin,
            self.base_name_input,
            self.format_combo,
            self.outline_chk,
            self.auto_fit_chk,
        ]:
            sig = getattr(w, 'valueChanged', None) or getattr(w, 'currentIndexChanged', None) or getattr(w, 'stateChanged', None) or getattr(w, 'textChanged', None)
            if sig:
                sig.connect(lambda *_: self.preview_timer.start())

        self._update_preview_blank()
        self.setStyleSheet("QGroupBox{font-weight:600;} QPushButton{padding:6px 10px;} QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox{padding:4px;}")

    def log_msg(self, txt: str):
        ts = time.strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{ts}] {txt}")

    def show_about(self):
        QMessageBox.information(
            self,
            "About / Contact",
            "LRD Image Creator Pro – Pro Edition\nDeveloper: LRD_SOUL (INS-SOUL)\nEmail: inscreator728@gmail.com\nTelegram: @LRD_SOUL",
        )

    def load_background(self):
        f, _ = QFileDialog.getOpenFileName(self, "Open background image", "", "Images (*.png *.jpg *.jpeg *.tiff *.tif)")
        if not f:
            return
        self.background_path = f
        self.bg_path_line.setText(f)
        self.log_msg(f"Background: {f}")
        try:
            img = Image.open(f)
            pix = pil_to_qpixmap(img, self.lbl_preview.width(), self.lbl_preview.height())
            self.lbl_preview.setPixmap(pix)
        except Exception as e:
            self.log_msg(f"Preview error: {e}")
            self.lbl_preview.setText("Preview unavailable")
        self.preview_timer.start()

    def choose_output_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output folder (or empty to use background folder)")
        if not d:
            return
        self.output_folder = d
        self.output_folder_line.setText(d)
        self.log_msg(f"Output: {d}")

    def choose_font(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose font file", "", "Fonts (*.ttf *.otf)")
        if f:
            self.font_path_line.setText(f)
            self.log_msg(f"Font file: {f}")
            self.preview_timer.start()

    def scan_system_fonts(self):
        roots = [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.local/share/fonts"),
            "C:/Windows/Fonts",
            "/Library/Fonts",
            "/System/Library/Fonts",
        ]
        exts = ("*.ttf", "*.otf")
        found = []
        for r in roots:
            if os.path.isdir(r):
                for e in exts:
                    found += glob.glob(os.path.join(r, "**", e), recursive=True)
        found = sorted(set(found))
        self.font_map = found
        self.font_combo.clear()
        for p in found:
            self.font_combo.addItem(os.path.basename(p))
        self.log_msg(f"Fonts found: {len(found)}")

    def _choose_font_from_combo(self):
        i = self.font_combo.currentIndex()
        if 0 <= i < len(self.font_map):
            self.font_path_line.setText(self.font_map[i])
            self.preview_timer.start()

    def pick_color(self):
        col = QColorDialog.getColor(QColor(0, 0, 0), self, "Pick font color")
        if col.isValid():
            c = col.toRgb()
            self.font_color = (c.red(), c.green(), c.blue())
            self.font_color_display.setStyleSheet(f"background: rgb({c.red()},{c.green()},{c.blue()}); border:1px solid #333;")
            self.log_msg(f"Font color: {self.font_color}")
            self.preview_timer.start()

    def _update_preview_blank(self):
        img = Image.new("RGBA", (800, 600), (255, 255, 255, 255))
        pix = pil_to_qpixmap(img, self.lbl_preview.width(), self.lbl_preview.height())
        self.lbl_preview.setPixmap(pix)

    def _on_auto_fit_changed(self):
        # When auto-fit is enabled, disable numeric width/height inputs to avoid
        # accidental user input. We still allow manual zero values to be set.
        if self.auto_fit_chk.isChecked():
            self.width_input.setEnabled(False)
            self.height_input.setEnabled(False)
            # visually set them to 0 to indicate auto behaviour
            self.width_input.setValue(0.0)
            self.height_input.setValue(0.0)
        else:
            self.width_input.setEnabled(True)
            self.height_input.setEnabled(True)
        self.preview_timer.start()

    def _collect_params(self):
        unit = self.unit_combo.currentText()
        w = float(self.width_input.value())
        h = float(self.height_input.value())
        # if auto-fit checkbox is checked, explicitly set w/h to 0.0
        if self.auto_fit_chk.isChecked():
            w = 0.0
            h = 0.0
        dpi = int(self.dpi_input.value())
        scale_bg = bool(self.scale_bg_chk.isChecked())
        font_path = self.font_path_line.text() or None
        font_size = int(self.font_size_spin.value())
        font_color = self.font_color
        halign = self.h_align_combo.currentText()
        valign = self.v_align_combo.currentText()
        pl = int(self.pad_left.value())
        pr = int(self.pad_right.value())
        pt = int(self.pad_top.value())
        pb = int(self.pad_bottom.value())
        mode = self.mode_combo.currentText()
        custom_text = self.text_input.text() or ""
        start = int(self.start_spin.value())
        end = int(self.end_spin.value())
        step = int(self.step_spin.value())
        out_ext = self.format_combo.currentText().lower()
        outline = self.outline_chk.isChecked()
        base = self.base_name_input.text().strip() or "created"
        return (
            unit,
            w,
            h,
            dpi,
            scale_bg,
            font_path,
            font_size,
            font_color,
            halign,
            valign,
            pl,
            pr,
            pt,
            pb,
            mode,
            custom_text,
            start,
            end,
            step,
            out_ext,
            outline,
            base,
        )

    def update_preview(self):
        if not self.background_path or not os.path.isfile(self.background_path):
            return
        (
            unit,
            w,
            h,
            dpi,
            scale_bg,
            font_path,
            font_size,
            font_color,
            halign,
            valign,
            pl,
            pr,
            pt,
            pb,
            mode,
            custom_text,
            start,
            end,
            step,
            out_ext,
            outline,
            base,
        ) = self._collect_params()

        try:
            bg = Image.open(self.background_path).convert("RGBA")
        except Exception:
            return

        w_in = inches_from_unit(w, unit)
        h_in = inches_from_unit(h, unit)
        tw = max(1, int(round(w_in * dpi)))
        th = max(1, int(round(h_in * dpi)))

        # If auto-fit is in effect (either via zero measurements or auto-fit checkbox)
        # override the target size with the background native dimensions so the
        # preview shows the exact pixel output the worker will produce.
        if tw <= 1 or th <= 1:
            tw, th = bg.width, bg.height

        canvas = Image.new("RGBA", (tw, th), (255, 255, 255, 255))

        if scale_bg:
            br = bg.width / bg.height
            tr = tw / th
            if br > tr:
                sh = th
                sw = int(round(bg.width * (sh / bg.height)))
            else:
                sw = tw
                sh = int(round(bg.height * (sw / bg.width)))
            bgr = bg.resize((sw, sh), Image.LANCZOS)
            left = (bgr.width - tw) // 2
            top = (bgr.height - th) // 2
            crop = bgr.crop((left, top, left + tw, top + th))
            canvas.paste(crop, (0, 0))
        else:
            bt = bg.copy()
            bt.thumbnail((tw, th), Image.LANCZOS)
            x = (tw - bt.width) // 2
            y = (th - bt.height) // 2
            canvas.paste(bt, (x, y), mask=bt)

        # build preview text based on mode (Text -> custom_text, Numbers -> first value)
        if mode == "Text":
            text = str(custom_text)
        else:
            text = f"{start}"

        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        t_w = bbox[2] - bbox[0]
        t_h = bbox[3] - bbox[1]

        x0 = pl
        y0 = pt
        x1 = tw - pr
        y1 = th - pb
        aw = max(1, x1 - x0)
        ah = max(1, y1 - y0)

        if (t_w > aw or t_h > ah) and isinstance(font, ImageFont.FreeTypeFont):
            cur = font_size
            while (t_w > aw or t_h > ah) and cur > 6:
                cur -= 2
                try:
                    font = ImageFont.truetype(font_path, cur) if font_path else ImageFont.load_default()
                except Exception:
                    font = ImageFont.load_default()
                bbox = draw.textbbox((0, 0), text, font=font)
                t_w = bbox[2] - bbox[0]
                t_h = bbox[3] - bbox[1]

        if halign == "left":
            tx = x0
        elif halign == "right":
            tx = x1 - t_w
        else:
            tx = x0 + (aw - t_w) / 2

        if valign == "top":
            ty = y0
        elif valign == "bottom":
            ty = y1 - t_h
        else:
            ty = y0 + (ah - t_h) / 2

        tx = int(round(max(0, tx)))
        ty = int(round(max(0, ty)))

        if self.outline_chk.isChecked():
            oc = (0, 0, 0, 255)
            for ox, oy in [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)]:
                draw.text((tx + ox, ty + oy), text, font=font, fill=oc)
        draw.text((tx, ty), text, font=font, fill=tuple(font_color) + (255,))

        pix = pil_to_qpixmap(canvas, self.lbl_preview.width(), self.lbl_preview.height())
        self.lbl_preview.setPixmap(pix)
        self.log_msg("Preview updated")

    def start_create(self):
        if not self.background_path or not os.path.isfile(self.background_path):
            QMessageBox.warning(self, "Missing background", "Load a background image.")
            return
        out_folder = self.output_folder or os.path.join(os.path.dirname(self.background_path), "Created")
        try:
            os.makedirs(out_folder, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Cannot create output folder: {e}")
            return

        (
            unit,
            w,
            h,
            dpi,
            scale_bg,
            font_path,
            font_size,
            font_color,
            halign,
            valign,
            pl,
            pr,
            pt,
            pb,
            mode,
            custom_text,
            start,
            end,
            step,
            out_ext,
            outline,
            base,
        ) = self._collect_params()

        # ensure when mode is Text we only generate single image using custom_text
        if mode == "Text":
            start = 0
            end = 0
            step = 1

        self.worker = CreatorWorker(
            self.background_path,
            out_folder,
            unit,
            w,
            h,
            dpi,
            scale_bg,
            font_path,
            font_size,
            font_color,
            halign,
            valign,
            pl,
            pr,
            pt,
            pb,
            mode,
            custom_text,
            start,
            end,
            step,
            out_ext,
            outline,
            base,
        )
        self.worker.start_estimate.connect(self._on_start_estimate)
        self.worker.preview.connect(self._on_preview)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.error.connect(self._on_error)
        self.worker.log.connect(lambda m: self.log_msg(m))

        self.progress_bar.setValue(0)
        self.progress_label.setText("Progress: 0/0")
        self.processed_count = 0
        self.total_est = 0
        self.start_time = time.time()
        self.eta_timer.start()
        self.create_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.worker.start()
        self.log_msg("Generation started")

    def cancel_create(self):
        if self.worker and self.worker.isRunning():
            self.worker.request_stop()
            self.log_msg("Cancel requested")
            self.cancel_btn.setEnabled(False)

    def _on_start_estimate(self, total_est):
        self.total_est = total_est
        self.progress_bar.setMaximum(total_est if total_est > 0 else 1)
        self.log_msg(f"Estimated: {total_est}")

    def _on_preview(self, pil_img):
        try:
            pix = pil_to_qpixmap(pil_img, self.lbl_preview.width(), self.lbl_preview.height())
            self.lbl_preview.setPixmap(pix)
        except Exception as e:
            self.log_msg(f"Preview error: {e}")

    def _on_progress(self, processed: int, total: int):
        self.processed_count = processed
        self.total_est = total
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(processed)
        else:
            self.progress_bar.setValue(processed)
        self.progress_label.setText(f"Progress: {processed}/{total}")

    def _on_done(self, out_folder: str):
        if self.eta_timer.isActive():
            self.eta_timer.stop()
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.log_msg("Generation finished")
        QMessageBox.information(self, "Done", f"Saved in:\n{out_folder}")
        self.create_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def _on_error(self, msg: str):
        if self.eta_timer.isActive():
            self.eta_timer.stop()
        self.log_msg(f"ERROR: {msg}")
        QMessageBox.critical(self, "Error", msg)
        self.create_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def _update_eta(self):
        if not self.start_time or not self.total_est or self.total_est <= 0:
            return
        elapsed = time.time() - self.start_time
        processed = max(1, self.processed_count)
        avg = elapsed / processed
        remaining = max(0, (self.total_est - self.processed_count) * avg)
        mins, secs = divmod(int(remaining), 60)
        pct = (self.processed_count / self.total_est) * 100 if self.total_est else 0.0
        self.progress_label.setText(f"Progress: {self.processed_count}/{self.total_est} ({pct:.2f}%) | Time Left: {mins}m {secs}s")

    def _on_mode_change(self):
        mode = self.mode_combo.currentText()
        is_text = (mode == "Text")
        self.text_input.setVisible(is_text)
        self.start_spin.setVisible(not is_text)
        self.end_spin.setVisible(not is_text)
        self.step_spin.setVisible(not is_text)
        self.preview_timer.start()


# ======================================================================
# Application entry point
# ======================================================================

def main():
    app = QApplication(sys.argv)
    w = CreatorApp()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()



# ===========================
