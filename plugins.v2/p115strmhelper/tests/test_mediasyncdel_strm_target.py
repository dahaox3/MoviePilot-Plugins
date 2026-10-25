"""STRM 文件级删除的真实文件系统及删除边界回归测试"""

import ast
import importlib.util
import sys
import typing
from dataclasses import replace
from enum import Enum
from pathlib import Path
from shutil import rmtree
from tempfile import TemporaryDirectory
from time import localtime, monotonic, sleep, strftime, time
from types import ModuleType, SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch


def _load_index():
    path = Path(__file__).resolve().parents[1] / "helper/mediasyncdel/strm_index.py"
    name = "_p115_strm_index_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    log = ModuleType("app.log")
    log.logger = Mock()
    sys.modules[name] = module
    with patch.dict(sys.modules, {"app.log": log}):
        spec.loader.exec_module(module)
    return module


INDEX = _load_index()


def _load_helper():
    path = Path(__file__).resolve().parents[1] / "helper/mediasyncdel/__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    helper = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    helper.decorator_list = []

    class MediaType(Enum):
        MOVIE = "电影"
        TV = "电视剧"

    namespace = dict(vars(typing))
    namespace.update(
        Path=Path,
        logger=Mock(),
        Event=Mock(),
        TransferHistory=Mock(),
        WebhookEventInfo=Mock(),
        MediaType=MediaType,
        MediaImageType=Mock(),
        NotificationType=Mock(),
        post_message=Mock(),
        strftime=strftime,
        localtime=localtime,
        time=time,
        i18n=Mock(),
        PathRemoveUtils=Mock(),
        StrmDeletePlan=INDEX.StrmDeletePlan,
        StrmPathIndex=INDEX.StrmPathIndex,
        StrmTarget=INDEX.StrmTarget,
        configer=SimpleNamespace(storage_module="u115", sync_del_remove_versions=False),
        settings=SimpleNamespace(RMT_MEDIAEXT=[".mkv", ".mp4", ".iso"]),
        WebhookUtils=SimpleNamespace(
            parse_item_paths_from_description=lambda value: []
        ),
    )
    utils = Path(__file__).resolve().parents[1] / "utils/path.py"
    utils_tree = ast.parse(utils.read_text(encoding="utf-8"))
    namespace["PurePosixPath"] = __import__("pathlib").PurePosixPath
    path_class = next(n for n in utils_tree.body if isinstance(n, ast.ClassDef))
    exec(
        compile(ast.Module(body=[path_class], type_ignores=[]), str(utils), "exec"),
        namespace,
    )
    exec(
        compile(ast.Module(body=[helper], type_ignores=[]), str(path), "exec"),
        namespace,
    )
    instance = object.__new__(namespace["MediaSyncDelHelper"])
    instance.storagechain = Mock()
    instance.transferhis = Mock()
    instance.transferhis.get_by_dest.return_value = None
    instance._save_sync_del_history = Mock()
    return instance, namespace


