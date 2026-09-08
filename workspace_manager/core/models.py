"""Data passed between workspace logic and the desktop application."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Mapping, Tuple


@dataclass(frozen=True)
class Dependency:
    name: str
    kind: str
    condition: str = ''


@dataclass(frozen=True)
class PackageInfo:
    name: str
    path: Path
    build_type: str
    dependencies: Tuple[Dependency, ...] = ()
    groups: FrozenSet[str] = frozenset()
    group_dependencies: FrozenSet[str] = frozenset()


@dataclass
class WorkspaceSnapshot:
    root: Path
    packages: Dict[str, PackageInfo]
    environment: Mapping[str, str]
    fingerprint: str
    diagnostics: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()
    dependencies: Dict[str, set] = field(default_factory=dict)
    reverse_dependencies: Dict[str, set] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildRequest:
    root: Path
    targets: Tuple[str, ...]
    packages: Tuple[str, ...]
    program: str
    arguments: Tuple[str, ...]
    environment: Mapping[str, str]


@dataclass(frozen=True)
class ProcessResult:
    task_id: str
    returncode: int
    normal_exit: bool
    cancelled: bool
    stdout: str
    stderr: str
    error: str = ''

    @property
    def succeeded(self):
        return (self.normal_exit and self.returncode == 0
                and not self.cancelled and not self.error)
