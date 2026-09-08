"""PyQt5 UI for the ROS2 Workspace Manager with improved styling and UX."""

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QCheckBox,
    QFileDialog, QGroupBox,
    QSpinBox, QToolBar, QAction, QLineEdit, QTextEdit,
    QStatusBar, QComboBox, QSplitter, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView, QDialog,
    QGraphicsView, QGraphicsScene, QMenu, QGraphicsRectItem, QGraphicsTextItem,
)
from PyQt5.QtCore import (
    Qt, QSize, QPointF, QRectF, QSignalBlocker, QTimer,
    QObject, QRunnable, QThreadPool, pyqtSignal,
)
from PyQt5.QtGui import QPen, QBrush, QColor, QFont, QPolygonF, QWheelEvent, QTextCursor
import os
import copy
from pathlib import Path
import shlex
import sys

from ..core.build_plan import build_request, colcon_program
from ..core.cleaner import execute_clean_plan, make_clean_plan
from ..core.dependency_graph import closure
from ..core.locks import workspace_lock
from ..core.package_scanner import (
    WorkspaceError, discovery_arguments, fingerprint, parse_discovery,
    parse_selection, validate_colcon_configuration, validate_root,
)
from .process_runner import ProcessRunner


class ZoomableGraphicsView(QGraphicsView):
    """支持鼠标滚轮缩放的 QGraphicsView"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.zoom_factor = 1.15

    def wheelEvent(self, event: QWheelEvent):
        """处理鼠标滚轮事件进行缩放"""
        if event.angleDelta().y() > 0:
            # 向上滚动，放大
            self.scale(self.zoom_factor, self.zoom_factor)
        else:
            # 向下滚动，缩小
            self.scale(1 / self.zoom_factor, 1 / self.zoom_factor)
        event.accept()


class ClickableNodeItem(QGraphicsRectItem):
    """可点击的节点图形项，支持选中状态和高亮"""
    def __init__(self, rect, package_name, is_selected, theme_name, parent=None):
        super().__init__(rect, parent)
        self.package_name = package_name
        self.is_initially_selected = is_selected
        self.theme_name = theme_name
        self.highlight_type = None  # None, 'incoming'(黄色), 'outgoing'(红色)
        self.text_item = None

        # 设置标志
        self.setFlag(QGraphicsRectItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)

        # 设置初始颜色
        self._update_colors()

    def _update_colors(self):
        """根据状态更新颜色"""
        if self.isSelected():
            # 选中状态 - 使用绿色
            color_bg = QColor(50, 180, 50)
            color_border = QColor(100, 220, 100)
            text_color = QColor(255, 255, 255)
        elif self.highlight_type == 'incoming':
            # 指向选中节点的节点 - 使用黄色
            color_bg = QColor(220, 180, 30)
            color_border = QColor(255, 220, 80)
            text_color = QColor(255, 255, 255)
        elif self.highlight_type == 'outgoing':
            # 选中节点指向的节点 - 使用红色
            color_bg = QColor(220, 60, 60)
            color_border = QColor(255, 100, 100)
            text_color = QColor(255, 255, 255)
        elif self.is_initially_selected:
            # 初始选中的包（从包列表选中的）
            if self.theme_name == 'light':
                color_bg = QColor(25, 118, 210)
                color_border = QColor(208, 208, 208)
                text_color = QColor(255, 255, 255)
            else:
                color_bg = QColor(94, 129, 172)
                color_border = QColor(59, 66, 82)
                text_color = QColor(255, 255, 255)
        else:
            # 普通状态
            if self.theme_name == 'light':
                color_bg = QColor(255, 255, 255)
                color_border = QColor(208, 208, 208)
                text_color = QColor(33, 33, 33)
            else:
                color_bg = QColor(42, 47, 58)
                color_border = QColor(59, 66, 82)
                text_color = QColor(230, 230, 230)

        self.setBrush(QBrush(color_bg))
        pen = QPen(color_border)
        pen.setWidth(3 if self.isSelected() else 2 if self.highlight_type else 1)
        self.setPen(pen)

        # 更新文本颜色
        if self.text_item:
            self.text_item.setDefaultTextColor(text_color)

    def set_highlight_type(self, highlight_type):
        """设置高亮类型：None, 'incoming'(黄色), 'outgoing'(红色)"""
        self.highlight_type = highlight_type
        self._update_colors()

    def mousePressEvent(self, event):
        """处理鼠标点击事件"""
        if event.button() == Qt.LeftButton:
            # 获取当前场景
            if self.scene():
                # 先取消所有节点的选中状态
                for node in self.scene().node_items.values():
                    if node != self and node.isSelected():
                        node.setSelected(False)
                        node._update_colors()

                # 切换当前节点的选中状态
                self.setSelected(not self.isSelected())
                self._update_colors()

                # 通知场景更新相关节点高亮
                self.scene().update_node_highlights()

        super().mousePressEvent(event)

    def hoverEnterEvent(self, event):
        """鼠标悬停进入"""
        self.setCursor(Qt.PointingHandCursor)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        """鼠标悬停离开"""
        self.setCursor(Qt.ArrowCursor)
        super().hoverLeaveEvent(event)


class DependencyGraphScene(QGraphicsScene):
    """依赖关系图场景，管理节点高亮和边的颜色"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.node_items = {}  # package_name -> ClickableNodeItem
        self.edges = []  # [(src_name, dest_name), ...]
        self.edge_items = []  # 存储边的图形项 [(line_item, arrow_item, src, dest), ...]
        self.theme_name = 'dark'

    def update_node_highlights(self):
        """更新所有节点的高亮状态和边的颜色"""
        # 获取当前选中的节点
        selected_nodes = set()
        for node_item in self.node_items.values():
            if node_item.isSelected():
                selected_nodes.add(node_item.package_name)

        if not selected_nodes:
            # 如果没有选中节点，清除所有高亮和边的颜色
            for node_item in self.node_items.values():
                node_item.set_highlight_type(None)
            self._reset_edge_colors()
            return

        # 对于选中的节点，区分入边和出边
        selected_node = list(selected_nodes)[0]  # 只支持单选
        incoming_nodes = set()  # 指向选中节点的节点（黄色）
        outgoing_nodes = set()  # 选中节点指向的节点（红色）

        for src, dest in self.edges:
            if dest == selected_node:
                incoming_nodes.add(src)
            if src == selected_node:
                outgoing_nodes.add(dest)

        # 更新所有节点的高亮状态
        for package_name, node_item in self.node_items.items():
            if package_name in selected_nodes:
                # 选中的节点不设置高亮
                node_item.set_highlight_type(None)
            elif package_name in incoming_nodes:
                # 指向选中节点的节点 - 黄色
                node_item.set_highlight_type('incoming')
            elif package_name in outgoing_nodes:
                # 选中节点指向的节点 - 红色
                node_item.set_highlight_type('outgoing')
            else:
                # 其他节点取消高亮
                node_item.set_highlight_type(None)

        # 更新边的颜色
        self._update_edge_colors(selected_node, incoming_nodes, outgoing_nodes)

    def _reset_edge_colors(self):
        """重置所有边为默认颜色"""
        default_color = QColor(136, 192, 208) if self.theme_name == 'dark' else QColor(100, 100, 100)
        default_pen = QPen(default_color)
        default_pen.setWidth(1)

        for line_item, arrow_item, _, _ in self.edge_items:
            line_item.setPen(default_pen)
            if arrow_item:
                arrow_item.setPen(default_pen)
                arrow_item.setBrush(QBrush(default_color))

    def _update_edge_colors(self, selected_node, incoming_nodes, outgoing_nodes):
        """更新边的颜色"""
        default_color = QColor(136, 192, 208) if self.theme_name == 'dark' else QColor(100, 100, 100)
        yellow_color = QColor(255, 220, 80)  # 黄色 - 指向选中节点的边
        red_color = QColor(255, 100, 100)    # 红色 - 选中节点指向的边

        for line_item, arrow_item, src, dest in self.edge_items:
            if dest == selected_node and src in incoming_nodes:
                # 指向选中节点的边 - 黄色
                pen = QPen(yellow_color)
                pen.setWidth(2)
                line_item.setPen(pen)
                if arrow_item:
                    arrow_item.setPen(pen)
                    arrow_item.setBrush(QBrush(yellow_color))
            elif src == selected_node and dest in outgoing_nodes:
                # 选中节点指向的边 - 红色
                pen = QPen(red_color)
                pen.setWidth(2)
                line_item.setPen(pen)
                if arrow_item:
                    arrow_item.setPen(pen)
                    arrow_item.setBrush(QBrush(red_color))
            else:
                # 其他边 - 默认颜色
                pen = QPen(default_color)
                pen.setWidth(1)
                line_item.setPen(pen)
                if arrow_item:
                    arrow_item.setPen(pen)
                    arrow_item.setBrush(QBrush(default_color))


