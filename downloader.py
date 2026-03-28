import sys
import os
import subprocess
import platform
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QFileDialog, QLabel, QComboBox, QCheckBox,
                               QMessageBox, QGroupBox, QTextEdit,
                               QFormLayout, QDoubleSpinBox, QSlider, QLineEdit, QTabWidget)
from PySide6.QtCore import QThread, Signal, Qt, QUrl, qInstallMessageHandler
from PySide6.QtGui import QTextCursor, QPainter, QColor, QPalette, QIcon

def qt_message_handler(mode, context, message):
    if "Late SEI is not implemented" in message:
        return
    if "If you want to help, upload a sample" in message:
        return
    if "[h264 @" in message:
        return
    print(message)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

os.environ["QT_LOGGING_RULES"] = "qt.multimedia.ffmpeg*=false;qt.multimedia.ffmpeg.libav*=false"

# --- WORKER: DOWNLOADER (yt-dlp) ---
class DownloadWorker(QThread):
    progress = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.process = None

    def run(self):
        try:
            self.progress.emit(f">> Starting Download: {self.url}")
            self.progress.emit(">> Forcing H.264 (Safe Mode) for preview compatibility...")

            # 1. Get Filename First
            cmd_name = ["yt-dlp", "--get-filename", "-o", "%(title)s.%(ext)s", "--restrict-filenames", self.url]
            name_proc = subprocess.run(cmd_name, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='ignore')
            filename = name_proc.stdout.strip()
            # Force mp4 for the intermediate preview file
            filename = os.path.splitext(filename)[0] + ".mp4"

            # 2. Download Command
            cmd = [
                "yt-dlp",
                "-S", "vcodec:h264,res,acodec:m4a",
                "-o", "%(title)s.%(ext)s",
                "--restrict-filenames",
                self.url
            ]

            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace', startupinfo=startupinfo
            )

            for line in self.process.stdout:
                line = line.strip()
                if line:
                    if "[download]" in line: self.progress.emit(line)

            self.process.wait()

            if self.process.returncode == 0:
                if os.path.exists(filename):
                    self.finished.emit(True, filename)
                else:
                    self.finished.emit(False, "Download finished but file not found.")
            else:
                self.finished.emit(False, "yt-dlp returned error or was stopped.")

        except Exception as e:
            self.finished.emit(False, str(e))

    def stop(self):
        if self.process:
            self.process.terminate()

# --- WORKER: CONVERTER (FFmpeg) ---
class ConversionWorker(QThread):
    finished = Signal(bool, str)
    log_output = Signal(str)

    def __init__(self, command):
        super().__init__()
        self.command = command
        self.process = None
        self.is_cancelled = False

    def run(self):
        try:
            self.log_output.emit(f">> COMMAND:\n{' '.join(self.command)}\n")
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            self.process = subprocess.Popen(
                self.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, startupinfo=startupinfo, encoding='utf-8', errors='replace'
            )

            while True:
                if self.is_cancelled:
                    self.process.kill()
                    break

                line = self.process.stdout.readline()
                if not line and self.process.poll() is not None:
                    break
                if line:
                    line_str = line.strip()
                    if "Late SEI is not implemented" in line_str: continue
                    if "If you want to help, upload a sample" in line_str: continue
                    if "[h264 @" in line_str: continue
                    self.log_output.emit(line_str)

            if self.is_cancelled:
                self.finished.emit(False, "Export Cancelled by User.")
            elif self.process.returncode == 0:
                self.finished.emit(True, "Conversion Complete!")
            else:
                self.finished.emit(False, "FFmpeg Error (Check Log)")

        except Exception as e:
            self.finished.emit(False, str(e))

    def stop(self):
        self.is_cancelled = True
        if self.process:
            self.process.kill()

