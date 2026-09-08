"""Locate the tool source tree and preserve its local configuration."""

import copy
import hashlib
import os
from pathlib import Path
import tempfile
import uuid
import xml.etree.ElementTree as ET

import yaml

from .locks import FileLock


class ConfigError(RuntimeError):
    """Configuration cannot be located or safely saved."""


def defaults():
    return {
        'schema_version': 2,
        'workspace_path': '',
        'workspaces': {},
        'symlink_install': True,
        'always_on_top': False,
        'parallel_workers': os.cpu_count() or 1,
        'theme': 'dark',
        'build_type': 'auto',
    }


def _is_source_root(path):
    try:
        return (ET.parse(path / 'package.xml').getroot().findtext('name')
                == 'workspace_manager'
                and (path / 'workspace_manager/workspace_manager_node.py').is_file())
    except (OSError, ET.ParseError):
        return False


def locate_source_root(module_path, install_prefix, environment):
    """Use a verified source tree, never the managed workspace or the cwd."""
    override = environment.get('WORKSPACE_MANAGER_SOURCE_ROOT')
    if override:
        candidate = Path(override).expanduser().resolve()
        if _is_source_root(candidate):
            return candidate
        raise ConfigError(f'WORKSPACE_MANAGER_SOURCE_ROOT 不是工具源码项目: {candidate}')
    for candidate in Path(module_path).resolve().parents:
        if _is_source_root(candidate):
            return candidate
    prefix = Path(install_prefix).resolve()
    for ancestor in (prefix, *tuple(prefix.parents)[:3]):
        source_dir = ancestor / 'src'
        if not source_dir.is_dir():
            continue
        matches = set()
        for directory, dirs, files in os.walk(source_dir):
            if 'COLCON_IGNORE' in files:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in {'.git', 'build', 'install', 'log'}]
            if 'package.xml' in files:
                candidate = Path(directory).resolve()
                if _is_source_root(candidate):
                    matches.add(candidate)
                dirs[:] = []
        if len(matches) == 1:
            return matches.pop()
        if matches:
            raise ConfigError('发现多个工具源码目录，请设置 WORKSPACE_MANAGER_SOURCE_ROOT')
    raise ConfigError(
        '无法定位工具源码。请将 WORKSPACE_MANAGER_SOURCE_ROOT 设置为包含 '
        'package.xml 的 ros2_workspace_manager 源码目录。')


def _digest(content):
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _workspace_path(value):
    """Keep workspace identities absolute inside the application."""
    return str(Path(value).expanduser().resolve())


def _serialized(data):
    """Use home-relative paths on disk without changing the in-memory keys."""
    home = Path.home().resolve()

    def portable(value):
        if not value:
            return ''
        try:
            relative = Path(value).relative_to(home)
        except ValueError:
            return value
        return str(Path('~') / relative)

    result = copy.deepcopy(data)
    result['workspace_path'] = portable(data['workspace_path'])
    result['workspaces'] = {
        portable(root): session for root, session in result['workspaces'].items()
    }
    return result


