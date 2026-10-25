"""深度删除前的本地 STRM 路径快照，不读取或删除任何网盘数据"""

from dataclasses import dataclass, field, replace
from os import lstat, scandir
from pathlib import Path, PurePosixPath
from sys import platform
from threading import RLock, Thread
from time import monotonic
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger


@dataclass(frozen=True)
class StrmTarget:
    """
    单个 STRM 的不可变路径追踪快照

    Attributes:
        source: MoviePilot 中的媒体库路径
        target: 解析软链接后的原始 STRM 路径
        remote: 对应网盘 STRM 路径，尚未推断媒体后缀
        mapping: 最长前缀且无歧义的目标路径映射
        linked: 是否解析过软链接
        identity: 源文件状态，用于发现排队期间的文件替换
        target_identity: 软链接目标状态
        deleted_at: 监控到源文件删除的单调时间
    """

    source: Path
    target: Path
    remote: str
    mapping: Tuple[str, str, str]
    linked: bool
    identity: Tuple[int, int, int, int]
    target_identity: Tuple[int, int, int, int]
    deleted_at: float = 0
    recorded_at: float = field(default_factory=monotonic)


@dataclass(frozen=True)
class StrmDeletePlan:
    """
    入队时冻结的文件级删除计划

    Attributes:
        targets: 已确认的精确文件路径
        error: 无法确认时的拒绝原因
    """

    targets: Tuple[StrmTarget, ...] = ()
    error: str = ""


