#!/usr/bin/env python3
import json
import os
import sys
import threading
import time
import io
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QIcon, QTextCursor, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QStatusBar, QSystemTrayIcon, QMenu,
    QLineEdit, QFormLayout, QComboBox, QMessageBox,
    QSplitter, QFrame, QCheckBox,
    QTextEdit, QStackedWidget, QScrollArea,
)

from kook_auth import login, login_with_token, KookSession
from kook_api import KookAPI
from kook_voice import VoiceClient
from kook_chat import ChatGateway

SETTINGS_FILE = os.path.expanduser("~/.kook_gui_settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(d):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(d, f, indent=2)


# ── Login ──

class LoginPage(QWidget):
    login_success = pyqtSignal(object)  # KookSession

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        form = QFrame()
        form.setMaximumWidth(420)
        form_layout = QVBoxLayout(form)

        icon_label = QLabel()
        icon_path = next(
            (os.path.join(os.path.dirname(__file__), n) for n in ("kook.png", "KOOK.png")
             if os.path.exists(os.path.join(os.path.dirname(__file__), n))),
            None)
        if icon_path:
            try:
                p = QPixmap(icon_path)
                if not p.isNull():
                    icon_label.setPixmap(p.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio,
                                                   Qt.TransformationMode.SmoothTransformation))
                    icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            except Exception:
                pass
        form_layout.addWidget(icon_label)

        title = QLabel("KOOK")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form_layout.addWidget(title)

        form_layout.addSpacing(16)

        self.tab = QComboBox()
        self.tab.addItems(["手机号登录", "Token 登录"])
        form_layout.addWidget(self.tab)

        self.phone_input = QLineEdit(placeholderText="手机号")
        self.pwd_input = QLineEdit(placeholderText="密码")
        self.pwd_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.prefix_input = QLineEdit("86")
        self.prefix_input.setMaximumWidth(60)

        phone_layout = QHBoxLayout()
        phone_layout.addWidget(self.prefix_input)
        phone_layout.addWidget(self.phone_input, 1)
        form_layout.addWidget(QLabel("手机号 / 密码"))
        form_layout.addLayout(phone_layout)
        form_layout.addWidget(self.pwd_input)

        self.token_input = QLineEdit(placeholderText="从网页 F12 抓取的 token")
        self.token_type = QComboBox()
        self.token_type.addItems(["raw", "bot", "bearer"])
        form_layout.addWidget(QLabel("Token"))
        form_layout.addWidget(self.token_input)
        form_layout.addWidget(self.token_type)
        self.token_input.setVisible(False)
        self.token_type.setVisible(False)

        self.tab.currentIndexChanged.connect(self._switch_tab)

        self.remember_cb = QCheckBox("记住登录状态")
        self.remember_cb.setChecked(True)
        form_layout.addWidget(self.remember_cb)
        form_layout.addSpacing(8)

        login_btn = QPushButton("登 录")
        login_btn.setStyleSheet("padding: 8px; font-size: 15px;")
        login_btn.clicked.connect(self._do_login)
        form_layout.addWidget(login_btn)

        layout.addWidget(form)

        s = load_settings()
        if "phone" in s:
            self.phone_input.setText(s["phone"])
            self.prefix_input.setText(s.get("prefix", "86"))

    def _switch_tab(self, idx):
        is_phone = idx == 0
        self.phone_input.setVisible(is_phone)
        self.pwd_input.setVisible(is_phone)
        self.prefix_input.setVisible(is_phone)
        self.token_input.setVisible(not is_phone)
        self.token_type.setVisible(not is_phone)

    def _do_login(self):
        try:
            if self.tab.currentIndex() == 0:
                phone = self.phone_input.text().strip()
                pwd = self.pwd_input.text()
                prefix = self.prefix_input.text().strip() or "86"
                if not phone or not pwd:
                    raise ValueError("请输入手机号和密码")
                ks = login(phone, pwd, mobile_prefix=prefix)
                if self.remember_cb.isChecked():
                    s = load_settings()
                    s.update(phone=phone, prefix=prefix)
                    save_settings(s)
            else:
                token = self.token_input.text().strip()
                tt = self.token_type.currentText()
                if not token:
                    raise ValueError("请输入 token")
                ks = login_with_token(token, tt)
            self.login_success.emit(ks)
        except Exception as e:
            QMessageBox.critical(self, "登录失败", str(e))


# ── Voice worker ──

