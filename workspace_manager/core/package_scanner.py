"""Convert colcon discovery output into a diagnosed workspace snapshot."""

import hashlib
import os
from pathlib import Path
from types import MappingProxyType

from catkin_pkg.package import InvalidPackage, parse_package
import yaml

from .dependency_graph import build_graph
from .models import Dependency, PackageInfo, WorkspaceSnapshot


class WorkspaceError(RuntimeError):
    """The workspace cannot be represented or operated on consistently."""


def validate_root(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir() or not (root / 'src').is_dir():
        raise WorkspaceError(f'工作空间必须包含 src 目录: {root}')
    return root


def configuration_files(root, environment):
    home = Path(environment.get('COLCON_HOME') or Path.home() / '.colcon').expanduser()
    if not home.is_absolute():
        home = Path(root) / home
    defaults = Path(environment.get('COLCON_DEFAULTS_FILE') or home / 'defaults.yaml').expanduser()
    if not defaults.is_absolute():
        defaults = Path(root) / defaults
    files = {defaults, Path(root) / 'colcon_defaults.yaml', Path(root) / 'colcon.meta'}
    metadata = home / 'metadata'
    if metadata.is_dir():
        files.update(metadata.rglob('*.meta'))
    return files


def manifest_files(root):
    files = set()
    for directory, dirs, names in os.walk(Path(root) / 'src'):
        path = Path(directory)
        if 'COLCON_IGNORE' in names:
            files.add(path / 'COLCON_IGNORE')
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__'}]
        for name in ('package.xml', 'colcon.pkg', 'AMENT_IGNORE', 'CATKIN_IGNORE'):
            if name in names:
                files.add(path / name)
        if 'package.xml' in names:
            dirs[:] = []
    return files


def fingerprint(root, environment, packages=None):
    digest = hashlib.sha256()
    files = manifest_files(root) | configuration_files(root, environment)
    if packages:
        for package in packages.values():
            files.update((package.path / 'package.xml', package.path / 'colcon.pkg'))
    for path in sorted(files, key=str):
        digest.update(str(path).encode('utf-8'))
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            digest.update(b'<missing>')
        except OSError as exc:
            raise WorkspaceError(f'无法读取工作空间元数据 {path}: {exc}') from exc
    return digest.hexdigest()


def validate_colcon_configuration(root, environment, symlink_install=None, packages=None):
    """Reject hidden overrides that this version cannot faithfully preview/clean."""
    home = Path(environment.get('COLCON_HOME') or Path.home() / '.colcon').expanduser()
    if not home.is_absolute():
        home = root / home
    global_file = Path(environment.get('COLCON_DEFAULTS_FILE') or home / 'defaults.yaml').expanduser()
    if not global_file.is_absolute():
        global_file = root / global_file
    effective = {}
    paths = [global_file, root / 'colcon_defaults.yaml']
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        except FileNotFoundError:
            continue
        except (OSError, yaml.YAMLError, UnicodeError) as exc:
            raise WorkspaceError(f'无法解析 colcon 配置 {path}: {exc}') from exc
        if not isinstance(data, dict):
            raise WorkspaceError(f'colcon 配置必须为字典: {path}')
        for verb in ('', 'list', 'build'):
            options = data.get(verb, {})
            if not isinstance(options, dict):
                raise WorkspaceError(f'{path} 的 {verb!r} 配置不是字典')
            for key, value in options.items():
                if not isinstance(key, str):
                    raise WorkspaceError(f'colcon 配置选项名称无效: {path}')
                if value and (key.startswith('packages-') or key in (
                        'paths', 'base-paths', 'metas', 'ignore-user-meta', 'mixin', 'mixin-files')):
                    raise WorkspaceError(
                        f'{path} 含 {verb}.{key}，无法保证选择预览一致，请移除该默认选择限制')
                if key in ('build-base', 'install-base') and value:
                    candidate = Path(str(value)).expanduser()
                    if not candidate.is_absolute():
                        candidate = root / candidate
                    expected = root / ('build' if key == 'build-base' else 'install')
                    if candidate.resolve() != expected.resolve():
                        raise WorkspaceError(f'本版本不支持自定义产物目录: {path}: {key}={value}')
            if verb == 'build':
                effective.update(options)
    if symlink_install is False and effective.get('symlink-install'):
        raise WorkspaceError('colcon 默认配置启用了 symlink-install，请同步勾选或修改默认配置')
    # Package-specific path/selection overrides also invalidate a global preview.
    meta_files = configuration_files(root, environment) - set(paths)
    meta_files.update(p for p in manifest_files(root) if p.name == 'colcon.pkg')
    if packages:
        meta_files.update(package.path / 'colcon.pkg' for package in packages.values())
    for path in meta_files:
        try:
            data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        except FileNotFoundError:
            continue
        except (OSError, yaml.YAMLError, UnicodeError) as exc:
            raise WorkspaceError(f'无法解析包元数据 {path}: {exc}') from exc
        if not isinstance(data, dict):
            raise WorkspaceError(f'包元数据必须为字典: {path}')
        entries = [data] if path.name == 'colcon.pkg' else []
        if path.name != 'colcon.pkg':
            for section in ('names', 'paths'):
                entries_map = data.get(section, {})
                if not isinstance(entries_map, dict):
                    raise WorkspaceError(f'包元数据 {path}: {section} 必须为字典')
                entries.extend(entries_map.values())
        for entry in entries:
            if not isinstance(entry, dict):
                raise WorkspaceError(f'包元数据条目必须为字典: {path}')
            for key, value in entry.items():
                if value and (str(key).startswith('packages-') or key in (
                        'build-base', 'install-base', 'paths', 'base-paths', 'metas')):
                    raise WorkspaceError(f'本版本不支持包元数据覆盖 {key}: {path}')
    return effective


def discovery_arguments(root, targets=None):
    arguments = ['list', '--base-paths', str(Path(root) / 'src')]
    if targets is not None:
        if not targets:
            raise WorkspaceError('不能使用空目标调用 colcon 选择查询')
        arguments.extend(['--packages-up-to', *sorted(targets), '--names-only'])
    return arguments


def _manifest(name, path, build_type, environment):
    package = parse_package(str(path / 'package.xml'))
    package.evaluate_conditions(dict(environment))
    if package.name != name:
        raise WorkspaceError(f'colcon 包名 {name} 与清单包名 {package.name} 不一致: {path}')
    dependencies = []
    for attr, kind in (
            ('build_depends', 'build'), ('build_export_depends', 'build_export'),
            ('buildtool_depends', 'buildtool'),
            ('buildtool_export_depends', 'buildtool_export'),
            ('exec_depends', 'run'), ('test_depends', 'test'), ('doc_depends', 'doc')):
        for dep in getattr(package, attr):
            if dep.evaluated_condition is not False:
                dependencies.append(Dependency(dep.name.strip(), kind, dep.condition or ''))
    return PackageInfo(
        name, path, build_type, tuple(dependencies),
        frozenset(g.name for g in package.member_of_groups if g.evaluated_condition is not False),
        frozenset(g.name for g in package.group_depends if g.evaluated_condition is not False))


def parse_discovery(root, stdout, stderr, environment, initial_fingerprint):
    packages = {}
    errors = []
    diagnostics = [line for line in stderr.splitlines() if line.strip()]
    if any('ERROR' in line or 'Traceback' in line for line in diagnostics):
        errors.append('colcon 发现过程报告错误，请检查扫描日志')
    names = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split('\t')
        if len(fields) != 3:
            errors.append(f'无法识别 colcon list 输出: {line}')
            continue
        name, path_text, kind = [f.strip() for f in fields]
        path = Path(path_text)
        path = (path if path.is_absolute() else root / path).resolve()
        if name in names:
            errors.append(f'重复包名 {name}: {names[name]} 与 {path}')
            continue
        names[name] = path
        try:
            build_type = kind.strip('()').rsplit('.', 1)[-1]
            if not (path / 'package.xml').is_file():
                raise WorkspaceError(f'暂不支持无 package.xml 的包: {name}: {path}')
            packages[name] = _manifest(name, path, build_type, environment)
        except (InvalidPackage, OSError, ValueError, WorkspaceError) as exc:
            errors.append(f'{name} 清单解析失败: {exc}')
    current_fingerprint = fingerprint(root, environment)
    if current_fingerprint != initial_fingerprint:
        raise WorkspaceError('扫描期间包清单或 colcon 配置发生变化，请重新刷新')
    validate_colcon_configuration(root, environment, packages=packages)
    forward, reverse = build_graph(packages)
    return WorkspaceSnapshot(
        root, packages, MappingProxyType(dict(environment)), fingerprint(root, environment, packages),
        tuple(diagnostics), tuple(errors), forward, reverse)


def parse_selection(stdout, targets, available):
    selected = {line.strip() for line in stdout.splitlines() if line.strip()}
    if not set(targets).issubset(selected):
        raise WorkspaceError('colcon 未选中全部目标，请刷新并检查忽略规则与选择配置')
    unknown = selected.difference(available)
    if unknown:
        raise WorkspaceError('构建集合出现新包，请刷新工作空间: ' + ', '.join(sorted(unknown)))
    return selected