class StrmPathIndex:
    """监控配置范围内的 STRM，保留十分钟删除快照以兼容先删文件后发通知"""

    RETENTION = 600
    MAX_ENTRIES = 100000

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: Dict[Path, StrmTarget] = {}
        self._invalid: Dict[Path, str] = {}
        self._mappings: Tuple[Tuple[str, str, str], ...] = ()
        self._observer: Any = None
        self._scanner: Optional[Thread] = None
        self._ready = False
        self._stopped = True

    @staticmethod
    def _parts(value: str) -> PurePosixPath:
        path = PurePosixPath(value.replace("\\", "/"))
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("路径必须为绝对路径且不能包含 ..")
        return path

    @classmethod
    def _match(
        cls, value: str, mappings: Tuple[Tuple[str, str, str], ...], column: int
    ) -> Tuple[str, str, str]:
        path = cls._parts(value)
        matches = [m for m in mappings if path.is_relative_to(cls._parts(m[column]))]
        if not matches:
            raise ValueError(f"路径不在已配置范围内: {value}")
        longest = max(len(cls._parts(m[column]).parts) for m in matches)
        matches = set(m for m in matches if len(cls._parts(m[column]).parts) == longest)
        if len(matches) != 1:
            raise ValueError(f"路径存在多条等长映射: {value}")
        return matches.pop()

    @staticmethod
    def _identity(path: Path) -> Tuple[int, int, int, int]:
        info = lstat(path)
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size

    @classmethod
    def _no_link_parents(cls, path: Path) -> None:
        if any(parent.is_symlink() for parent in path.parents):
            raise ValueError(f"目录软链接不参与自动删除: {path}")

    def _snapshot(self, path: Path) -> StrmTarget:
        self._match(path.as_posix(), self._mappings, 1)
        self._no_link_parents(path)
        linked = path.is_symlink()
        target = path.resolve(strict=True)
        mapping = self._match(target.as_posix(), self._mappings, 1)
        if not target.is_file() or target.suffix.lower() != ".strm":
            raise ValueError(f"目标不是 STRM 普通文件: {target}")
        remote = (
            self._parts(mapping[2]) / target.relative_to(Path(mapping[1])).as_posix()
        )
        if remote == self._parts(mapping[2]):
            raise ValueError("禁止删除网盘映射根目录")
        return StrmTarget(
            source=path,
            target=target,
            remote=str(remote),
            mapping=mapping,
            linked=linked,
            identity=self._identity(path),
            target_identity=self._identity(target),
        )

    def _prune(self) -> None:
        now = monotonic()
        self._records = {
            p: r
            for p, r in self._records.items()
            if not r.deleted_at or now - r.deleted_at <= self.RETENTION
        }

    def _scan(self, root: Path) -> None:
        self._no_link_parents(root / "placeholder")
        with scandir(root) as entries:
            for entry in entries:
                if self._stopped:
                    return
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    self._scan(path)
                elif path.suffix.lower() == ".strm":
                    self._record(path)
                elif entry.is_symlink() and path.is_dir():
                    self._invalid[path] = "目录软链接不参与自动删除"

    def _record(self, path: Path) -> None:
        self._records.pop(path, None)
        self._invalid.pop(path, None)
        try:
            if len(self._records) + len(self._invalid) >= self.MAX_ENTRIES:
                raise OverflowError("STRM 路径索引超过容量限制")
            self._records[path] = self._snapshot(path)
        except (OSError, RuntimeError, ValueError) as error:
            self._invalid[path] = str(error)

    def _event(self, path: str, deleted: bool, directory: bool) -> None:
        with self._lock:
            if self._stopped:
                return
            local = Path(path)
            self._prune()
            affected = [
                p
                for p in self._records
                if p == local or (directory and local in p.parents)
            ]
            if deleted:
                if local in {Path(m[1]) for m in self._mappings}:
                    self._ready = False
                for item in affected:
                    self._records[item] = replace(
                        self._records[item], deleted_at=monotonic()
                    )
                return
            for item in affected:
                self._records.pop(item, None)
            for item in list(self._invalid):
                if item == local or (directory and local in item.parents):
                    self._invalid.pop(item, None)
            try:
                if directory and local.is_dir() and not local.is_symlink():
                    self._scan(local)
                elif local.suffix.lower() == ".strm":
                    self._record(local)
            except (OSError, ValueError, OverflowError) as error:
                self._ready = False
                logger.warning(
                    f"【同步删除】STRM 路径索引失效，暂停软链接删除: {error}"
                )

    def start(self, paths: str) -> None:
        """启动目录监控并异步扫描，扫描完成前拒绝索引模式删除"""
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        self.stop()
        if not platform.startswith("linux"):
            raise ValueError("STRM 目标删除目前仅支持 Linux 容器路径")
        mappings = []
        for line in (paths or "").splitlines():
            if not line.strip():
                continue
            parts = tuple(str(self._parts(p.strip())) for p in line.split("#"))
            if len(parts) != 3 or any(p == "/" for p in parts):
                raise ValueError("软链接删除要求三段非根目录映射")
            mappings.append(parts)
        if not mappings:
            raise ValueError("未配置同步删除路径映射")
        self._mappings = tuple(mappings)
        self._stopped = False
        index = self

        class Handler(FileSystemEventHandler):
            """在文件变化时刷新路径快照"""

            def on_any_event(self, event: Any) -> None:
                """忽略读取事件，移动同时更新旧路径和新路径"""
                if event.is_directory and event.event_type == "modified":
                    return
                if event.event_type in {"created", "modified", "deleted", "moved"}:
                    index._event(
                        event.src_path,
                        event.event_type in {"deleted", "moved"},
                        event.is_directory,
                    )
                    if event.event_type == "moved":
                        index._event(event.dest_path, False, event.is_directory)

        roots = sorted({Path(m[1]) for m in self._mappings}, key=lambda p: len(p.parts))
        roots = [p for p in roots if not any(q in p.parents for q in roots)]
        try:
            self._observer = Observer()
            for root in roots:
                self._no_link_parents(root / "placeholder")
                if not root.is_dir():
                    raise ValueError(f"MoviePilot 映射目录不可访问: {root}")
                self._observer.schedule(Handler(), str(root), recursive=True)
            self._observer.start()

            def scan() -> None:
                with self._lock:
                    try:
                        for root in roots:
                            self._scan(root)
                        self._ready = not self._stopped
                        logger.info(
                            f"【同步删除】STRM 路径索引就绪: {len(self._records)} 个文件"
                        )
                    except (OSError, ValueError, OverflowError) as error:
                        logger.warning(
                            f"【同步删除】STRM 路径索引未就绪，跳过软链接删除: {error}"
                        )

            self._scanner = Thread(target=scan, name="P115-StrmPathIndex", daemon=True)
            self._scanner.start()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """停止监控并丢弃本次生命周期的快照，不重用重启前的过期路径"""
        self._stopped = True
        self._ready = False
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=10)
        if self._scanner:
            self._scanner.join(timeout=10)
        with self._lock:
            self._observer = None
            self._scanner = None
            self._records.clear()
            self._invalid.clear()
            self._mappings = ()

    def capture(self, media_path: str, paths: str) -> StrmDeletePlan:
        """将 Emby 路径转换成受配置范围约束的文件计划，失效或未知路径不回退目录删除"""
        with self._lock:
            try:
                if (
                    not self._ready
                    or not self._observer
                    or not self._observer.is_alive()
                ):
                    raise ValueError("STRM 路径索引未就绪，请等待扫描完成后重试")
                expected = tuple(
                    tuple(str(self._parts(p.strip())) for p in line.split("#"))
                    for line in paths.splitlines()
                    if line.strip()
                )
                if expected != self._mappings:
                    raise ValueError("路径映射已改变，请等待索引重建")
                if any(not Path(m[1]).is_dir() for m in self._mappings):
                    raise ValueError("MoviePilot 映射目录不可访问，暂停删除")
                self._prune()
                media = self._parts(media_path)
                candidates = [
                    m for m in self._mappings if media.is_relative_to(self._parts(m[0]))
                ]
                if not candidates:
                    raise ValueError("Emby 路径不在已配置媒体库范围内")
                longest = max(len(self._parts(m[0]).parts) for m in candidates)
                candidates = set(
                    m for m in candidates if len(self._parts(m[0]).parts) == longest
                )
                if len(candidates) > 1:
                    confirmed = set()
                    for candidate in candidates:
                        candidate_path = Path(candidate[1]).joinpath(
                            *media.relative_to(self._parts(candidate[0])).parts
                        )
                        if (
                            candidate_path.exists()
                            or candidate_path.is_symlink()
                            or candidate_path in self._records
                            or candidate_path in self._invalid
                            or any(candidate_path in p.parents for p in self._records)
                        ):
                            confirmed.add(candidate)
                    candidates = confirmed
                if len(candidates) != 1:
                    raise ValueError("Emby 路径对应多个或无法确认的本地映射")
                mapping = candidates.pop()
                relative = self._parts(media_path).relative_to(self._parts(mapping[0]))
                if not relative.parts:
                    raise ValueError("禁止删除媒体库映射根目录")
                local = Path(mapping[1]).joinpath(*relative.parts)
                self._no_link_parents(local)
                if local.is_symlink() and local.suffix.lower() != ".strm":
                    raise ValueError("不支持目录软链接删除")
                if local.suffix.lower() == ".strm":
                    if local.exists() or local.is_symlink():
                        self._record(local)
                    targets = [self._records[local]] if local in self._records else []
                else:
                    if local.exists():
                        self._scan(local)
                    invalid = [
                        p for p in self._invalid if p == local or local in p.parents
                    ]
                    if invalid:
                        raise ValueError(
                            f"目录包含无法确认的 STRM 或软链接: {invalid[0]}"
                        )
                    targets = [
                        r for p, r in self._records.items() if local in p.parents
                    ]
                if not targets:
                    raise ValueError(
                        "没有可确认的 STRM 快照，可能文件在索引启动前已删除"
                    )
                for target in targets:
                    self.validate(target)
                return StrmDeletePlan(
                    targets=tuple(sorted(targets, key=lambda t: str(t.source)))
                )
            except (OSError, RuntimeError, ValueError, OverflowError) as error:
                return StrmDeletePlan(error=str(error))

    @classmethod
    def validate(cls, target: StrmTarget) -> None:
        """删除执行前再次校验快照，重建文件、目标变化或超时均拒绝删除"""
        if target.deleted_at and monotonic() - target.deleted_at > cls.RETENTION:
            raise ValueError("已删除 STRM 的路径快照已过期")
        cls._no_link_parents(target.source)
        if target.source.exists() or target.source.is_symlink():
            if cls._identity(target.source) != target.identity:
                raise ValueError("STRM 在入队后被替换，已跳过删除")
            if not target.source.is_symlink():
                raise ValueError("本地普通 STRM 仍存在，已跳过删除")
            if target.source.resolve(strict=True) != target.target:
                raise ValueError("STRM 软链接目标已变化")
        elif monotonic() - (target.deleted_at or target.recorded_at) > cls.RETENTION:
            raise ValueError("缺失 STRM 的路径快照已过期")
        if target.linked:
            cls._no_link_parents(target.target)
            if (
                target.target.is_symlink()
                or cls._identity(target.target) != target.target_identity
            ):
                raise ValueError("原始 STRM 目标已变化，已跳过删除")


strm_path_index = StrmPathIndex()