# --- WIDGET: VISUAL TIMELINE BAR ---
class RangeBar(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(15)
        self.duration = 100
        self.start_pos = 0
        self.end_pos = 100

    def update_range(self, start, end, duration):
        self.start_pos = start
        self.end_pos = end
        self.duration = duration if duration > 0 else 100
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        width = self.width()
        painter.fillRect(0, 0, width, self.height(), QColor("#333333")) # Background
        if self.duration <= 0: return

        x1 = int((self.start_pos / self.duration) * width)
        x2 = int((self.end_pos / self.duration) * width)
        x1 = max(0, min(x1, width))
        x2 = max(0, min(x2, width))
        w_rect = max(x2 - x1, 2)

        painter.fillRect(x1, 0, w_rect, self.height(), QColor("#0078D7")) # Selection

# --- MAIN APP ---
class VideoEditorApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pro Video Suite (Downloader + Editor)")
        self.resize(1000, 950)

        # Data
        self.input_file = None
        self.fps = 30.0
        self.duration_ms = 0
        self.start_ms = 0
        self.end_ms = 0
        self.was_playing_before_scrub = False
        self.loop_enabled = False

        # UI Setup
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Tab 1: Downloader
        self.tab_download = QWidget()
        self.setup_downloader_tab()
        self.tabs.addTab(self.tab_download, "1. Download")

        # Tab 2: Editor
        self.tab_editor = QWidget()
        self.setup_editor_tab()
        self.tabs.addTab(self.tab_editor, "2. Editor")

        # Audio/Video Backend
        self.setup_player()

    # ==========================
    # TAB 1: DOWNLOADER SETUP
    # ==========================
    def setup_downloader_tab(self):
        layout = QVBoxLayout()
        layout.setSpacing(15)
        layout.setContentsMargins(40, 40, 40, 40)

        lbl_title = QLabel("YouTube Downloader")
        lbl_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #0078D7;")
        layout.addWidget(lbl_title)

        input_group = QGroupBox("Video URL")
        ig_layout = QVBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste YouTube link here...")
        self.url_input.setStyleSheet("padding: 8px; font-size: 14px; background-color: #222; color: white; border: 1px solid #555;")
        ig_layout.addWidget(self.url_input)
        input_group.setLayout(ig_layout)
        layout.addWidget(input_group)

        self.btn_download = QPushButton("DOWNLOAD & LOAD")
        self.btn_download.setFixedHeight(50)
        self.btn_download.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; font-size: 14px;")
        self.btn_download.clicked.connect(self.start_download)
        layout.addWidget(self.btn_download)

        self.dl_console = QTextEdit()
        self.dl_console.setReadOnly(True)
        self.dl_console.setStyleSheet("background-color: #111; color: #0f0; font-family: Consolas; font-size: 11px;")
        layout.addWidget(self.dl_console)

        self.tab_download.setLayout(layout)

    # ==========================
    # TAB 2: EDITOR SETUP
    # ==========================
    def setup_editor_tab(self):
        main_layout = QVBoxLayout()
        main_layout.setSpacing(10)

        # Video Preview
        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background-color: #000; border: 1px solid #333;")
        self.video_widget.setMinimumHeight(380)
        main_layout.addWidget(self.video_widget)

        # Timeline
        timeline_group = QGroupBox("Timeline")
        timeline_group.setStyleSheet("QGroupBox { border: 1px solid #333; margin-top: 10px; padding-top: 10px; font-weight: bold; color: #ccc; }")
        t_layout = QVBoxLayout()

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderPressed.connect(self.slider_pressed)
        self.slider.sliderReleased.connect(self.slider_released)
        self.slider.sliderMoved.connect(self.set_position)
        self.slider.setStyleSheet("QSlider::groove:horizontal { height: 4px; background: #333; } QSlider::handle:horizontal { background: #ddd; width: 12px; margin: -4px 0; border-radius: 6px; }")
        t_layout.addWidget(self.slider)

        self.range_bar = RangeBar()
        t_layout.addWidget(self.range_bar)

        c_layout = QHBoxLayout()
        self.btn_play = QPushButton("Play")
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_play.setFixedWidth(80)
        c_layout.addWidget(self.btn_play)

        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setStyleSheet("color: #888; font-family: Consolas;")
        c_layout.addWidget(self.lbl_time)
        c_layout.addStretch()

        self.btn_in = QPushButton("[ Set IN ]")
        self.btn_in.clicked.connect(self.set_in_point)
        self.btn_in.setStyleSheet("background-color: #2e4d34; color: #8fbc8f; border: none; padding: 6px 12px; border-radius: 3px;")

        self.btn_out = QPushButton("[ Set OUT ]")
        self.btn_out.clicked.connect(self.set_out_point)
        self.btn_out.setStyleSheet("background-color: #4d2e2e; color: #bc8f8f; border: none; padding: 6px 12px; border-radius: 3px;")

        self.btn_reset = QPushButton("Reset")
        self.btn_reset.clicked.connect(self.reset_cut)
        self.btn_reset.setStyleSheet("background-color: #333; color: #aaa; border: none; padding: 6px 12px; border-radius: 3px;")

        c_layout.addWidget(self.btn_in)
        c_layout.addWidget(self.btn_out)
        c_layout.addWidget(self.btn_reset)
        t_layout.addLayout(c_layout)

        self.lbl_trim_info = QLabel("Export Range: Full Video")
        self.lbl_trim_info.setAlignment(Qt.AlignCenter)
        self.lbl_trim_info.setStyleSheet("color: #666; font-size: 11px; margin-top: 5px;")
        t_layout.addWidget(self.lbl_trim_info)

        timeline_group.setLayout(t_layout)
        main_layout.addWidget(timeline_group)

        # Settings
        settings_group = QGroupBox("Configuration")
        settings_group.setStyleSheet("QGroupBox { border: 1px solid #333; margin-top: 10px; padding-top: 10px; font-weight: bold; color: #ccc; }")
        s_layout = QHBoxLayout()

        col1 = QVBoxLayout()
        self.btn_browse = QPushButton("Open Video File...")
        self.btn_browse.clicked.connect(self.browse_file)
        self.btn_browse.setStyleSheet("background-color: #444; color: white; border: 1px solid #555; border-radius: 4px;")
        col1.addWidget(self.btn_browse)

        name_layout = QHBoxLayout()
        lbl_name = QLabel("Output Name:")
        lbl_name.setStyleSheet("color: #aaa;")
        self.txt_output_name = QLineEdit()
        self.txt_output_name.setPlaceholderText("File name (no extension)")
        self.txt_output_name.setStyleSheet("background-color: #222; color: white; border: 1px solid #444; padding: 4px;")
        # No label for extension here, as it changes dynamically
        name_layout.addWidget(lbl_name)
        name_layout.addWidget(self.txt_output_name)
        col1.addLayout(name_layout)

        self.chk_gop = QCheckBox("Force Smart Keyframes")
        self.chk_gop.setChecked(True)
        col1.addWidget(self.chk_gop)
        s_layout.addLayout(col1)

        col2 = QFormLayout()
        self.combo_encoder = QComboBox()
        self.populate_encoders()
        col2.addRow("Format:", self.combo_encoder)

        self.combo_speed = QComboBox()
        self.combo_speed.addItems(["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"])
        self.combo_speed.setCurrentText("medium")
        col2.addRow("Speed:", self.combo_speed)

        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["Auto Quality (CRF)", "Target Size (MB)"])
        self.combo_mode.currentIndexChanged.connect(self.toggle_mode)
        col2.addRow("Mode:", self.combo_mode)

        self.spin_size = QDoubleSpinBox()
        self.spin_size.setRange(1, 5000)
        self.spin_size.setValue(95.0)
        self.spin_size.setEnabled(False)
        self.spin_size.setSuffix(" MB")
        col2.addRow("Size:", self.spin_size)

        s_layout.addLayout(col2)
        settings_group.setLayout(s_layout)
        main_layout.addWidget(settings_group)

        # Action Buttons
        btn_layout = QHBoxLayout()
        self.btn_run = QPushButton("EXPORT")
        self.btn_run.setFixedHeight(50)
        self.btn_run.setStyleSheet("QPushButton { background-color: #0078D7; color: white; font-weight: bold; font-size: 15px; } QPushButton:hover { background-color: #008ae6; } QPushButton:disabled { background-color: #333; color: #555; }")
        self.btn_run.setEnabled(False)
        self.btn_run.clicked.connect(self.start_encoding)
        btn_layout.addWidget(self.btn_run)

        self.btn_stop = QPushButton("CANCEL")
        self.btn_stop.setFixedHeight(50)
        self.btn_stop.setFixedWidth(100)
        self.btn_stop.setStyleSheet("QPushButton { background-color: #7d2e2e; color: white; font-weight: bold; font-size: 15px; } QPushButton:hover { background-color: #a63d3d; }")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_encoding)
        btn_layout.addWidget(self.btn_stop)
        main_layout.addLayout(btn_layout)

        self.console = QTextEdit()
        self.console.setMaximumHeight(80)
        self.console.setReadOnly(True)
        self.console.setStyleSheet("background-color: #111; color: #0f0; font-family: Consolas; font-size: 10px; border: 1px solid #333;")
        main_layout.addWidget(self.console)

        self.tab_editor.setLayout(main_layout)

    # ==========================
    # DOWNLOAD LOGIC
    # ==========================
    def start_download(self):
        url = self.url_input.text().strip()
        if not url:
            self.dl_console.append(">> Error: Please enter a URL.")
            return

        self.btn_download.setEnabled(False)
        self.dl_console.clear()

        self.dl_worker = DownloadWorker(url)
        self.dl_worker.progress.connect(self.dl_console.append)
        self.dl_worker.finished.connect(self.on_download_complete)
        self.dl_worker.start()

    def on_download_complete(self, success, result):
        self.btn_download.setEnabled(True)
        if success:
            self.dl_console.append(f">> SUCCESS: Downloaded {result}")
            self.load_video_file(result)
            self.tabs.setCurrentIndex(1)
            QMessageBox.information(self, "Download Complete", f"Loaded: {result}\n\nSwitched to Editor tab.")
        else:
            self.dl_console.append(f">> FAILED: {result}")
            QMessageBox.critical(self, "Error", result)

    # ==========================
    # EDITOR LOGIC
    # ==========================
    def setup_player(self):
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.durationChanged.connect(self.duration_changed)
        self.player.positionChanged.connect(self.position_changed)
        self.player.mediaStatusChanged.connect(self.media_status_changed)

    def browse_file(self):
        file, _ = QFileDialog.getOpenFileName(self, "Open Video", "", "Videos (*.mp4 *.mov *.mkv *.webm *.avi)")
        if file:
            self.load_video_file(file)

    def load_video_file(self, filepath):
        self.input_file = filepath
        self.player.setSource(QUrl.fromLocalFile(filepath))
        self.player.play()
        self.btn_play.setText("Pause")
        self.btn_run.setEnabled(True)

        filename = os.path.basename(filepath)
        self.btn_browse.setText(f"Loaded: {filename}")

        base_name = os.path.splitext(filename)[0]
        self.txt_output_name.setText(f"{base_name}_edit")

        self.detect_fps()
        self.log(f">> Loaded: {filename}")

    def duration_changed(self, duration):
        self.duration_ms = duration
        self.slider.setRange(0, duration)
        self.end_ms = duration
        self.start_ms = 0
        self.update_range_ui()

    def position_changed(self, position):
        if not self.slider.isSliderDown():
            self.slider.setValue(position)
        self.update_time_label(position)

        # Loop Logic
        if self.loop_enabled and self.player.playbackState() == QMediaPlayer.PlayingState:
            if position >= self.end_ms:
                self.player.setPosition(self.start_ms)

    def update_time_label(self, current_ms):
        def fmt(ms):
            s = (ms // 1000) % 60
            m = (ms // 60000)
            return f"{m:02}:{s:02}"
        self.lbl_time.setText(f"{fmt(current_ms)} / {fmt(self.duration_ms)}")

    def slider_pressed(self):
        self.was_playing_before_scrub = (self.player.playbackState() == QMediaPlayer.PlayingState)
        self.player.pause()

    def slider_released(self):
        self.player.setPosition(self.slider.value())
        if self.was_playing_before_scrub:
            self.player.play()

    def set_position(self, p): self.update_time_label(p)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.btn_play.setText("Play")
        else:
            self.player.play()
            self.btn_play.setText("Pause")

    def media_status_changed(self, status):
        if status == QMediaPlayer.EndOfMedia and not self.loop_enabled:
            self.btn_play.setText("Play")

    def set_in_point(self):
        self.start_ms = self.player.position()
        if self.start_ms >= self.end_ms: self.end_ms = self.duration_ms
        self.loop_enabled = True
        self.update_range_ui()
        self.log(f">> Cut Start: {self.start_ms/1000:.2f}s (Loop Active)")

    def set_out_point(self):
        self.end_ms = self.player.position()
        if self.end_ms <= self.start_ms: self.start_ms = 0
        self.loop_enabled = True
        self.update_range_ui()
        self.log(f">> Cut End: {self.end_ms/1000:.2f}s (Loop Active)")

    def reset_cut(self):
        self.start_ms = 0
        self.end_ms = self.duration_ms
        self.loop_enabled = False
        self.update_range_ui()
        self.log(">> Range Reset")

    def update_range_ui(self):
        self.range_bar.update_range(self.start_ms, self.end_ms, self.duration_ms)
        s = self.start_ms / 1000.0
        e = self.end_ms / 1000.0
        self.lbl_trim_info.setText(f"Trim: {s:.2f}s to {e:.2f}s (Duration: {e-s:.2f}s)")

    def populate_encoders(self):
        system = platform.system()
        self.combo_encoder.clear()
        # Video Options
        if system == "Windows":
            self.combo_encoder.addItem("Video - Best (libx264)", "libx264")
            self.combo_encoder.addItem("Video - NVIDIA (h264_nvenc)", "h264_nvenc")
            self.combo_encoder.addItem("Video - AMD (h264_amf)", "h264_amf")
        else:
            self.combo_encoder.addItem("Video - Standard (libx264)", "libx264")

        # --- NEW: Audio Options ---
        self.combo_encoder.addItem("Audio Only (MP3)", "audio_mp3")
        self.combo_encoder.addItem("Audio Only (M4A)", "audio_m4a")
        # --------------------------

    def toggle_mode(self): self.spin_size.setEnabled(self.combo_mode.currentIndex() == 1)

    def detect_fps(self):
        try:
            cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", self.input_file]
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, startupinfo=startupinfo)
            parts = result.stdout.strip().split('/')
            if len(parts) == 2: self.fps = float(parts[0]) / float(parts[1])
            else: self.fps = float(result.stdout.strip())
        except: self.fps = 30.0

    def start_encoding(self):
        if not self.input_file: return

        custom_name = self.txt_output_name.text().strip()
        if not custom_name: custom_name = "output_video"

        input_dir = os.path.dirname(self.input_file)

        # --- NEW: Determine Extension ---
        encoder_data = self.combo_encoder.currentData()
        if encoder_data == "audio_mp3":
            ext = ".mp3"
        elif encoder_data == "audio_m4a":
            ext = ".m4a"
        else:
            ext = ".mp4"

        output_file = os.path.join(input_dir, f"{custom_name}{ext}")
        # --------------------------------

        if os.path.exists(output_file):
            reply = QMessageBox.question(self, "Overwrite?", f"File '{custom_name}{ext}' exists. Overwrite?", QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.No: return

        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.console.clear()

        start_sec = self.start_ms / 1000.0
        end_sec = self.end_ms / 1000.0
        duration = end_sec - start_sec
        if duration <= 0: duration = self.duration_ms / 1000.0

        cmd = ["ffmpeg", "-y"]
        if start_sec > 0: cmd.extend(["-ss", f"{start_sec:.3f}"])
        if end_sec < (self.duration_ms / 1000.0): cmd.extend(["-to", f"{end_sec:.3f}"])

        cmd.extend(["-i", self.input_file])

        # --- NEW: Audio vs Video Logic ---
        if "audio" in encoder_data:
            # AUDIO MODE
            cmd.append("-vn") # No Video
            if encoder_data == "audio_mp3":
                cmd.extend(["-c:a", "libmp3lame", "-q:a", "2"]) # High quality VBR
            else:
                cmd.extend(["-c:a", "aac", "-b:a", "192k"]) # High quality AAC
        else:
            # VIDEO MODE
            cmd.extend(["-c:v", encoder_data])
            speed = self.combo_speed.currentText()

            if self.combo_mode.currentIndex() == 1:
                target_mb = self.spin_size.value()
                bitrate = int(((target_mb * 8192) / duration) - 128)
                if bitrate < 100: bitrate = 100
                cmd.extend(["-b:v", f"{bitrate}k"])
                if "libx264" in encoder_data: cmd.extend(["-preset", speed])
                self.log(f">> Target: {target_mb}MB -> {bitrate}k bitrate")
            else:
                if "libx264" in encoder_data:
                    cmd.extend(["-crf", "23", "-preset", speed])
                else:
                    cmd.extend(["-b:v", "4000k", "-preset", speed])

            vf = ["scale='min(1920,iw)':-2", "format=yuv420p"]
            cmd.extend(["-vf", ",".join(vf)])

            if self.chk_gop.isChecked():
                gop = int(round(self.fps))
                cmd.extend(["-g", str(gop), "-bf", "0"])

            cmd.extend(["-c:a", "aac", "-b:a", "128k"])
            cmd.extend(["-movflags", "+faststart", "-use_editlist", "0"])
        # -----------------------------------

        cmd.append(output_file)

        self.worker = ConversionWorker(cmd)
        self.worker.log_output.connect(self.log)
        self.worker.finished.connect(self.done)
        self.worker.start()

    def stop_encoding(self):
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.stop()
            self.btn_stop.setEnabled(False)
            self.log(">> STOP REQUESTED...")

    def log(self, msg):
        self.console.append(msg)
        c = self.console.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        self.console.setTextCursor(c)

    def done(self, success, msg):
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if success: QMessageBox.information(self, "Success", msg)
        else: QMessageBox.critical(self, "Error/Stopped", msg)

if __name__ == "__main__":
    qInstallMessageHandler(qt_message_handler)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.WindowText, Qt.white)
    palette.setColor(QPalette.Base, QColor(25, 25, 25))
    palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ToolTipBase, Qt.white)
    palette.setColor(QPalette.ToolTipText, Qt.white)
    palette.setColor(QPalette.Text, Qt.white)
    palette.setColor(QPalette.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ButtonText, Qt.white)
    palette.setColor(QPalette.BrightText, Qt.red)
    palette.setColor(QPalette.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.Highlight, QColor(0, 120, 215))
    palette.setColor(QPalette.HighlightedText, Qt.black)
    app.setPalette(palette)

    # Set App Icon based on OS
    os_name = platform.system()
    if os_name == "Windows":
        try:
            import ctypes
            myappid = 'mycompany.myproduct.subproduct.version' # arbitrary string
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videoplayflat_106010.ico")
    elif os_name == "Darwin":
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videoplayflat_106010.icns")
    else:
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videoplayflat_106010.png")

    app_icon = QIcon(icon_path)
    app.setWindowIcon(app_icon)

    window = VideoEditorApp()
    window.setWindowIcon(app_icon)
    window.show()
    sys.exit(app.exec())
