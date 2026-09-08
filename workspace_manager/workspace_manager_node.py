#!/usr/bin/env python3
"""ROS entry point and lifetime management for the desktop tool."""

import os
import sys

from ament_index_python.packages import get_package_prefix
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QMessageBox
import rclpy
from rclpy.node import Node

from .core.config_store import ConfigStore, locate_source_root
from .gui.main_window import WorkspaceManagerGUI


class WorkspaceManagerNode(Node):
    def __init__(self):
        super().__init__('workspace_manager_node')
        self.get_logger().info('Workspace Manager Node started')


def main(args=None):
    rclpy.init(args=args)
    app = QApplication(sys.argv)
    node = None
    result = 1
    try:
        if sys.platform != 'linux':
            raise RuntimeError('此版本的进程组与清理保护仅支持 Linux')
        node = WorkspaceManagerNode()
        prefix = get_package_prefix('workspace_manager')
        source = locate_source_root(__file__, prefix, os.environ)
        store = ConfigStore(source)
        QIcon.setThemeName('Adwaita')
        if not QIcon.hasThemeIcon('folder'):
            QIcon.setThemeName('hicolor')
        window = WorkspaceManagerGUI(node, store, source, prefix)
        window.setWindowIcon(QIcon(str(source / 'workspace_manager/icon/icon.jpg')))
        window.show()
        result = app.exec_()
    except (OSError, RuntimeError, ValueError) as exc:
        if node is not None:
            node.get_logger().error(str(exc))
        QMessageBox.critical(None, 'Workspace Manager 启动失败', str(exc))
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return result


if __name__ == '__main__':
    sys.exit(main())