class ConfigStore:
    """Validate, migrate and atomically save one source-local YAML file."""

    def __init__(self, source_root):
        self.path = Path(source_root) / 'workspace_manager/config/config.yaml'
        self.warnings = []
        self.read_only = False
        self._expected = None
        self._backup_required = False
        self.data = defaults()
        self.load()

    def _read(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def load(self):
        self.warnings.clear()
        self.read_only = False
        self._backup_required = False
        try:
            raw = self._read()
        except OSError as exc:
            self.read_only = True
            self.warnings.append(f'配置无法读取，当前使用内存默认值: {exc}')
            self.data = defaults()
            return
        self._expected = _digest(raw)
        try:
            loaded = yaml.safe_load(raw) if raw is not None else {}
        except (yaml.YAMLError, UnicodeError) as exc:
            loaded = {}
            self._backup_required = raw is not None
            self.warnings.append(f'配置格式错误，使用默认值；原文件将在保存前备份: {exc}')
        if not isinstance(loaded, dict):
            loaded = {}
            self._backup_required = raw is not None
            self.warnings.append('配置内容不是字典，使用默认值')
        version = loaded.get('schema_version', 1)
        if type(version) is not int or version not in (1, 2):
            self.read_only = True
            self.warnings.append(f'不支持配置版本 {version!r}，禁止覆盖原文件')
        self.data = self._validated(loaded)
        if version == 1:
            workspace = self.data['workspace_path']
            previous = loaded.get('last_selected_packages', [])
            if workspace and isinstance(previous, list):
                session = self.data['workspaces'].setdefault(workspace, {})
                session['explicit_targets'] = sorted(
                    set(session.get('explicit_targets', [])) | {
                        name.strip() for name in previous
                        if isinstance(name, str) and name.strip()})
            self.data.pop('last_selected_packages', None)
            self._backup_required = raw is not None
        if raw is None and not self.read_only:
            try:
                self.save(self.data)
            except (ConfigError, OSError, RuntimeError) as exc:
                self.warnings.append(f'默认配置无法创建，当前设置不会自动持久化: {exc}')

    def _validated(self, loaded):
        data = copy.deepcopy(loaded)
        baseline = defaults()
        for key, value in baseline.items():
            data.setdefault(key, value)
        for key in ('symlink_install', 'always_on_top'):
            if type(data[key]) is not bool:
                self.warnings.append(f'{key} 必须为布尔值，已使用默认值')
                data[key] = baseline[key]
        for key, allowed in [('theme', ('light', 'dark')),
                             ('build_type', ('auto', 'Release', 'Debug'))]:
            if data[key] not in allowed:
                self.warnings.append(f'{key} 取值无效，已使用默认值')
                data[key] = baseline[key]
        workers = data['parallel_workers']
        if type(workers) is not int or workers < 1:
            self.warnings.append('parallel_workers 必须为正整数，已使用默认值')
            workers = baseline['parallel_workers']
        data['parallel_workers'] = min(workers, os.cpu_count() or 1)
        if (not isinstance(data['workspace_path'], str)
                or '\x00' in data['workspace_path']):
            self.warnings.append('workspace_path 必须为字符串，已清空')
            data['workspace_path'] = ''
        elif data['workspace_path']:
            try:
                data['workspace_path'] = _workspace_path(data['workspace_path'])
            except (OSError, ValueError, RuntimeError):
                self.warnings.append('workspace_path 无法规范化，已清空')
                data['workspace_path'] = ''
        sessions = data['workspaces']
        clean_sessions = {}
        if not isinstance(sessions, dict):
            self.warnings.append('workspaces 必须为字典，已清空')
            sessions = {}
        for root, session in sessions.items():
            if not isinstance(root, str) or not root or not isinstance(session, dict):
                self.warnings.append('已忽略无效工作空间记录')
                continue
            targets = session.get('explicit_targets', [])
            clean_session = copy.deepcopy(session)
            clean_session['explicit_targets'] = sorted({
                t.strip() for t in targets if isinstance(t, str) and t.strip()
            }) if isinstance(targets, list) else []
            try:
                key = _workspace_path(root)
            except (OSError, ValueError, RuntimeError):
                self.warnings.append(f'已忽略无法规范化的工作空间路径: {root!r}')
                continue
            if key in clean_sessions:
                previous = clean_sessions[key]
                clean_session['explicit_targets'] = sorted(
                    set(previous['explicit_targets']) | set(clean_session['explicit_targets']))
                clean_session = {**previous, **clean_session}
                self.warnings.append('同一工作空间存在不同路径写法，已合并包选择')
            clean_sessions[key] = clean_session
        data['workspaces'] = clean_sessions
        data['schema_version'] = 2
        return data

    def targets_for(self, root):
        key = _workspace_path(root)
        return set(self.data['workspaces'].get(key, {}).get('explicit_targets', []))

    def save(self, data):
        if self.read_only:
            raise ConfigError('当前配置只读，请解决读取错误或配置版本问题后重新加载')
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or self.path.is_symlink():
            raise ConfigError('配置目录或配置文件是符号链接，未覆盖其目标')
        candidate = self._validated(data)
        payload = yaml.safe_dump(
            _serialized(candidate), allow_unicode=True, sort_keys=False).encode('utf-8')
        with FileLock(directory / '.config.lock'):
            current = self._read()
            if _digest(current) != self._expected:
                raise ConfigError('配置已被其他实例或编辑器修改，请重新加载配置后再保存')
            if self._backup_required and current is not None:
                backup = directory / ('config.yaml.bak.' + uuid.uuid4().hex)
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(current)
                    stream.flush()
                    os.fsync(stream.fileno())
            name = None
            try:
                fd, name = tempfile.mkstemp(prefix='.config-', suffix='.tmp', dir=directory)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
                name = None
                self._expected = _digest(payload)
                self._backup_required = False
                self.data = candidate
            finally:
                if name is not None:
                    os.unlink(name)
