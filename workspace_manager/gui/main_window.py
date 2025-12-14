"""PyQt5 UI for the ROS2 Workspace Manager with improved styling and UX."""

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QCheckBox,
    QFileDialog, QGroupBox,
    QSpinBox, QToolBar, QAction, QLineEdit, QTextEdit,
    QStatusBar, QComboBox, QSplitter, QStyle, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView, QDialog,
    QGraphicsView, QGraphicsScene, QMenu, QGraphicsRectItem, QGraphicsTextItem,
)
from PyQt5.QtCore import Qt, QProcess, QSize, QPointF, QRectF
from PyQt5.QtGui import QPen, QBrush, QColor, QFont, QPolygonF, QWheelEvent
import os
import yaml
import xml.etree.ElementTree as ET
import shutil
from ament_index_python.packages import get_package_share_directory


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


class WorkspaceManagerGUI(QMainWindow):
    def __init__(self, node):
        super().__init__()
        self.node = node
        self.workspace_root = None
        self.package_checkboxes = {}
        self.config_file = os.path.join(
            get_package_share_directory('workspace_manager'),
            'config',
            'config.yaml'
        )
        self.load_config()
        self.build_process = None
        self.theme_name = self.config.get('theme', 'dark')
        self.setupUI()
        # 应用主题
        try:
            self.apply_theme(self.theme_name)
        except Exception:
            pass
        self.package_dependencies = {}  # 存储包的依赖关系
        self.reverse_dependencies = {}  # 存储反向依赖关系
        self.always_on_top = False  # 添加一个标志来跟踪窗口是否置顶

        # 如果配置文件中有工作空间路径，则加载它
        if self.config.get('workspace_path'):
            self.workspace_root = self.config['workspace_path']
            self.workspace_path.setText(self.workspace_root)
            self.refresh_packages()

    def get_package_dependencies(self, package_xml_path):
        """解析package.xml获取依赖关系"""
        try:
            tree = ET.parse(package_xml_path)
            root = tree.getroot()
            deps = set()

            # 检查所有类型的依赖
            for dep_type in ['depend', 'build_depend', 'build_export_depend',
                             'exec_depend', 'test_depend']:
                for dep in root.findall(dep_type):
                    if dep.text:
                        deps.add(dep.text)
            return deps
        except (ET.ParseError, AttributeError):
            return set()

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
        try:
            with open(self.config_file, 'r') as f:
                self.config = yaml.safe_load(f)
        except (FileNotFoundError, yaml.YAMLError):
            # 默认配置
            self.config = {
                'workspace_path': '',
                'last_selected_packages': [],
                'symlink_install': True,
                'always_on_top': False,  # 添加新的配置项
                'parallel_workers': os.cpu_count() or 8,
                'theme': 'dark',
                'build_type': 'auto',
            }

    def save_config(self):
        self.config['workspace_path'] = self.workspace_root or ''
        self.config['last_selected_packages'] = [
            pkg for pkg, cb in self.package_checkboxes.items()
            if cb.isChecked()
        ]
        self.config['symlink_install'] = self.symlink_check.isChecked()
        self.config['always_on_top'] = self.always_on_top  # 保存置顶状态
        # 保存并同步并行编译线程数
        try:
            self.config['parallel_workers'] = int(self.workers_spin.value())
        except Exception:
            self.config['parallel_workers'] = os.cpu_count() or 8
        # 保存主题
        try:
            self.config['theme'] = self.theme_combo.currentData() or self.theme_name
        except Exception:
            self.config['theme'] = self.theme_name
        # 保存构建类型（使用 data 存储）
        try:
            self.config['build_type'] = self.build_type_combo.currentData()
        except Exception:
            self.config['build_type'] = 'auto'

        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        with open(self.config_file, 'w') as f:
            yaml.dump(self.config, f)

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

        options_layout.addWidget(QLabel('并行线程'))
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
            print(f"无法设置X11窗口属性: {e}")

    def _create_toolbar_actions(self):
        """创建工具栏动作并绑定（文字按钮）。"""

        act_select_ws = QAction('选择工作空间', self)
        act_select_ws.triggered.connect(self.select_workspace)
        self.toolbar.addAction(act_select_ws)

        act_refresh = QAction('刷新', self)
        act_refresh.triggered.connect(self.refresh_packages)
        self.toolbar.addAction(act_refresh)

        self.toolbar.addSeparator()

        act_select_all = QAction('全选', self)
        act_select_all.triggered.connect(self.select_all_packages)
        self.toolbar.addAction(act_select_all)

        act_deselect_all = QAction('全不选', self)
        act_deselect_all.triggered.connect(self.deselect_all_packages)
        self.toolbar.addAction(act_deselect_all)

        self.toolbar.addSeparator()

        # 依赖关系图
        act_graph = QAction('依赖关系图', self)
        act_graph.triggered.connect(self.show_dependency_graph)
        self.toolbar.addAction(act_graph)

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
            share_dir = get_package_share_directory('workspace_manager')
            qss_name = 'style_dark.qss' if theme_name == 'dark' else 'style_light.qss'
            qss_path = os.path.join(share_dir, 'gui', qss_name)
            if os.path.exists(qss_path):
                with open(qss_path, 'r', encoding='utf-8') as f:
                    self.setStyleSheet(f.read())
            else:
                self.setStyleSheet('')
        except Exception as exc:
            print(f'Failed to apply theme: {exc}')

    def select_all_packages(self):
        for checkbox in self.package_checkboxes.values():
            checkbox.setChecked(True)

    def deselect_all_packages(self):
        for checkbox in self.package_checkboxes.values():
            checkbox.setChecked(False)

    def select_workspace(self):
        dir_path = QFileDialog.getExistingDirectory(self, 'Select Workspace Root')
        if dir_path:
            self.workspace_root = dir_path
            self.workspace_path.setText(dir_path)
            self.refresh_packages()
            self.save_config()

    def get_package_name_from_xml(self, package_xml_path):
        try:
            tree = ET.parse(package_xml_path)
            root = tree.getroot()
            return root.find('name').text
        except (ET.ParseError, AttributeError):
            return None

    def refresh_packages(self):
        if not self.workspace_root:
            QMessageBox.warning(self, 'Error', 'Please select workspace first!')
            return

        src_dir = os.path.join(self.workspace_root, 'src')
        if not os.path.exists(src_dir):
            QMessageBox.warning(self, 'Error', 'src directory not found!')
            return

        # 清空现有的表格条目
        try:
            self.packages_table.setRowCount(0)
        except Exception:
            pass
        self.package_checkboxes.clear()
        self.package_dependencies.clear()
        self.reverse_dependencies.clear()

        # 第一遍：收集所有包和它们的依赖
        available_packages = {}
        package_paths = {}  # 存储包路径用于计算大小
        for root, dirs, files in os.walk(src_dir):
            if 'package.xml' in files:
                package_xml_path = os.path.join(root, 'package.xml')
                package_name = self.get_package_name_from_xml(package_xml_path)
                if package_name:
                    available_packages[package_name] = package_xml_path
                    package_paths[package_name] = root  # 存储包的根目录路径
                    self.package_dependencies[package_name] = set()
                    self.reverse_dependencies[package_name] = set()

        # 第二遍：构建依赖关系
        for package_name, xml_path in available_packages.items():
            deps = self.get_package_dependencies(xml_path)
            # 只保留工作空间内的依赖
            workspace_deps = deps.intersection(available_packages.keys())
            self.package_dependencies[package_name] = workspace_deps
            # 构建反向依赖
            for dep in workspace_deps:
                self.reverse_dependencies[dep].add(package_name)

        # 填充表格
        for package_name in sorted(available_packages.keys()):
            row = self.packages_table.rowCount()
            self.packages_table.insertRow(row)

            # 选择列：复选框（居中显示）
            checkbox = QCheckBox()
            if package_name in self.config.get('last_selected_packages', []):
                checkbox.setChecked(True)
            checkbox.stateChanged.connect(
                lambda state, pkg=package_name: self.on_package_checkbox_changed(pkg, state)
            )
            
            # 创建一个容器widget来让复选框居中
            checkbox_widget = QWidget()
            checkbox_layout = QHBoxLayout(checkbox_widget)
            checkbox_layout.addWidget(checkbox)
            checkbox_layout.setAlignment(Qt.AlignCenter)
            checkbox_layout.setContentsMargins(0, 0, 0, 0)
            
            self.packages_table.setCellWidget(row, 0, checkbox_widget)

            # 包名列
            item = QTableWidgetItem(package_name)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.packages_table.setItem(row, 1, item)

            # 包大小列
            package_path = package_paths[package_name]
            package_size = self.get_package_size(package_path)
            size_text = self.format_size(package_size)
            size_item = QTableWidgetItem(size_text)
            size_item.setFlags(size_item.flags() & ~Qt.ItemIsEditable)
            self.packages_table.setItem(row, 2, size_item)

            self.package_checkboxes[package_name] = checkbox

        # 应用搜索过滤
        try:
            self._apply_search_filter(self.search_edit.text())
        except Exception:
            pass


    def build_package(self):
        """使用 QProcess 启动编译并将日志实时输出到右侧面板。"""
        if not self.workspace_root:
            QMessageBox.warning(self, 'Error', 'Please select workspace first!')
            return

        selected_packages = [
            pkg for pkg, cb in self.package_checkboxes.items() if cb.isChecked()
        ]
        if not selected_packages:
            QMessageBox.warning(self, 'Error', 'Please select at least one package!')
            return

        cmd = ['colcon', 'build']
        if self.symlink_check.isChecked():
            cmd.append('--symlink-install')
        workers = int(self.config.get('parallel_workers', os.cpu_count() or 8))
        cmd.extend(['--parallel-workers', str(workers)])
        # 构建类型（CMAKE_BUILD_TYPE）
        build_type = self.config.get('build_type', 'auto')
        if build_type in ['Release', 'Debug']:
            cmd.extend(['--cmake-args', f'-DCMAKE_BUILD_TYPE={build_type}'])
        cmd.extend(['--packages-select'])
        cmd.extend(selected_packages)

        # 准备 UI
        self.log_text.clear()
        self.status.showMessage('开始编译...')
        self.progress.setVisible(True)
        self._set_building_ui_state(True)

        # 启动进程
        self.build_process = QProcess(self)
        self.build_process.setProgram(cmd[0])
        self.build_process.setArguments(cmd[1:])
        self.build_process.setWorkingDirectory(self.workspace_root)
        self.build_process.readyReadStandardOutput.connect(self._read_build_stdout)
        self.build_process.readyReadStandardError.connect(self._read_build_stderr)
        self.build_process.finished.connect(self._on_build_finished)
        self.build_process.errorOccurred.connect(self._on_build_error)
        self.build_process.start()
        if not self.build_process.waitForStarted(3000):
            self._append_log('[错误] 无法启动构建进程。')
            self.progress.setVisible(False)
            self._set_building_ui_state(False)

    def stop_build(self):
        """停止当前编译。"""
        if self.build_process and self.build_process.state() != QProcess.NotRunning:
            self.build_process.kill()
            self.build_process.waitForFinished(1000)
            self.status.showMessage('已停止编译')
            self.progress.setVisible(False)
            self._set_building_ui_state(False)

    def _append_log(self, text: str):
        self.log_text.append(text.rstrip())

    def _read_build_stdout(self):
        data = bytes(self.build_process.readAllStandardOutput()).decode('utf-8', 'ignore')
        for line in data.splitlines():
            self._append_log(line)

    def _read_build_stderr(self):
        data = bytes(self.build_process.readAllStandardError()).decode('utf-8', 'ignore')
        for line in data.splitlines():
            self._append_log(f"[ERR] {line}")

    def _on_build_finished(self, code: int, _status):
        self.progress.setVisible(False)
        self._set_building_ui_state(False)
        if code == 0:
            self.status.showMessage('编译成功')
            QMessageBox.information(self, 'Success', 'Build completed successfully!')
            self.save_config()
        else:
            self.status.showMessage('编译失败')
            QMessageBox.critical(self, 'Error', 'Build failed. 请查看日志。')

    def _on_build_error(self, _err):
        self.progress.setVisible(False)
        self._set_building_ui_state(False)
        self.status.showMessage('构建进程启动失败')
        QMessageBox.critical(self, 'Error', 'Failed to start build process.')

    def _set_building_ui_state(self, building: bool):
        # 在构建期间禁用部分控件
        if hasattr(self, 'act_build'):
            self.act_build.setEnabled(not building)
        if hasattr(self, 'act_stop'):
            self.act_stop.setEnabled(building)
        if hasattr(self, 'build_primary_btn'):
            self.build_primary_btn.setEnabled(not building)
        if hasattr(self, 'clean_secondary_btn'):
            self.clean_secondary_btn.setEnabled(not building)
        if hasattr(self, 'build_type_combo'):
            self.build_type_combo.setEnabled(not building)
        self.symlink_check.setEnabled(not building)
        self.workers_spin.setEnabled(not building)
        if hasattr(self, 'packages_table'):
            self.packages_table.setEnabled(not building)
        for cb in self.package_checkboxes.values():
            cb.setEnabled(not building)


    def closeEvent(self, event):
        self.save_config()
        super().closeEvent(event)

    def clean_workspace(self):
        if not self.workspace_root:
            QMessageBox.warning(self, 'Error', 'Please select workspace first!')
            return

        reply = QMessageBox.question(self, 'Confirm Clean',
                                     'This will clean both build and install directories while preserving specific files.\n'
                                     'Are you sure you want to continue?',
                                     QMessageBox.Yes | QMessageBox.No)

        if reply == QMessageBox.Yes:
            build_dir = os.path.join(self.workspace_root, 'build')
            install_dir = os.path.join(self.workspace_root, 'install')

            if not (os.path.exists(build_dir) or os.path.exists(install_dir)):
                QMessageBox.warning(self, 'Error', 'Neither build nor install directory found!')
                return

            try:
                import shutil

                def remove_contents(directory):
                    for item in os.listdir(directory):
                        item_path = os.path.join(directory, item)

                        # 跳过特定目录
                        if any(skip in item_path for skip in ['.cache', '.idea']):
                            continue

                        # 如果是文件
                        if os.path.isfile(item_path):
                            # 跳过需要保留的文件
                            if item in ['COLCON_IGNORE', 'compile_commands.json', '.built_by']:
                                continue
                            try:
                                os.remove(item_path)
                            except OSError as e:
                                self.node.get_logger().warning(f"Failed to remove file {item_path}: {e}")

                        # 如果是目录
                        elif os.path.isdir(item_path):
                            try:
                                shutil.rmtree(item_path)
                            except OSError as e:
                                self.node.get_logger().warning(f"Failed to remove directory {item_path}: {e}")

                # 清理 build 目录
                if os.path.exists(build_dir):
                    remove_contents(build_dir)

                # 清理 install 目录
                if os.path.exists(install_dir):
                    remove_contents(install_dir)

                QMessageBox.information(self, 'Success',
                                        'Clean completed successfully!\n'
                                        'Both build and install directories have been cleaned.')

            except Exception as e:
                QMessageBox.critical(self, 'Error', f'Clean failed: {str(e)}')
                self.node.get_logger().error(f'Clean failed: {str(e)}')

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
        # 准备依赖信息
        if not self.package_dependencies:
            try:
                self.refresh_packages()
            except Exception:
                pass

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
        """处理包选择状态改变"""
        if self.package_checkboxes[package_name].isChecked():
            # 如果选中了一个包，递归选中其所有依赖
            self.select_dependencies(package_name)
        else:
            # 如果取消选中一个包，递归取消选中依赖它的包
            self.deselect_dependent_packages(package_name)

    def select_dependencies(self, package_name, visited=None):
        """递归选中所有依赖的包"""
        if visited is None:
            visited = set()

        if package_name in visited:
            return
        visited.add(package_name)

        # 选中当前包
        if package_name in self.package_checkboxes:
            self.package_checkboxes[package_name].setChecked(True)

        # 递归选中所有依赖
        for dep in self.package_dependencies.get(package_name, set()):
            self.select_dependencies(dep, visited)

    def deselect_dependent_packages(self, package_name, visited=None):
        """递归取消选中依赖此包的包"""
        if visited is None:
            visited = set()

        if package_name in visited:
            return
        visited.add(package_name)

        # 取消选中依赖此包的所有包
        for dep in self.reverse_dependencies.get(package_name, set()):
            if dep in self.package_checkboxes:
                self.package_checkboxes[dep].setChecked(False)
                self.deselect_dependent_packages(dep, visited)

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
            
        # 查找包路径
        src_dir = os.path.join(self.workspace_root, 'src')
        package_path = None
        
        for root, dirs, files in os.walk(src_dir):
            if 'package.xml' in files:
                package_xml_path = os.path.join(root, 'package.xml')
                found_name = self.get_package_name_from_xml(package_xml_path)
                if found_name == package_name:
                    package_path = root
                    break
        
        if not package_path:
            QMessageBox.warning(self, '错误', f'找不到包 {package_name} 的路径')
            return
            
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
            print(f"Error getting package details: {e}")
            
        return details
