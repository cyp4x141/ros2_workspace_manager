"""Construct reproducible requests without executing a shell."""

import shutil
from types import MappingProxyType

from .models import BuildRequest
from .package_scanner import WorkspaceError, fingerprint, validate_colcon_configuration


def colcon_program(environment):
    program = shutil.which('colcon', path=environment.get('PATH', ''))
    if not program:
        raise WorkspaceError('找不到 colcon，请安装所需扩展并在 ROS 环境中启动工具')
    return program


def build_request(snapshot, targets, effective_packages, config):
    if snapshot.errors:
        raise WorkspaceError('工作空间包含扫描错误，暂不能构建')
    if not targets or not set(targets).issubset(snapshot.packages):
        raise WorkspaceError('目标包为空或已不在工作空间中')
    if fingerprint(snapshot.root, snapshot.environment, snapshot.packages) != snapshot.fingerprint:
        raise WorkspaceError('包清单或 colcon 配置已变化，请刷新工作空间')
    symlink = config['symlink_install']
    options = validate_colcon_configuration(
        snapshot.root, snapshot.environment, symlink, snapshot.packages)
    root = snapshot.root
    for name in ('build', 'install'):
        path = root / name
        if path.is_symlink():
            raise WorkspaceError(f'本版本不支持产物根目录符号链接: {path}')
    arguments = [
        'build', '--base-paths', str(root / 'src'),
        '--build-base', str(root / 'build'), '--install-base', str(root / 'install'),
        '--executor', 'parallel', '--parallel-workers', str(config['parallel_workers']),
        '--packages-up-to', *sorted(targets),
    ]
    if symlink:
        arguments.append('--symlink-install')
    layout_file = root / 'install/.colcon_install_layout'
    try:
        layout = layout_file.read_text().strip()
    except FileNotFoundError:
        layout = ''
    if layout not in ('', 'isolated', 'merged'):
        raise WorkspaceError(f'无法识别安装布局: {layout_file}')
    if options.get('merge-install') and layout == 'isolated':
        raise WorkspaceError('colcon 的 merge-install 设置与现有 isolated 安装冲突')
    if layout == 'merged' or options.get('merge-install'):
        arguments.append('--merge-install')
    build_type = config['build_type']
    if build_type in ('Release', 'Debug') and any(
            snapshot.packages[name].build_type in ('ament_cmake', 'cmake', 'catkin')
            for name in effective_packages):
        arguments.extend(['--cmake-args', f'-DCMAKE_BUILD_TYPE={build_type}'])
    return BuildRequest(
        root, tuple(sorted(targets)), tuple(sorted(effective_packages)),
        colcon_program(snapshot.environment), tuple(arguments),
        MappingProxyType(dict(snapshot.environment)))