class VoiceWorker(QObject):
    connected = pyqtSignal(dict)
    disconnected = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, api):
        super().__init__()
        self.api = api
        self.vc: VoiceClient = None
        self._thread = None

    def join(self, channel_id):
        def run():
            try:
                vc = VoiceClient(self.api)
                self.vc = vc
                info = vc.join(channel_id)
                self.connected.emit(info)
                vc.push_mic()
                while vc._running:
                    time.sleep(0.1)
            except Exception as e:
                self.error.emit(str(e))
            finally:
                self.disconnected.emit()
                self.vc = None

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def leave(self):
        if self.vc:
            vc = self.vc
            self.vc = None
            vc.stop()


# ── Chat panel ──

class _ImageLoader(QObject):
    loaded = pyqtSignal(str, object, str)  # url, QPixmap, author_time_html

    def __init__(self, http_session, parent=None):
        super().__init__(parent)
        self.http = http_session
        self._cache = {}

    def load(self, url, author_time_html):
        if url in self._cache:
            self.loaded.emit(url, self._cache[url], author_time_html)
            return
        threading.Thread(target=self._dl, args=(url, author_time_html), daemon=True).start()

    def _dl(self, url, author_time_html):
        try:
            resp = self.http.get(url, timeout=15)
            pixmap = QPixmap()
            pixmap.loadFromData(resp.content)
            if pixmap.isNull():
                return
            max_w = 360
            if pixmap.width() > max_w:
                pixmap = pixmap.scaledToWidth(max_w, Qt.TransformationMode.SmoothTransformation)
            self._cache[url] = pixmap
            self.loaded.emit(url, pixmap, author_time_html)
        except Exception:
            pass