@skipUnless(sys.platform.startswith("linux"), "STRM 目标删除仅支持 Linux 容器路径")
class TestStrmPathIndex(TestCase):
    """验证软链接追踪和删除前索引的失效条件"""

    def setUp(self) -> None:
        """创建独立媒体库和原始 STRM 目录"""
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.library = self.root / "media"
        self.original = self.root / "linshi"
        self.library.mkdir()
        self.original.mkdir()
        self.paths = f"/emby#{self.library.as_posix()}#/整理完成\n/raw#{self.original.as_posix()}#/临时下载"
        self.index = INDEX.StrmPathIndex()
        self.addCleanup(self.index.stop)

    def _link(self, name="movie.strm", relative=False):
        target = self.original / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("private-url", encoding="utf-8")
        source = self.library / name
        source.parent.mkdir(parents=True, exist_ok=True)
        destination = (
            Path(__import__("os").path.relpath(target, source.parent))
            if relative
            else target
        )
        source.symlink_to(destination)
        return source, target

    def _start(self):
        self.index.start(self.paths)
        self.index._scanner.join(timeout=10)
        self.assertTrue(self.index._ready)

    def test_absolute_and_relative_links_use_original_mapping(self):
        """绝对与相对链接均映射到临时下载而非整理完成"""
        for relative in (False, True):
            name = f"film{relative}.strm"
            source, target = self._link(name, relative)
            if not self.index._ready:
                self._start()
            plan = self.index.capture(f"/emby/{name}", self.paths)
            self.assertFalse(plan.error)
            self.assertEqual(plan.targets[0].target, target)
            self.assertEqual(plan.targets[0].remote, f"/临时下载/{name}")
            self.assertTrue(source.is_symlink())

    def test_deleted_link_is_available_from_index(self):
        """Emby 先删链接后发通知仍可追溯原始路径"""
        source, target = self._link()
        self._start()
        source.unlink()
        plan = self.index.capture("/emby/movie.strm", self.paths)
        self.assertFalse(plan.error)
        self.assertEqual(plan.targets[0].target, target)

    def test_removed_directory_is_expanded_to_each_episode(self):
        """目录已被删除仍逐个返回分集而不返回网盘目录"""
        for episode in range(1, 4):
            self._link(f"Show/Season 1/E{episode}.strm")
        self._start()
        rmtree(self.library / "Show")
        plan = self.index.capture("/emby/Show", self.paths)
        self.assertFalse(plan.error)
        self.assertEqual(len(plan.targets), 3)
        self.assertTrue(
            all(t.remote.startswith("/临时下载/Show/") for t in plan.targets)
        )

    def test_mixed_directory_retains_regular_file_mapping(self):
        """混合目录中的普通 STRM 不会被当成软链接映射"""
        source, _ = self._link("Show/E1.strm")
        regular = source.parent / "E2.strm"
        regular.write_text("direct", encoding="utf-8")
        self._start()
        rmtree(source.parent)
        plan = self.index.capture("/emby/Show", self.paths)
        self.assertFalse(plan.error)
        self.assertEqual(
            {t.remote for t in plan.targets},
            {"/临时下载/Show/E1.strm", "/整理完成/Show/E2.strm"},
        )

    def test_directory_modified_event_does_not_discard_deleted_snapshots(self):
        """删除文件引起的父目录修改事件不会清空快照"""
        source, _ = self._link("Show/E1.strm")
        self._start()
        source.unlink()
        sleep(0.2)
        self.assertFalse(self.index.capture("/emby/Show/E1.strm", self.paths).error)

    def test_unknown_missing_path_never_falls_back(self):
        """索引启动前已删除的路径无法猜测"""
        self._start()
        self.assertTrue(self.index.capture("/emby/unknown.strm", self.paths).error)

    def test_same_emby_prefix_uses_only_confirmed_local_snapshot(self):
        """两个容器挂载共用 Emby 前缀时以唯一真实索引确定源映射"""
        source, target = self._link("Show/E1.strm")
        renamed = self.library / "renamed.strm"
        source.rename(renamed)
        self.paths = self.paths.replace("/raw#", "/emby#")
        self._start()
        renamed.unlink()
        plan = self.index.capture("/emby/renamed.strm", self.paths)
        self.assertFalse(plan.error)
        self.assertEqual(plan.targets[0].target, target)

    def test_same_emby_prefix_with_two_real_paths_is_ambiguous(self):
        """两个本地位置都存在相同相对路径时拒绝猜测"""
        self._link("Show/E1.strm")
        self.paths = self.paths.replace("/raw#", "/emby#")
        self._start()
        self.assertTrue(self.index.capture("/emby/Show/E1.strm", self.paths).error)

    def test_outside_target_and_broken_link_are_rejected(self):
        """范围外目标和断链均拒绝删除"""
        external = self.root / "outside.strm"
        external.write_text("private", encoding="utf-8")
        (self.library / "outside.strm").symlink_to(external)
        (self.library / "broken.strm").symlink_to(self.original / "missing.strm")
        self._start()
        for name in ("outside", "broken"):
            self.assertTrue(self.index.capture(f"/emby/{name}.strm", self.paths).error)

    def test_loop_and_directory_link_are_rejected(self):
        """循环链接和目录链接不会触发递归删除"""
        (self.library / "loop.strm").symlink_to("loop.strm")
        (self.library / "linked").symlink_to(self.original, target_is_directory=True)
        self._start()
        for name in ("loop.strm", "linked"):
            self.assertTrue(self.index.capture(f"/emby/{name}", self.paths).error)

    def test_longest_local_prefix_and_ambiguity(self):
        """最长前缀独立于配置顺序，同长歧义则拒绝"""
        self._link("Show/E1.strm")
        child = f"/child#{(self.original / 'Show').as_posix()}#/另一目录"
        self.paths += "\n" + child
        self._start()
        self.assertEqual(
            self.index.capture("/emby/Show/E1.strm", self.paths).targets[0].remote,
            "/另一目录/E1.strm",
        )
        self.index.stop()
        self.paths += "\n" + child.replace("/另一目录", "/冲突目录")
        self._start()
        self.assertTrue(self.index.capture("/emby/Show/E1.strm", self.paths).error)

    def test_mapping_roots_and_traversal_are_rejected(self):
        """根目录、越界路径及相似前缀均不能删除"""
        self._start()
        for path in ("/emby", "/emby/../raw/file.strm", "/emby-other/file.strm"):
            self.assertTrue(self.index.capture(path, self.paths).error)

    def test_recreated_regular_file_is_not_deleted(self):
        """链接入队后被替换为普通文件时拒绝原计划"""
        source, _ = self._link()
        self._start()
        plan = self.index.capture("/emby/movie.strm", self.paths)
        source.unlink()
        source.write_text("new-media", encoding="utf-8")
        with self.assertRaises(ValueError):
            INDEX.StrmPathIndex.validate(plan.targets[0])

    def test_changed_target_is_not_deleted(self):
        """原始 STRM 在排队期间变化时拒绝原计划"""
        _, target = self._link()
        self._start()
        plan = self.index.capture("/emby/movie.strm", self.paths)
        target.write_text("new-target-content", encoding="utf-8")
        with self.assertRaises(ValueError):
            INDEX.StrmPathIndex.validate(plan.targets[0])

    def test_expired_deleted_snapshot_is_rejected(self):
        """过期删除快照不会在重复通知中被重用"""
        source, _ = self._link()
        self._start()
        target = self.index.capture("/emby/movie.strm", self.paths).targets[0]
        source.unlink()
        with self.assertRaises(ValueError):
            INDEX.StrmPathIndex.validate(replace(target, deleted_at=monotonic() - 601))

    def test_changed_mapping_or_stopped_index_is_rejected(self):
        """配置变化和停止监控均不回退旧映射"""
        self._link()
        self._start()
        self.assertTrue(
            self.index.capture(
                "/emby/movie.strm", self.paths + "\n/new#/new#/new"
            ).error
        )
        self.index.stop()
        self.assertTrue(self.index.capture("/emby/movie.strm", self.paths).error)

    def test_live_regular_and_hard_link_are_not_removed(self):
        """普通 STRM 和硬链接仍存在时保留原有不删网盘的保护"""
        regular = self.library / "normal.strm"
        regular.write_text("data", encoding="utf-8")
        (self.library / "hard.strm").hardlink_to(regular)
        self._start()
        for name in ("normal", "hard"):
            self.assertTrue(self.index.capture(f"/emby/{name}.strm", self.paths).error)