class _CleanSignals(QObject):
    completed = pyqtSignal(object, str)


class _CleanWorker(QRunnable):
    def __init__(self, plan):
        super().__init__()
        self.plan = plan
        self.signals = _CleanSignals()

    def run(self):
        try:
            self.signals.completed.emit(execute_clean_plan(self.plan), '')
        except Exception as exc:
            self.signals.completed.emit(None, str(exc))


class WorkspaceManagerGUI(QMainWindow):
    def __init__(self, node, config_store, source_root, install_prefix):
        super().__init__()
        self.node = node
        self.config_store = config_store
        self.source_root = Path(source_root)
        self.install_prefix = Path(install_prefix)
        self.config_file = str(config_store.path)
        self.config = copy.deepcopy(config_store.data)
        self.workspace_root = None
        self.snapshot = None
        self.explicit_targets = set()
        self.effective_packages = set()
        self.selection_verified = False
        self.package_checkboxes = {}
        self.package_dependencies = {}
        self.reverse_dependencies = {}
        self.operation = 'idle'
        self._operation_id = None
        self._operation_callback = None
        self._operation_lock = None
        self._close_pending = False
        self._initializing = True
        self._clean_worker = None
        self.theme_name = self.config['theme']
        self.always_on_top = self.config['always_on_top']
        self.runner = ProcessRunner(self.source_root / 'workspace_manager/process_launcher.py', self)
        self.runner.output.connect(self._on_process_output)
        self.runner.completed.connect(self._on_process_completed)
        self.setupUI()
        self.apply_theme(self.theme_name)
        self._initializing = False
        self._set_operation('idle')
        self._append_log(f'配置文件: {self.config_file}')
        for warning in self.config_store.warnings:
            self._append_log('[配置] ' + warning)
        initial_root = self.config.get('workspace_path')
        if initial_root:
            QTimer.singleShot(0, lambda: self._scan_workspace(initial_root, restore=True))

    def get_package_size(self, package_path):
        """计算包文件夹的大小"""
        try:
            total_size = 0
            seen_inodes = set()  # 用于避免硬链接重复计算

            for dirpath, dirnames, filenames in os.walk(package_path):
                # 跳过一些常见的大型缓存目录
                dirnames[:] = [d for d in dirnames if d not in ['.git', '__pycache__', '.pytest_cache', 'build', '.vscode']]

                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    try:
                        # 获取文件状态
                        stat_info = os.lstat(filepath)  # 使用lstat避免跟随符号链接

                        # 检查是否是硬链接（避免重复计算）
                        inode = (stat_info.st_dev, stat_info.st_ino)
                        if inode in seen_inodes:
                            continue
                        seen_inodes.add(inode)

                        # 只计算常规文件的大小
                        if os.path.isfile(filepath) and not os.path.islink(filepath):
                            total_size += stat_info.st_size
                        elif os.path.islink(filepath):
                            # 符号链接本身的大小（链接路径的长度）
                            total_size += len(os.readlink(filepath))

                    except (OSError, IOError):
                        # 跳过无法访问的文件
                        continue
            return total_size
        except Exception:
            return 0

    def format_size(self, size_bytes):
        """格式化文件大小显示"""
        if size_bytes == 0:
            return "0 B"
        elif size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:  # 小于1MB
            return f"{size_bytes / 1024:.1f} KB"
        else:  # 大于等于1MB
            return f"{size_bytes / (1024 * 1024):.1f} MB"


    def load_config(self):
        if self.operation != 'idle':
            return
        self.config_store.load()
        self.config = copy.deepcopy(self.config_store.data)
        widgets = (self.symlink_check, self.workers_spin, self.build_type_combo,
                   self.theme_combo, self.always_on_top_btn)
        blockers = [QSignalBlocker(widget) for widget in widgets]
        self.symlink_check.setChecked(self.config['symlink_install'])
        self.workers_spin.setValue(self.config['parallel_workers'])
        self.build_type_combo.setCurrentIndex(
            self.build_type_combo.findData(self.config['build_type']))
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self.config['theme']))
        self.always_on_top_btn.setChecked(self.config['always_on_top'])
        self.always_on_top = self.config['always_on_top']
        self.set_always_on_top(self.always_on_top)
        self.theme_name = self.config['theme']
        self.apply_theme(self.theme_name)
        del blockers
        for warning in self.config_store.warnings:
            self._append_log('[配置] ' + warning)
        root = self.config.get('workspace_path') or self.workspace_root
        if root:
            self._scan_workspace(root, restore=True)

    def save_config(self, *_args):
        if self._initializing:
            return True
        candidate = copy.deepcopy(self.config)
        candidate.update({
            'workspace_path': self.workspace_root or '',
            'symlink_install': self.symlink_check.isChecked(),
            'always_on_top': self.always_on_top,
            'parallel_workers': self.workers_spin.value(),
            'theme': self.theme_combo.currentData(),
            'build_type': self.build_type_combo.currentData(),
        })
        if self.workspace_root:
            sessions = candidate.setdefault('workspaces', {})
            sessions.setdefault(self.workspace_root, {})['explicit_targets'] = sorted(
                self.explicit_targets)
        self.config = candidate
        try:
            self.config_store.save(candidate)
            self.config = copy.deepcopy(self.config_store.data)
            return True
        except (OSError, RuntimeError, ValueError) as exc:
            self._append_log(f'[配置未保存] {exc}')
            self.status.showMessage('配置未保存；可使用“重新加载配置”处理外部修改')
            return False

    def setupUI(self):
        self.setWindowTitle('ROS2 Workspace Manager')
        self.setMinimumSize(980, 680)

        # Toolbar
        self.toolbar = QToolBar('工具栏')
        self.toolbar.setIconSize(QSize(18, 18))
        # 使用纯文字工具按钮，避免主题图标依赖
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(self.toolbar)
        self._create_toolbar_actions()

        # Central widget and layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Workspace + Search header
        header_layout = QHBoxLayout()
        self.workspace_label = QLabel('工作空间:')
        self.workspace_path = QLabel('未选择')
        header_layout.addWidget(self.workspace_label)
        header_layout.addWidget(self.workspace_path, stretch=1)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('搜索包...')
        self.search_edit.textChanged.connect(self._apply_search_filter)
        header_layout.addWidget(self.search_edit)
        layout.addLayout(header_layout)

        # Splitter with packages (left) and logs (right)
        splitter = QSplitter()

        # Packages panel
        packages_group = QGroupBox('包列表')
        packages_layout = QVBoxLayout()
        # 表格形式展示包
        self.packages_table = QTableWidget(0, 3)
        self.packages_table.setHorizontalHeaderLabels(['选择', '包名', '包大小'])
        header = self.packages_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.packages_table.setAlternatingRowColors(True)
        self.packages_table.setShowGrid(True)
        self.packages_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.packages_table.customContextMenuRequested.connect(self.show_package_context_menu)
        packages_layout.addWidget(self.packages_table)
        packages_group.setLayout(packages_layout)
        splitter.addWidget(packages_group)

        # Log panel
        self.log_group = QGroupBox('构建日志')
        log_layout = QVBoxLayout()
        self.log_text = QTextEdit(readOnly=True)
        self.log_text.document().setMaximumBlockCount(6000)
        self.log_text.setPlaceholderText('编译输出将在此显示...')
        log_layout.addWidget(self.log_text)
        self.log_group.setLayout(log_layout)
        splitter.addWidget(self.log_group)
        # 默认让包列表占据更大的横向空间
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 2)
        try:
            splitter.setSizes([800, 400])
        except Exception:
            pass
        layout.addWidget(splitter)

        # Build options row
        options_layout = QHBoxLayout()
        options_layout.setContentsMargins(0, 6, 0, 6)
        self.symlink_check = QCheckBox('符号链接安装 (symlink)')
        self.symlink_check.setChecked(self.config.get('symlink_install', True))
        self.symlink_check.stateChanged.connect(self.save_config)
        options_layout.addWidget(self.symlink_check)

        # 构建类型（Release/Debug/让CMakeLists决定）紧邻 symlink 选项
        options_layout.addWidget(QLabel('构建类型'))
        self.build_type_combo = QComboBox()
        self.build_type_combo.addItem('让CMakeLists决定', 'auto')
        self.build_type_combo.addItem('Release', 'Release')
        self.build_type_combo.addItem('Debug', 'Debug')
        bt = self.config.get('build_type', 'auto')
        if bt not in ['auto', 'Release', 'Debug']:
            bt = 'auto'
        idx_bt = self.build_type_combo.findData(bt)
        if idx_bt >= 0:
            self.build_type_combo.setCurrentIndex(idx_bt)
        self.build_type_combo.currentIndexChanged.connect(self.save_config)
        options_layout.addWidget(self.build_type_combo)

        self.always_on_top_btn = QPushButton('置顶窗口')
        self.always_on_top_btn.setCheckable(True)
        self.always_on_top_btn.setChecked(self.config.get('always_on_top', False))
        self.always_on_top_btn.clicked.connect(self.toggle_always_on_top)
        options_layout.addWidget(self.always_on_top_btn)

        options_layout.addWidget(QLabel('并行包数'))
        self.workers_spin = QSpinBox()
        self.workers_spin.setMinimum(1)
        max_workers = os.cpu_count() or 32
        self.workers_spin.setMaximum(max_workers)
        self.workers_spin.setValue(int(self.config.get('parallel_workers', max_workers)))
        self.workers_spin.valueChanged.connect(self.save_config)
        options_layout.addWidget(self.workers_spin)

        options_layout.addWidget(QLabel('主题'))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem('浅色', 'light')
        self.theme_combo.addItem('深色', 'dark')
        idx = self.theme_combo.findData(self.theme_name)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        options_layout.addWidget(self.theme_combo)

        # ROS_DISTRO 显示（只读）
        ros_distro = os.environ.get('ROS_DISTRO') or '未知'
        self.ros_distro_label = QLabel(f'ROS_DISTRO: {ros_distro}')
        options_layout.addWidget(self.ros_distro_label)

        # push primary actions to the far right
        options_layout.addStretch(1)

        # Secondary + Primary buttons (same row, aligned height)
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(8)

        self.clean_secondary_btn = QPushButton('清理')
        self.clean_secondary_btn.setObjectName('cleanSecondaryBtn')
        self.clean_secondary_btn.setFixedHeight(36)
        self.clean_secondary_btn.setMinimumWidth(120)
        self.clean_secondary_btn.clicked.connect(self.clean_workspace)
        buttons_row.addWidget(self.clean_secondary_btn)

        self.build_primary_btn = QPushButton('编译所选')
        self.build_primary_btn.setObjectName('buildPrimaryBtn')
        self.build_primary_btn.setFixedHeight(36)
        self.build_primary_btn.setMinimumWidth(140)
        self.build_primary_btn.clicked.connect(self.build_package)
        buttons_row.addWidget(self.build_primary_btn)

        options_layout.addLayout(buttons_row)

        layout.addLayout(options_layout)

        # Status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.progress = QProgressBar()
        self.progress.setMaximum(0)
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)

        # Apply stored always-on-top
        self.always_on_top = self.config.get('always_on_top', False)
        if self.always_on_top:
            self.set_always_on_top(True)

        # footer removed; build button lives in options row for aligned height

    def toggle_always_on_top(self):
        """切换窗口置顶状态"""
        self.always_on_top = self.always_on_top_btn.isChecked()
        self.set_always_on_top(self.always_on_top)
        self.save_config()

    def set_always_on_top(self, on_top):
        """设置窗口置顶状态"""
        flags = self.windowFlags()
        if on_top:
            self.setWindowFlags(flags | Qt.WindowStaysOnTopHint)
            self.always_on_top_btn.setText('取消置顶')
        else:
            self.setWindowFlags(flags & ~Qt.WindowStaysOnTopHint)
            self.always_on_top_btn.setText('置顶窗口')
        self.show()  # 需要重新显示窗口以应用新的标志

        # 使用X11特定API设置窗口置顶
        try:
            if hasattr(self.windowHandle(), 'setProperty'):
                # 设置X11属性
                self.windowHandle().setProperty("_NET_WM_STATE_ABOVE", on_top)
        except Exception as e:
            self.node.get_logger().warning(f"无法设置X11窗口属性: {e}")

    def _create_toolbar_actions(self):
        """创建工具栏动作并绑定（文字按钮）。"""

        self._workspace_actions = []
        act_select_ws = QAction('选择工作空间', self)
        self._workspace_actions.append(act_select_ws)
        act_select_ws.triggered.connect(self.select_workspace)
        self.toolbar.addAction(act_select_ws)

        act_refresh = QAction('刷新', self)
        act_refresh.triggered.connect(self.refresh_packages)
        self.toolbar.addAction(act_refresh)
        self._workspace_actions.append(act_refresh)

        self.toolbar.addSeparator()

        act_select_all = QAction('全选', self)
        act_select_all.triggered.connect(self.select_all_packages)
        self.toolbar.addAction(act_select_all)
        self._workspace_actions.append(act_select_all)

        act_deselect_all = QAction('全不选', self)
        act_deselect_all.triggered.connect(self.deselect_all_packages)
        self.toolbar.addAction(act_deselect_all)
        self._workspace_actions.append(act_deselect_all)

        self.toolbar.addSeparator()

        # 依赖关系图
        act_graph = QAction('依赖关系图', self)
        act_graph.triggered.connect(self.show_dependency_graph)
        self.toolbar.addAction(act_graph)
        self._workspace_actions.append(act_graph)

        act_reload = QAction('重新加载配置', self)
        act_reload.triggered.connect(self.load_config)
        self.toolbar.addAction(act_reload)
        self._workspace_actions.append(act_reload)
        self.toolbar.addSeparator()

        # 仅保留“停止编译”在工具栏；构建/清理移动到底部
        self.act_stop = QAction('停止编译', self)
        self.act_stop.triggered.connect(self.stop_build)
        self.act_stop.setEnabled(False)
        self.toolbar.addAction(self.act_stop)

    def _on_theme_changed(self):
        """主题切换处理。"""
        theme = self.theme_combo.currentData()
        self.theme_name = theme
        self.apply_theme(theme)
        self.save_config()

    def apply_theme(self, theme_name: str):
        """应用主题（light/dark）。"""
        try:
            share_dir = str(self.source_root / 'workspace_manager')
            qss_name = 'style_dark.qss' if theme_name == 'dark' else 'style_light.qss'
            qss_path = os.path.join(share_dir, 'gui', qss_name)
            if os.path.exists(qss_path):
                with open(qss_path, 'r', encoding='utf-8') as f:
                    self.setStyleSheet(f.read())
            else:
                self.setStyleSheet('')
        except Exception as exc:
            self.node.get_logger().warning(f'Failed to apply theme: {exc}')

    def select_all_packages(self):
        if self.operation == 'idle' and self.snapshot:
            self.explicit_targets = set(self.snapshot.packages)
            self._update_selection()

    def deselect_all_packages(self):
        if self.operation == 'idle':
            self.explicit_targets.clear()
            self._update_selection()

    def select_workspace(self):
        if self.operation != 'idle':
            return
        path = QFileDialog.getExistingDirectory(self, '选择工作空间根目录')
        if path:
            self._scan_workspace(path, restore=True)

    def refresh_packages(self):
        if self.operation == 'idle' and self.workspace_root:
            self._scan_workspace(self.workspace_root, restore=False)

    def _scan_workspace(self, path, restore):
        if self.operation != 'idle':
            return
        try:
            root = validate_root(path)
            environment = dict(os.environ)
            environment['PWD'] = str(root)
            validate_colcon_configuration(root, environment)
            initial = fingerprint(root, environment)
            program = colcon_program(environment)
            targets = (set(self.config.get('workspaces', {}).get(str(root), {}).get(
                'explicit_targets', [])) if restore else set(self.explicit_targets))

            def scanned(result):
                if not result.succeeded:
                    self._report_process_failure(result, '扫描')
                    return
                snapshot = parse_discovery(
                    root, result.stdout, result.stderr, environment, initial)
                if snapshot.errors:
                    for message in (*snapshot.diagnostics, *snapshot.errors):
                        self._append_log('[扫描] ' + message)
                    if self.snapshot and self.snapshot.root == root:
                        self.selection_verified = False
                    self._show_error('候选工作空间存在扫描错误，未替换当前列表；请检查日志')
                    return
                self.snapshot = snapshot
                self.workspace_root = str(root)
                self.workspace_path.setText(str(root))
                self.package_dependencies = snapshot.dependencies
                self.reverse_dependencies = snapshot.reverse_dependencies
                disappeared = targets.difference(snapshot.packages)
                self.explicit_targets = targets.intersection(snapshot.packages)
                if disappeared:
                    self._append_log('已移除不存在的目标: ' + ', '.join(sorted(disappeared)))
                self._fill_packages_table()
                for message in (*snapshot.diagnostics, *snapshot.errors):
                    self._append_log('[扫描] ' + message)
                self._update_selection()

            self._start_process('scanning', program, discovery_arguments(root),
                                root, environment, scanned, capture=True)
        except (OSError, RuntimeError, ValueError) as exc:
            self._show_error(str(exc))

    def _fill_packages_table(self):
        self.package_checkboxes.clear()
        self.packages_table.setRowCount(0)
        for name, package in sorted(self.snapshot.packages.items()):
            row = self.packages_table.rowCount()
            self.packages_table.insertRow(row)
            checkbox = QCheckBox()
            checkbox.stateChanged.connect(
                lambda state, pkg=name: self.on_package_checkbox_changed(pkg, state))
            self.package_checkboxes[name] = checkbox
            container = QWidget()
            layout = QHBoxLayout(container)
            layout.addWidget(checkbox)
            layout.setAlignment(Qt.AlignCenter)
            layout.setContentsMargins(0, 0, 0, 0)
            self.packages_table.setCellWidget(row, 0, container)
            item = QTableWidgetItem(name)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setToolTip(f'{package.path}\n构建类型: {package.build_type}')
            self.packages_table.setItem(row, 1, item)
            size = QTableWidgetItem(self.format_size(self.get_package_size(package.path)))
            size.setFlags(size.flags() & ~Qt.ItemIsEditable)
            self.packages_table.setItem(row, 2, size)
        self._apply_search_filter(self.search_edit.text())

    def _update_selection(self):
        self.selection_verified = False
        self.effective_packages = closure(self.explicit_targets, self.package_dependencies)
        self._apply_selection_widgets()
        self.save_config()
        if not self.snapshot or self.snapshot.errors or not self.explicit_targets:
            self._set_operation('idle')
            return
        snapshot = self.snapshot
        targets = set(self.explicit_targets)
        try:
            if fingerprint(snapshot.root, snapshot.environment, snapshot.packages) != snapshot.fingerprint:
                raise WorkspaceError('包清单已变化，请刷新后重新选择')

            def selected(result):
                if not result.succeeded:
                    self._report_process_failure(result, '依赖校核')
                    return
                if 'ERROR' in result.stderr or 'Traceback' in result.stderr:
                    raise WorkspaceError('colcon 依赖校核报告错误，请检查日志')
                if fingerprint(snapshot.root, snapshot.environment, snapshot.packages) != snapshot.fingerprint:
                    raise WorkspaceError('依赖校核期间清单已变化，请刷新')
                self.effective_packages = parse_selection(
                    result.stdout, targets, snapshot.packages)
                self.selection_verified = True
                self._apply_selection_widgets()
                self.status.showMessage(
                    f'目标 {len(targets)} 个，实际构建集合 {len(self.effective_packages)} 个')

            self._start_process(
                'checking', colcon_program(snapshot.environment),
                discovery_arguments(snapshot.root, targets), snapshot.root,
                snapshot.environment, selected, capture=True)
        except (OSError, RuntimeError, ValueError) as exc:
            self._show_error(str(exc))
            self._set_operation('idle')

    def _apply_selection_widgets(self):
        blockers = [QSignalBlocker(cb) for cb in self.package_checkboxes.values()]
        for name, checkbox in self.package_checkboxes.items():
            derived = name in self.effective_packages and name not in self.explicit_targets
            checkbox.setChecked(name in self.effective_packages)
            checkbox.setEnabled(self.operation == 'idle' and not derived)
            if derived:
                owners = sorted(t for t in self.explicit_targets
                                if name in closure({t}, self.package_dependencies))
                checkbox.setToolTip('自动依赖，请先取消引用它的目标。来源: ' +
                                    (', '.join(owners) or 'colcon 元数据/依赖选择'))
            else:
                checkbox.setToolTip('用户选择的构建目标')
        del blockers

    def build_package(self):
        if self.operation != 'idle' or not self.snapshot or not self.selection_verified:
            return
        snapshot = self.snapshot
        targets = set(self.explicit_targets)
        previous = set(self.effective_packages)
        options = {
            'symlink_install': self.symlink_check.isChecked(),
            'parallel_workers': self.workers_spin.value(),
            'build_type': self.build_type_combo.currentData(),
        }
        try:
            self._operation_lock = workspace_lock(snapshot.root).acquire()
            request = build_request(snapshot, targets, previous, options)

            def checked(result):
                if not result.succeeded:
                    self._release_operation_lock()
                    self._report_process_failure(result, '构建前校核')
                    return
                if 'ERROR' in result.stderr or 'Traceback' in result.stderr:
                    raise WorkspaceError('构建前校核报告错误，请检查日志')
                actual = parse_selection(result.stdout, targets, snapshot.packages)
                if actual != previous:
                    self.effective_packages = actual
                    self._apply_selection_widgets()
                    self._release_operation_lock()
                    self._show_error('实际构建集合发生变化，已更新列表，请检查后重新点击编译')
                    return
                # Recheck manifests and options after the asynchronous query.
                current = build_request(snapshot, targets, actual, options)
                self.log_text.clear()
                self._append_log('工作空间: ' + str(current.root))
                self._append_log('环境来自本次扫描快照，ROS_DISTRO=' +
                                 current.environment.get('ROS_DISTRO', '未知'))
                self._append_log('命令: ' + shlex.join([current.program, *current.arguments]))
                self._start_process('building', current.program, current.arguments,
                                    current.root, current.environment, self._build_completed)

            self._start_process('checking', request.program,
                                discovery_arguments(snapshot.root, targets), snapshot.root,
                                snapshot.environment, checked, capture=True)
        except Exception as exc:
            self._release_operation_lock()
            self._show_error(str(exc))

    def _build_completed(self, result):
        self._release_operation_lock()
        if result.succeeded:
            self.status.showMessage('编译成功')
            self._append_log('编译成功')
            self.save_config()
        else:
            self._report_process_failure(result, '编译')

    def _start_process(self, operation, program, arguments, root, environment, callback,
                       capture=False):
        self._set_operation(operation)
        self._operation_callback = callback
        try:
            self._operation_id = self.runner.start(
                program, list(arguments), root, environment, capture=capture,
                timeout_ms=60000 if capture else 0)
        except Exception:
            self._operation_callback = None
            self._operation_id = None
            self._set_operation('idle')
            raise

    def _on_process_output(self, task_id, line, stderr):
        if task_id == self._operation_id:
            # Discovery results are consumed as data; its diagnostics remain visible.
            if self.operation == 'building' or stderr or self.operation == 'cancelling':
                self._append_log(('[stderr] ' if stderr else '') + line)

    def _on_process_completed(self, result):
        if result.task_id != self._operation_id:
            return
        callback = self._operation_callback
        self._operation_callback = None
        self._operation_id = None
        self._set_operation('idle')
        if self._close_pending:
            self._release_operation_lock()
            QTimer.singleShot(0, self.close)
            return
        try:
            if callback:
                callback(result)
        except Exception as exc:
            self._release_operation_lock()
            self._show_error(str(exc))
        finally:
            if not self.runner.active:
                self._set_operation('idle')

    def _report_process_failure(self, result, label):
        if result.cancelled and not result.error:
            self.status.showMessage(label + '已取消')
            self._append_log(label + '已取消')
            return
        message = result.error or (
            f'{label}失败，退出码 {result.returncode}' if result.normal_exit
            else label + '进程异常退出')
        self._show_error(message)

    def stop_build(self):
        if self.runner.active:
            self._set_operation('cancelling')
            self.runner.cancel()

    def _append_log(self, text):
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(str(text) + '\n')
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

    def _show_error(self, message):
        self._append_log('[错误] ' + message)
        self.status.showMessage(message)
        self.node.get_logger().warning(message)
        if not self._close_pending:
            QMessageBox.warning(self, '操作未完成', message)

    def _release_operation_lock(self):
        if self._operation_lock is not None:
            self._operation_lock.release()
            self._operation_lock = None

    def _set_operation(self, operation):
        self.operation = operation
        busy = operation != 'idle'
        self.progress.setVisible(busy)
        for action in self._workspace_actions:
            action.setEnabled(not busy)
        self.build_primary_btn.setEnabled(
            not busy and self.snapshot is not None and not self.snapshot.errors
            and bool(self.explicit_targets) and self.selection_verified)
        self.clean_secondary_btn.setEnabled(not busy and self.snapshot is not None)
        self.act_stop.setEnabled(operation in ('scanning', 'checking', 'building'))
        for widget in (self.build_type_combo, self.symlink_check, self.workers_spin):
            widget.setEnabled(not busy)
        self.packages_table.setEnabled(not busy)
        self._apply_selection_widgets()
        labels = {'scanning': '正在扫描...', 'checking': '正在校核依赖...',
                  'building': '正在编译...', 'cancelling': '正在停止并等待子进程退出...',
                  'cleaning': '正在清理...'}
        if busy:
            self.status.showMessage(labels.get(operation, operation))

    def closeEvent(self, event):
        if self.runner.active:
            self._close_pending = True
            self.stop_build()
            event.ignore()
            return
        if self.operation == 'cleaning':
            self._close_pending = True
            self.status.showMessage('清理完成后退出...')
            event.ignore()
            return
        if not self.save_config():
            QMessageBox.warning(self, '配置未保存', '本次设置未写入配置文件，请查看日志中的原因。')
        self._release_operation_lock()
        event.accept()

    def clean_workspace(self):
        if self.operation != 'idle' or not self.snapshot:
            return
        try:
            root = self.snapshot.root
            self._operation_lock = workspace_lock(root).acquire()
            self._set_operation('cleaning')
            validate_colcon_configuration(
                root, dict(os.environ), packages=self.snapshot.packages)
            protected = [self.install_prefix, Path(__file__).resolve(),
                         self.source_root, Path(self.config_file), Path(sys.executable)]
            protected.extend(Path(module.__file__).resolve()
                             for module in tuple(sys.modules.values())
                             if getattr(module, '__file__', None))
            plans, errors = {}, {}
            for include_install in (False, True):
                try:
                    plans[include_install] = make_clean_plan(
                        root, include_install, protected, dict(os.environ))
                except (OSError, RuntimeError, ValueError) as exc:
                    errors[include_install] = str(exc)
            if not plans:
                raise WorkspaceError('；'.join(dict.fromkeys(errors.values())))
            box = QMessageBox(self)
            box.setWindowTitle('确认清理范围')
            box.setText('工作空间: ' + str(root) + '\n请选择清理范围。保留根级缓存和标记文件。')
            details = []
            for include_install, plan in plans.items():
                details.append('build 和 install' if include_install else '仅 build')
                for directory in plan.directories:
                    details.extend(('保留 ' if name in directory.preserve else '删除 ') +
                                   str(directory.path / name) for name, _ in directory.entries)
            details.extend('不可用范围: ' + message for message in errors.values())
            box.setDetailedText('\n'.join(details))
            if errors:
                box.setInformativeText('\n'.join(dict.fromkeys(errors.values())))
            build_button = box.addButton('仅清理 build', QMessageBox.AcceptRole)
            all_button = box.addButton('清理 build 和 install', QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            build_button.setEnabled(False in plans)
            all_button.setEnabled(True in plans)
            box.setDefaultButton(QMessageBox.Cancel)
            box.exec_()
            chosen = (False if box.clickedButton() is build_button else
                      True if box.clickedButton() is all_button else None)
            if chosen is None:
                self._release_operation_lock()
                self._set_operation('idle')
                if self._close_pending:
                    QTimer.singleShot(0, self.close)
                return
            worker = _CleanWorker(plans[chosen])
            self._clean_worker = worker
            worker.signals.completed.connect(self._clean_completed)
            QThreadPool.globalInstance().start(worker)
        except (OSError, RuntimeError, ValueError) as exc:
            self._release_operation_lock()
            self._set_operation('idle')
            self._show_error(str(exc))

    def _clean_completed(self, result, error):
        self._clean_worker = None
        self._release_operation_lock()
        self._set_operation('idle')
        if error:
            self._show_error(error)
        elif result.failures:
            for failure in result.failures:
                self._append_log('[清理失败] ' + failure)
            self._show_error(f'部分清理失败：删除 {len(result.removed)} 项，'
                             f'失败 {len(result.failures)} 项，请查看日志')
        else:
            self.status.showMessage(f'清理完成：删除 {len(result.removed)} 项，'
                                    f'保留 {len(result.preserved)} 项')
        if self._close_pending:
            QTimer.singleShot(0, self.close)

    def _apply_search_filter(self, text: str):
        """根据输入文本过滤包列表（不区分大小写）。"""
        text = (text or '').strip().lower()
        if hasattr(self, 'packages_table'):
            for row in range(self.packages_table.rowCount()):
                name_item = self.packages_table.item(row, 1)
                name = name_item.text() if name_item else ''
                visible = (text in name.lower()) if text else True
                self.packages_table.setRowHidden(row, not visible)

    def show_dependency_graph(self):
        """打开依赖关系图对话框。优先显示选中包及其依赖，否则显示全部。"""
        if self.operation != 'idle' or not self.snapshot:
            return

        selected = [pkg for pkg, cb in self.package_checkboxes.items() if cb.isChecked()]
        if selected:
            # 求闭包：选中包及其所有依赖
            nodes = set()
            stack = list(selected)
            while stack:
                p = stack.pop()
                if p in nodes:
                    continue
                nodes.add(p)
                for d in self.package_dependencies.get(p, set()):
                    if d not in nodes:
                        stack.append(d)
        else:
            nodes = set(self.package_checkboxes.keys())

        # 构建子图边
        edges = []
        for src in nodes:
            for dep in self.package_dependencies.get(src, set()):
                if dep in nodes:
                    edges.append((src, dep))

        # 创建并展示对话框
        dlg = QDialog(self)
        dlg.setWindowTitle('包依赖关系图')
        dlg.resize(900, 600)

        layout = QVBoxLayout(dlg)
        view = ZoomableGraphicsView()
        scene = self._build_dependency_scene(nodes, edges, set(selected))
        view.setScene(scene)
        view.setRenderHints(view.renderHints())
        layout.addWidget(view)

        # 自适应内容
        try:
            view.fitInView(scene.itemsBoundingRect(), Qt.KeepAspectRatio)
        except Exception:
            pass

        dlg.setLayout(layout)
        dlg.exec_()

    def _build_dependency_scene(self, nodes, edges, selected_set):
        """根据 nodes/edges 构建简单分层布局图。"""
        scene = DependencyGraphScene()
        scene.edges = [(s, d) for s, d in edges]  # 保存边信息
        scene.theme_name = self.theme_name  # 设置主题

        # 计算层（拓扑层次）
        deps_map = {n: set() for n in nodes}
        for s, d in edges:
            deps_map[s].add(d)
        indeg = {n: 0 for n in nodes}
        for s in nodes:
            for d in deps_map[s]:
                indeg[d] += 1

        levels = []
        current = [n for n in nodes if indeg[n] == 0]
        seen = set()
        while current:
            levels.append(current)
            next_level = []
            for u in current:
                seen.add(u)
                for v in deps_map[u]:
                    indeg[v] -= 1
                    if indeg[v] == 0:
                        next_level.append(v)
            current = next_level
        # 若有剩余（环/未分配），放到最后一层
        remain = [n for n in nodes if n not in seen]
        if remain:
            levels.append(remain)

        # 布局与绘制
        X_SPACING = 240
        Y_SPACING = 80
        RECT_W = 140
        RECT_H = 36

        pos = {}
        for i, layer in enumerate(levels):
            # 居中排列此层
            for j, n in enumerate(sorted(layer)):
                x = i * X_SPACING
                y = j * Y_SPACING
                pos[n] = QPointF(x, y)

        # 先画节点
        for n, p in pos.items():
            rect = QRectF(p.x(), p.y(), RECT_W, RECT_H)

            # 创建可点击的节点项
            node_item = ClickableNodeItem(rect, n, n in selected_set, self.theme_name)
            scene.addItem(node_item)
            scene.node_items[n] = node_item

            # 文本
            text_item = QGraphicsTextItem(n, node_item)
            text_item.setFont(QFont('Sans', 9))
            # 居中放置
            tb = text_item.boundingRect()
            text_item.setPos(rect.center().x() - tb.width()/2, rect.center().y() - tb.height()/2)
            node_item.text_item = text_item
            node_item._update_colors()

        # 再画边
        edge_pen = QPen(QColor(136, 192, 208) if self.theme_name == 'dark' else QColor(100, 100, 100))
        edge_pen.setWidth(1)

        for s, d in edges:
            if s not in scene.node_items or d not in scene.node_items:
                continue
            rs = scene.node_items[s].rect()
            rd = scene.node_items[d].rect()
            start = QPointF(rs.right(), rs.center().y())
            end = QPointF(rd.left(), rd.center().y())
            line_item = scene.addLine(start.x(), start.y(), end.x(), end.y(), edge_pen)

            # 箭头
            arrow_item = None
            try:
                dx = end.x() - start.x()
                dy = end.y() - start.y()
                length = max((dx*dx + dy*dy) ** 0.5, 1.0)
                ux, uy = dx/length, dy/length
                arrow_size = 8
                p1 = end
                p2 = QPointF(end.x() - ux*arrow_size - uy*arrow_size/2, end.y() - uy*arrow_size + ux*arrow_size/2)
                p3 = QPointF(end.x() - ux*arrow_size + uy*arrow_size/2, end.y() - uy*arrow_size - ux*arrow_size/2)
                poly = QPolygonF([p1, p2, p3])
                arrow_item = scene.addPolygon(poly, edge_pen, QBrush(edge_pen.color()))
            except Exception:
                pass

            # 保存边的图形项以便后续更新颜色
            scene.edge_items.append((line_item, arrow_item, s, d))

        # 视图边界
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-40, -40, 80, 80))
        return scene

    def on_package_checkbox_changed(self, package_name, state):
        if self.operation != 'idle':
            return
        if state == Qt.Checked:
            self.explicit_targets.add(package_name)
        else:
            self.explicit_targets.discard(package_name)
        self._update_selection()

    def show_package_context_menu(self, position):
        """显示包列表的右键菜单"""
        item = self.packages_table.itemAt(position)
        if item is None:
            return

        row = item.row()
        package_name_item = self.packages_table.item(row, 1)
        if package_name_item is None:
            return

        package_name = package_name_item.text()

        menu = QMenu(self)

        # 显示包详细信息
        detail_action = QAction('显示包详细信息', self)
        detail_action.triggered.connect(lambda: self.show_package_details(package_name))
        menu.addAction(detail_action)

        # 在鼠标位置显示菜单
        menu.exec_(self.packages_table.mapToGlobal(position))

    def show_package_details(self, package_name):
        """显示包的详细信息对话框"""
        if not self.workspace_root:
            return

        package = self.snapshot.packages.get(package_name) if self.snapshot else None
        if package is None:
            QMessageBox.warning(self, '错误', f'找不到包 {package_name} 的路径')
            return
        package_path = str(package.path)

        # 计算详细的大小信息
        details = self.get_package_detailed_info(package_path)

        # 创建详细信息对话框
        dialog = QDialog(self)
        dialog.setWindowTitle(f'包详细信息 - {package_name}')
        dialog.resize(600, 400)

        layout = QVBoxLayout(dialog)

        # 基本信息
        info_text = QTextEdit()
        info_text.setReadOnly(True)

        info_content = f"""包名: {package_name}
路径: {package_path}
总大小: {self.format_size(details['total_size'])}

文件统计:
- 总文件数: {details['file_count']}
- 普通文件: {details['regular_files']} ({self.format_size(details['regular_size'])})
- 符号链接: {details['symlinks']} ({self.format_size(details['symlink_size'])})
- 跳过的文件: {details['skipped_files']}

目录统计:
- 总目录数: {details['dir_count']}
- 跳过的目录: {details['skipped_dirs']}

大文件 (>100KB):
"""

        for file_info in details['large_files']:
            info_content += f"- {file_info['name']}: {self.format_size(file_info['size'])}\n"

        info_text.setPlainText(info_content)
        layout.addWidget(info_text)

        # 关闭按钮
        close_btn = QPushButton('关闭')
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn)

        dialog.exec_()

    def get_package_detailed_info(self, package_path):
        """获取包的详细信息"""
        details = {
            'total_size': 0,
            'file_count': 0,
            'regular_files': 0,
            'regular_size': 0,
            'symlinks': 0,
            'symlink_size': 0,
            'skipped_files': 0,
            'dir_count': 0,
            'skipped_dirs': 0,
            'large_files': []
        }

        try:
            seen_inodes = set()

            for dirpath, dirnames, filenames in os.walk(package_path):
                details['dir_count'] += 1

                # 跳过一些常见的大型缓存目录
                original_dirs = dirnames[:]
                dirnames[:] = [d for d in dirnames if d not in ['.git', '__pycache__', '.pytest_cache', 'build', '.vscode']]
                details['skipped_dirs'] += len(original_dirs) - len(dirnames)

                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    details['file_count'] += 1

                    try:
                        # 获取文件状态
                        stat_info = os.lstat(filepath)

                        # 检查是否是硬链接（避免重复计算）
                        inode = (stat_info.st_dev, stat_info.st_ino)
                        if inode in seen_inodes:
                            continue
                        seen_inodes.add(inode)

                        if os.path.isfile(filepath) and not os.path.islink(filepath):
                            # 普通文件
                            details['regular_files'] += 1
                            details['regular_size'] += stat_info.st_size
                            details['total_size'] += stat_info.st_size

                            # 记录大文件
                            if stat_info.st_size > 100 * 1024:  # 大于100KB
                                relative_path = os.path.relpath(filepath, package_path)
                                details['large_files'].append({
                                    'name': relative_path,
                                    'size': stat_info.st_size
                                })

                        elif os.path.islink(filepath):
                            # 符号链接
                            details['symlinks'] += 1
                            link_size = len(os.readlink(filepath))
                            details['symlink_size'] += link_size
                            details['total_size'] += link_size

                    except (OSError, IOError):
                        details['skipped_files'] += 1
                        continue

            # 按大小排序大文件列表
            details['large_files'].sort(key=lambda x: x['size'], reverse=True)
            # 只保留前10个最大的文件
            details['large_files'] = details['large_files'][:10]

        except Exception as e:
            self.node.get_logger().warning(f"Error getting package details: {e}")

        return details
