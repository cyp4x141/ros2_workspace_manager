"""Plan and perform bounded cleanup without following directory symlinks."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
from typing import Tuple

from .package_scanner import validate_colcon_configuration, validate_root


class CleanError(RuntimeError):
    """Cleanup cannot proceed within the agreed boundaries."""


@dataclass(frozen=True)
class CleanDirectory:
    path: Path
    identity: Tuple[int, int]
    entries: Tuple[Tuple[str, Tuple[int, int, int]], ...]
    preserve: Tuple[str, ...]


@dataclass(frozen=True)
class CleanPlan:
    root: Path
    root_identity: Tuple[int, int]
    directories: Tuple[CleanDirectory, ...]


@dataclass(frozen=True)
class CleanResult:
    removed: Tuple[str, ...]
    preserved: Tuple[str, ...]
    failures: Tuple[str, ...]


def _identity(info):
    return info.st_dev, info.st_ino


def _entry_identity(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def make_clean_plan(root, include_install, protected_paths, environment):
    root = validate_root(root)
    validate_colcon_configuration(root, environment)
    if not shutil.rmtree.avoids_symlink_attacks:
        raise CleanError('当前 Python 不提供所需的目录删除保护，已停止清理')
    directories = []
    for name in (('build', 'install') if include_install else ('build',)):
        path = root / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise CleanError(f'产物根目录不是普通目录，未进入链接目标: {path}')
        for protected in protected_paths:
            protected = Path(protected).resolve()
            if os.path.commonpath((str(protected), str(path))) == str(path):
                raise CleanError(
                    f'{path} 包含当前工具正在使用的文件或安装前缀。'
                    '请仅清理 build，或从另一管理工作空间启动后清理。')
        preserve = ({'.cache', '.idea', 'COLCON_IGNORE', 'compile_commands.json', '.built_by'}
                    if name == 'build' else {'COLCON_IGNORE', '.colcon_install_layout'})
        entries = tuple(sorted(
            (entry.name, _entry_identity(entry.lstat())) for entry in path.iterdir()
        ))
        directories.append(CleanDirectory(path, _identity(info), entries, tuple(sorted(preserve))))
    if not directories:
        raise CleanError('所选范围没有可清理的产物目录')
    return CleanPlan(root, _identity(root.stat()), tuple(directories))


def execute_clean_plan(plan):
    """Use open directory descriptors to prevent redirection during deletion."""
    removed, preserved, failures = [], [], []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = os.open(str(plan.root), flags)
    try:
        if _identity(os.fstat(root_fd)) != plan.root_identity:
            raise CleanError('工作空间目录已被替换，请重新生成清理计划')
        for directory in plan.directories:
            fd = None
            try:
                fd = os.open(directory.path.name, flags, dir_fd=root_fd)
                if _identity(os.fstat(fd)) != directory.identity:
                    raise CleanError('产物目录已被替换')
                current = {
                    name: _entry_identity(os.stat(name, dir_fd=fd, follow_symlinks=False))
                    for name in os.listdir(fd)
                }
                if current != dict(directory.entries):
                    raise CleanError('目录内容在确认后发生变化，请重新生成清理计划')
                for name, expected in directory.entries:
                    label = str(directory.path / name)
                    if name in directory.preserve:
                        preserved.append(label)
                        continue
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if _entry_identity(info) != expected:
                            raise CleanError('条目在清理期间被替换')
                        if stat.S_ISDIR(info.st_mode):
                            # Python 3.10 rmtree has no dir_fd argument. /proc/self/fd
                            # anchors its symlink-resistant implementation to our open fd.
                            shutil.rmtree(f'/proc/self/fd/{fd}/{name}')
                        else:
                            os.unlink(name, dir_fd=fd)
                        removed.append(label)
                    except (OSError, CleanError) as exc:
                        failures.append(f'{label}: {exc}')
            except (OSError, CleanError) as exc:
                failures.append(f'{directory.path}: {exc}')
            finally:
                if fd is not None:
                    os.close(fd)
    finally:
        os.close(root_fd)
    return CleanResult(tuple(removed), tuple(preserved), tuple(failures))