@skipUnless(sys.platform.startswith("linux"), "STRM 目标删除仅支持 Linux 容器路径")
class TestExactStrmDelete(TestCase):
    """以存储假对象验证完整 Webhook 路径不会扩大删除范围"""

    def setUp(self) -> None:
        """准备精确文件计划和假的存储对象"""
        self.helper, self.ns = _load_helper()
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = Path(temp.name) / "original.strm"
        source.write_text("private", encoding="utf-8")
        self.target = INDEX.StrmTarget(
            source=source.parent / "removed.strm",
            target=source,
            remote="/临时下载/Show/E1.strm",
            mapping=("/raw", str(source.parent), "/临时下载"),
            linked=True,
            identity=(0, 0, 0, 0),
            target_identity=INDEX.StrmPathIndex._identity(source),
        )
        self.remote = SimpleNamespace(type="file", path="/临时下载/Show/E1.mkv")
        self.helper.storagechain.get_file_item.side_effect = lambda **kw: (
            self.remote
            if str(kw["path"]) == self.remote.path
            else SimpleNamespace(type="dir", path=str(kw["path"]))
        )
        self.helper.storagechain.list_files.return_value = [self.remote]
        self.helper.storagechain.delete_file.return_value = True
        self.event = SimpleNamespace(
            event="deep.delete",
            item_type="Series",
            item_name="Show",
            item_path="/emby/Show",
            tmdb_id="123",
            season_id=None,
            episode_id=None,
            json_object={},
        )
        self.plan = INDEX.StrmDeletePlan(targets=(self.target,))

    def _run(self, plan=None, force=True):
        return self.helper.sync_del_by_webhook(
            event_data=self.event,
            enabled=True,
            notify=False,
            del_source=False,
            p115_library_path="/emby#/media#/整理完成",
            p115_force_delete_files=force,
            strm_delete_plans={self.event.item_path: plan or self.plan},
        )

    def test_deleted_series_uses_only_target_file(self):
        """整剧事件只删已索引的精确文件，不扩大查询到整剧历史"""
        self.assertIsNotNone(self._run())
        self.helper.storagechain.delete_file.assert_called_once_with(self.remote)
        self.helper.storagechain.delete_media_file.assert_not_called()
        self.helper.transferhis.get_by.assert_not_called()
        self.helper.transferhis.get_by_dest.assert_any_call(self.remote.path)

    def test_movie_season_episode_and_versions_use_exact_files(self):
        """电影、季、单集及多版本开关均不放宽目标路径"""
        for kind, season, episode in (
            ("Movie", None, None),
            ("Season", "1", None),
            ("Episode", "1", "1"),
        ):
            for versions in (False, True):
                self.event.item_type, self.event.season_id, self.event.episode_id = (
                    kind,
                    season,
                    episode,
                )
                self.ns["configer"].sync_del_remove_versions = versions
                self.helper.transferhis.reset_mock()
                self.assertIsNotNone(self._run())
                self.helper.transferhis.get_by.assert_not_called()
                self.helper.transferhis.get_by_dest.assert_any_call(self.remote.path)

    def test_multiple_remote_extensions_are_ambiguous(self):
        """同名 mkv 与 mp4 不猜后缀也不强制删除目录"""
        self.helper.storagechain.list_files.return_value.append(
            SimpleNamespace(type="file", path="/临时下载/Show/E1.mp4")
        )
        self.assertIsNone(self._run())
        self.helper.storagechain.delete_file.assert_not_called()

    def test_embedded_extension_selects_only_that_version(self):
        """保留原后缀的 STRM 可区分多个媒体版本"""
        self.plan = INDEX.StrmDeletePlan(
            targets=(replace(self.target, remote="/临时下载/Show/E1.mkv.strm"),)
        )
        self.helper.storagechain.list_files.return_value.append(
            SimpleNamespace(type="file", path="/临时下载/Show/E1.mp4")
        )
        self.assertIsNotNone(self._run())

    def test_invalid_plan_never_falls_back_to_force_delete(self):
        """路径不确定时即使强制删除已开启也跳过"""
        self.assertIsNone(self._run(INDEX.StrmDeletePlan(error="ambiguous")))
        self.helper.storagechain.get_file_item.assert_not_called()
        self.helper.transferhis.get_by.assert_not_called()

    def test_duplicate_targets_are_deleted_once(self):
        """同一文件的多条链接不会导致重复删除"""
        self.plan = INDEX.StrmDeletePlan(targets=(self.target, self.target))
        self._run()
        self.helper.storagechain.delete_file.assert_called_once()

    def test_no_history_requires_force_setting(self):
        """无整理记录仍遵守强制删除开关"""
        self.assertIsNone(self._run(force=False))
        self.helper.storagechain.delete_file.assert_not_called()

    def test_storage_failure_keeps_history(self):
        """网盘删除失败时不清理整理记录也不报告成功"""
        history = SimpleNamespace(
            id=1,
            dest=self.remote.path,
            dest_storage="u115",
            title="Show",
            image=None,
            year=2026,
        )
        self.helper.transferhis.get_by_dest.side_effect = lambda p: (
            history if p == self.remote.path else None
        )
        self.helper.storagechain.delete_file.return_value = False
        self.assertIsNone(self._run())
        self.helper.transferhis.delete.assert_not_called()
        self.helper._save_sync_del_history.assert_not_called()

    def test_history_is_removed_only_after_success(self):
        """精确匹配的整理记录在删除成功后清理"""
        history = SimpleNamespace(
            id=1,
            dest=self.remote.path,
            dest_storage="u115",
            title="Show",
            image=None,
            year=2026,
        )
        self.helper.transferhis.get_by_dest.side_effect = lambda p: (
            history if p == self.remote.path else None
        )
        self.assertIsNotNone(self._run(force=False))
        self.helper.transferhis.delete.assert_called_once_with(1)

    def test_directory_substitution_is_not_deleted(self):
        """列表返回文件但执行时已变目录则拒绝删除"""
        self.helper.storagechain.get_file_item.side_effect = (
            lambda **kw: SimpleNamespace(type="dir", path=str(kw["path"]))
        )
        self.assertIsNone(self._run())
        self.helper.storagechain.delete_file.assert_not_called()
        self.helper.storagechain.delete_media_file.assert_not_called()

    def test_foreign_history_is_rejected(self):
        """其他储存的同路径整理记录不能被删除"""
        self.helper.transferhis.get_by_dest.side_effect = lambda p: (
            SimpleNamespace(dest=self.remote.path, dest_storage="local")
            if p == self.remote.path
            else None
        )
        self.assertIsNone(self._run())
        self.helper.storagechain.delete_file.assert_not_called()

    def test_local_softlink_history_is_checked_and_cleaned_after_success(self):
        """对应本地整理记录在成功删除网盘文件后清理"""
        local = SimpleNamespace(
            id=9,
            dest_storage="local",
            src_storage="local",
            mode="softlink",
            src=str(self.target.target),
        )
        self.helper.transferhis.get_by_dest.side_effect = lambda p: (
            local if p == str(self.target.source) else None
        )
        self.assertIsNotNone(self._run())
        self.helper.transferhis.delete.assert_called_once_with(9)

    def test_inconsistent_local_history_is_rejected(self):
        """本地整理记录与当前索引指向不符时不执行网盘删除"""
        self.helper.transferhis.get_by_dest.return_value = SimpleNamespace(
            id=9,
            dest_storage="local",
            src_storage="local",
            mode="softlink",
            src="/wrong.strm",
        )
        self.assertIsNone(self._run())
        self.helper.storagechain.get_file_item.assert_not_called()

    def test_none_delete_result_is_not_success(self):
        """网盘模块未返回成功时保留本地整理记录"""
        local = SimpleNamespace(
            id=9,
            dest_storage="local",
            src_storage="local",
            mode="softlink",
            src=str(self.target.target),
        )
        self.helper.transferhis.get_by_dest.side_effect = lambda p: (
            local if p == str(self.target.source) else None
        )
        self.helper.storagechain.delete_file.return_value = None
        self.assertIsNone(self._run())
        self.helper.transferhis.delete.assert_not_called()