class ChatPanel(QWidget):
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.current_channel = None
        self._gateway: ChatGateway = None
        self._message_ids = set()
        self._img_loader = _ImageLoader(api.http)
        self._img_loader.loaded.connect(self._on_img_loaded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.header = QLabel("选择文字频道")
        self.header.setStyleSheet("font-size: 16px; padding: 6px; background: #eee;")
        layout.addWidget(self.header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.msg_area = QWidget()
        self.msg_layout = QVBoxLayout(self.msg_area)
        self.msg_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.msg_area)
        layout.addWidget(self.scroll, 1)

        input_layout = QHBoxLayout()
        self.input_box = QLineEdit(placeholderText="输入消息…")
        self.input_box.returnPressed.connect(self._send)
        self.send_btn = QPushButton("发送")
        self.send_btn.clicked.connect(self._send)
        input_layout.addWidget(self.input_box, 1)
        input_layout.addWidget(self.send_btn)
        layout.addLayout(input_layout)

        self.setEnabled(False)

    def set_channel(self, channel_id: str, channel_name: str):
        self.current_channel = channel_id
        self.header.setText(f"# {channel_name}")
        self._clear_messages()
        self.setEnabled(True)
        self._load_history()
        self._connect_gateway()

    def _clear_messages(self):
        while self.msg_layout.count():
            item = self.msg_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._message_ids.clear()

    def _author_html(self, author, time_str):
        return f"<b>{author}</b>  {time_str}"

    def _add_message(self, author: str, content: str, msg_id: str = None,
                     timestamp=None, msg_type: int = 1, attachments=None):
        if msg_id and msg_id in self._message_ids:
            return
        if msg_id:
            self._message_ids.add(msg_id)

        time_str = ""
        if timestamp is not None:
            if isinstance(timestamp, (int, float)) and timestamp > 1e11:
                timestamp /= 1000
            time_str = datetime.fromtimestamp(float(timestamp)).strftime("%H:%M")

        if msg_type == 2:
            url = None
            if attachments and isinstance(attachments, list):
                for a in attachments:
                    u = a.get("url") if isinstance(a, dict) else a
                    if u:
                        url = u
                        break
            if not url:
                url = content.strip().strip("()")
            if url:
                ah = self._author_html(author, time_str)
                self._img_loader.load(url, ah)
            return

        label = QLabel(
            f"<b>{author}</b>  {time_str}<br>{content}")
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setContentsMargins(4, 2, 4, 2)
        self.msg_layout.addWidget(label)

    def _on_img_loaded(self, url, pixmap, author_time_html):
        container = QWidget()
        cl = QVBoxLayout(container)
        cl.setContentsMargins(4, 2, 4, 2)
        header = QLabel(author_time_html)
        header.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(header)
        img_label = QLabel()
        img_label.setPixmap(pixmap)
        img_label.setCursor(Qt.CursorShape.PointingHandCursor)
        img_label.mousePressEvent = lambda e, u=url: self._show_full_img(u)
        cl.addWidget(img_label)
        self.msg_layout.addWidget(container)

    def _show_full_img(self, url):
        try:
            resp = self.api.http.get(url, timeout=15)
            pix = QPixmap()
            pix.loadFromData(resp.content)
            if pix.isNull():
                return
            from PyQt6.QtWidgets import QDialog, QGraphicsView, QGraphicsScene
            dlg = QDialog(self)
            dlg.setWindowTitle("查看图片")
            dlg.resize(min(pix.width() + 40, 900), min(pix.height() + 40, 700))
            layout = QVBoxLayout(dlg)
            scene = QGraphicsScene()
            scene.addPixmap(pix)
            view = QGraphicsView(scene)
            view.setRenderHint(view.RenderHint.SmoothPixmapTransform)
            layout.addWidget(view)
            dlg.exec()
        except Exception:
            pass

    def _load_history(self):
        try:
            msgs = self.api.get_messages(self.current_channel, page_size=50)
            for m in msgs:
                author = m.get("author", {}).get("username", "?")
                content = m.get("content", "")
                msg_id = m.get("id") or m.get("msg_id")
                ts = m.get("create_at") or m.get("created_at", 0)
                msg_type = m.get("type", 1)
                attachments = m.get("attachments")
                self._add_message(author, content, msg_id, ts, msg_type, attachments)
            QTimer.singleShot(100, lambda: self.scroll.verticalScrollBar().setValue(
                self.scroll.verticalScrollBar().maximum()))
        except Exception as e:
            self._add_message("系统", f"加载消息失败: {e}")

    def _connect_gateway(self):
        if self._gateway:
            self._gateway.stop()
        try:
            url = self.api.get_gateway_index()
            self._gateway = ChatGateway(self.api.session.token)
            ch = self.current_channel
            def on_msg(data):
                if data.get("target_id") != ch:
                    return
                author = data.get("author", {}).get("username", "?")
                content = data.get("content", "")
                msg_id = data.get("id", str(time.time()))
                msg_type = data.get("type", 1)
                ts = data.get("created_at") or data.get("create_at", time.time())
                attachments = data.get("attachments")
                self._add_message(author, content, msg_id, ts, msg_type, attachments)
            self._gateway.on_message = on_msg
            self._gateway.connect(url)
        except Exception as e:
            import traceback; traceback.print_exc()
            logger.warning("Gateway connect failed: %s", e)

    def _send(self):
        text = self.input_box.text().strip()
        if not text or not self.current_channel:
            return
        self.input_box.clear()
        try:
            self.api.send_message(self.current_channel, text)
            self._add_message("我", text, timestamp=time.time())
        except Exception as e:
            self._add_message("系统", f"发送失败: {e}")

    def stop_gateway(self):
        if self._gateway:
            self._gateway.stop()
            self._gateway = None


# ── Main window ──

class MainWindow(QMainWindow):
    def __init__(self, api=None):
        super().__init__()
        self.api = api
        self.voice_worker = None
        self.chat_panel = None
        self.joined = False

        self.setWindowTitle("KOOK")
        self.setMinimumSize(800, 550)
        app_icon = QApplication.instance().windowIcon()
        self.setWindowIcon(app_icon)
        self._build_ui()
        self._connect_signals()
        self._setup_tray()

        if api:
            self._on_logged_in(api)

    def _on_logged_in(self, ks):
        self.api = KookAPI(ks)
        self.voice_worker = VoiceWorker(self.api)
        self.voice_worker.connected.connect(self._on_connected)
        self.voice_worker.disconnected.connect(self._on_disconnected)
        self.voice_worker.error.connect(self._on_error)
        self.main_stack.setCurrentIndex(1)
        if not self._load_guilds():
            self.main_stack.setCurrentIndex(0)
            QMessageBox.critical(self, "登录失败", "会话已过期，请重新登录")
            return
        self._show_status("就绪")

    def _build_ui(self):
        self.main_stack = QStackedWidget()
        self.setCentralWidget(self.main_stack)

        # page 0: login page
        self.login_page = LoginPage()
        self.main_stack.addWidget(self.login_page)

        # page 1: main content
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.guild_tree = QTreeWidget()
        self.guild_tree.setHeaderLabel("频道")
        self.guild_tree.setMinimumWidth(200)
        left_layout.addWidget(self.guild_tree)
        splitter.addWidget(left)

        self.right_stack = QStackedWidget()

        self.voice_panel = QWidget()
        vp_layout = QVBoxLayout(self.voice_panel)
        self.voice_header = QLabel("选择语音频道")
        self.voice_header.setStyleSheet("font-size: 16px; padding: 6px; background: #eee;")
        vp_layout.addWidget(self.voice_header)
        vp_layout.addStretch()

        btn_layout = QHBoxLayout()
        self.join_btn = QPushButton("加入频道")
        self.leave_btn = QPushButton("退出频道")
        self.leave_btn.setEnabled(False)
        btn_layout.addWidget(self.join_btn)
        btn_layout.addWidget(self.leave_btn)
        vp_layout.addLayout(btn_layout)

        self.status_frame = QFrame()
        self.status_frame.setFrameStyle(QFrame.Shape.StyledPanel)
        status_layout = QVBoxLayout(self.status_frame)
        self.status_label = QLabel("未连接")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-size: 14px; padding: 8px;")
        status_layout.addWidget(self.status_label)
        vp_layout.addWidget(self.status_frame)

        self.right_stack.addWidget(self.voice_panel)  # 0
        self.right_stack.addWidget(QWidget())          # 1 placeholder for chat

        splitter.addWidget(self.right_stack)
        splitter.setSizes([280, 520])
        content_layout.addWidget(splitter)

        self.main_stack.addWidget(content)

        # ── menu bar ──
        menubar = self.menuBar()
        account_menu = menubar.addMenu("账号")
        logout_action = QAction("退出登录", self)
        logout_action.triggered.connect(self._logout)
        account_menu.addAction(logout_action)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._show_status("就绪")

    def _connect_signals(self):
        self.guild_tree.itemClicked.connect(self._on_item_clicked)
        self.join_btn.clicked.connect(self._join)
        self.leave_btn.clicked.connect(self._leave)
        self.login_page.login_success.connect(self._on_logged_in)

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.windowIcon())
        self.tray_icon.setToolTip("KOOK")

        tray_menu = QMenu()
        show_action = QAction("显示窗口", self)
        show_action.triggered.connect(self.show)
        tray_menu.addAction(show_action)

        self.tray_join = QAction("加入语音频道…", self)
        self.tray_join.triggered.connect(self._join)
        tray_menu.addAction(self.tray_join)

        self.tray_leave = QAction("退出语音频道", self)
        self.tray_leave.triggered.connect(self._leave)
        self.tray_leave.setEnabled(False)
        tray_menu.addAction(self.tray_leave)

        tray_menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self.tray_icon.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.raise_()

    def _quit(self):
        if self.joined:
            self.voice_worker.leave()
        if self.chat_panel:
            self.chat_panel.stop_gateway()
        QApplication.quit()

    # ── guild / channel loading ──

    def _load_guilds(self):
        self.guild_tree.clear()
        try:
            guilds = self.api.get_guilds()
            for g in guilds:
                item = QTreeWidgetItem([g.get("name", "?")])
                item.setData(0, Qt.ItemDataRole.UserRole, g["id"])
                item.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                self.guild_tree.addTopLevelItem(item)
            return True
        except Exception as e:
            self.status_bar.showMessage(f"加载服务器失败: {e}")
            return False

    def _show_status(self, msg):
        self.status_bar.showMessage(msg)

    def _logout(self):
        if self.joined:
            self.voice_worker.leave()
        if self.chat_panel:
            self.chat_panel.stop_gateway()
        try:
            os.remove(KookSession._session_path() if hasattr(KookSession, '_session_path') else
                      os.path.expanduser("~/.config/kook-linux/session.json"))
        except Exception:
            pass
        self.api = None
        self.voice_worker = None
        self.joined = False
        self.guild_tree.clear()
        self.right_stack.setCurrentIndex(0)
        self.chat_panel = None
        self.status_label.setText("未连接")
        self.main_stack.setCurrentIndex(0)

    def _on_item_clicked(self, item, _col):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        ctype = item.data(0, Qt.ItemDataRole.UserRole + 1)
        guild_id = item.data(0, Qt.ItemDataRole.UserRole + 2)

        # If it's a guild, load channels
        if ctype is None:
            guild_id = data  # guild ID is in UserRole for guild items
            if guild_id and item.childCount() == 0:
                self._load_channels(item, guild_id)
            return

        # If it's a channel, route to appropriate panel
        if ctype == 1:  # text
            self._show_text_channel(data, item.text(0))
        elif ctype == 2:  # voice
            self._show_voice_channel(data, item.text(0))

    def _load_channels(self, parent_item, guild_id):
        try:
            for ctype, prefix in [(1, "# "), (2, "🔊 ")]:
                channels = self.api.get_channels(guild_id, channel_type=ctype)
                if channels:
                    section = QTreeWidgetItem([f"{'文字' if ctype==1 else '语音'}频道"])
                    section.setFlags(section.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    parent_item.addChild(section)
                    for c in channels:
                        child = QTreeWidgetItem([f"{prefix}{c.get('name', '?')}"])
                        child.setData(0, Qt.ItemDataRole.UserRole, c["id"])
                        child.setData(0, Qt.ItemDataRole.UserRole + 1, ctype)
                        child.setData(0, Qt.ItemDataRole.UserRole + 2, guild_id)
                        section.addChild(child)
            parent_item.setExpanded(True)
        except Exception as e:
            self.status_bar.showMessage(f"加载频道失败: {e}")

    # ── text channel ──

    def _show_text_channel(self, channel_id, name):
        # lazy init chat panel
        if self.right_stack.widget(1).isWidgetType() and not isinstance(self.right_stack.widget(1), ChatPanel):
            self.chat_panel = ChatPanel(self.api)
            self.right_stack.removeWidget(self.right_stack.widget(1))
            self.right_stack.insertWidget(1, self.chat_panel)
        elif self.chat_panel is None:
            self.chat_panel = ChatPanel(self.api)
            self.right_stack.removeWidget(self.right_stack.widget(1))
            self.right_stack.insertWidget(1, self.chat_panel)

        clean_name = name.lstrip("# ")
        self.chat_panel.set_channel(channel_id, clean_name)
        self.right_stack.setCurrentIndex(1)

    # ── voice channel ──

    def _show_voice_channel(self, channel_id, name):
        self._voice_channel_id = channel_id
        clean_name = name.lstrip("🔊 ")
        self.voice_header.setText(f"🔊 {clean_name}")
        self.right_stack.setCurrentIndex(0)

    def _join(self):
        if self.joined:
            return
        ch_id = getattr(self, '_voice_channel_id', None)
        if not ch_id:
            QMessageBox.information(self, "提示", "请先选择一个语音频道")
            return
        self.joined = True
        self.join_btn.setEnabled(False)
        self.leave_btn.setEnabled(True)
        self.tray_join.setEnabled(False)
        self.tray_leave.setEnabled(True)
        self.status_label.setText("🔊 连接中…")
        self.status_label.setStyleSheet(
            "font-size: 14px; padding: 8px; color: #f0ad4e;")
        self.status_bar.showMessage("正在加入频道…")
        self.voice_worker.join(ch_id)

    def _leave(self):
        if not self.joined:
            return
        self.joined = False
        self.voice_worker.leave()
        self._on_disconnected()

    def _on_connected(self, info):
        self.status_label.setText("🎤 通话中")
        self.status_label.setStyleSheet(
            "font-size: 14px; padding: 8px; color: #5cb85c;")
        self.status_bar.showMessage(
            f"已加入语音频道 | {info.get('ip', '')}:{info.get('port', '')}")

    def _on_disconnected(self):
        self.joined = False
        self.join_btn.setEnabled(True)
        self.leave_btn.setEnabled(False)
        self.tray_join.setEnabled(True)
        self.tray_leave.setEnabled(False)
        self.status_label.setText("未连接")
        self.status_label.setStyleSheet(
            "font-size: 14px; padding: 8px; color: #888;")
        self.status_bar.showMessage("已离开频道")

    def _on_error(self, msg):
        QMessageBox.critical(self, "语音错误", msg)
        self._on_disconnected()

    def closeEvent(self, ev):
        if self.tray_icon and self.tray_icon.isVisible():
            self.hide()
            ev.ignore()
        else:
            if self.joined:
                self.voice_worker.leave()
            if self.chat_panel:
                self.chat_panel.stop_gateway()
            ev.accept()


import logging
logger = logging.getLogger(__name__)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("KOOK")

    icon_path = next(
        (os.path.join(os.path.dirname(__file__), n) for n in ("kook.png", "KOOK.png")
         if os.path.exists(os.path.join(os.path.dirname(__file__), n))),
        None)
    if icon_path:
        try:
            icon = QIcon(icon_path)
            if not icon.isNull():
                app.setWindowIcon(icon)
        except Exception:
            pass

    win = MainWindow()
    ks = KookSession.load()
    if ks:
        win._on_logged_in(ks)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
