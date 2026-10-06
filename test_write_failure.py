#!/usr/bin/env python3
"""report.export_csv 在输出写入失败后报告失败并清理半成品的回归测试。

只依赖 Python 标准库；在独立临时目录中自行准备 people/notes 合成库与
输出父目录，可重复执行，不依赖真实磁盘写满、系统权限差异或仓库预置
数据，用例结束后清理全部临时文件。

覆盖两个可重复的写入故障时点：

  1. 目标文件已新建，但表头一个字节都还没写成（0 字节半成品）；
  2. 表头与第一行数据已落盘，第二行数据写入时失败（部分行半成品）。

两种故障统一为带固定消息的 OSError：export_csv 必须把它包装成包含
“无法写入输出文件”、目标路径与原始故障消息的 ValueError，删除本次
新建的半成品且不波及同目录其他文件；恢复正常写入条件后，用同一源库、
同一查询和同一输出路径必须能再次导出成功。

在项目根目录执行：
    python -m unittest discover
"""

import builtins
import csv
import io
import os
import sqlite3
import tempfile
import unittest

import report

_BUILTIN_OPEN = builtins.open

JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id=n.person_id ORDER BY p.id"
)
# 同时包含中文、逗号与双引号，用于验证 CSV 字段引用
NOTE_VALUE = '中文,含"引号"'
# 统一的固定写入故障消息
FAIL_MESSAGE = "注入的固定写入故障：写入失败"
# 重试成功时 CSV 经 csv.reader 解析后的完整逻辑行
EXPECTED_ROWS = [["姓名", "备注"], ["小明", ""], ["小红", NOTE_VALUE]]


class _FailingOutputFile:
    """输出文件对象的替身：真实创建并写入目标文件，前 allow_writes 次
    write 正常落盘，此后每次 write 抛带固定消息的 OSError。

    文件仍由真实内置 open 以独占新建模式 "x" 创建，因此“目标已创建”
    与随后的 os.remove 清理都发生在真实磁盘上。每次成功写入后立即
    flush，使故障触发瞬间磁盘上的半成品字节完全确定，可供测试实际
    核对（标准库 csv.writer 每个 writerow 恰好产生一次底层 write）。
    """

    def __init__(self, path, allow_writes, message, on_failure):
        self._real = _BUILTIN_OPEN(path, "x", encoding="utf-8", newline="")
        self._allow = allow_writes
        self._message = message
        self._on_failure = on_failure
        self.tripped = False
        self.successful_writes = []

    def write(self, data):
        if self._allow <= 0:
            if not self.tripped:
                self.tripped = True
                self._real.flush()
                # 通知测试在异常向上传播、export_csv 清理之前记录磁盘状态
                self._on_failure()
            raise OSError(self._message)
        written = self._real.write(data)
        self._real.flush()
        self.successful_writes.append(data)
        self._allow -= 1
        return written

    def flush(self):
        self._real.flush()

    def close(self):
        self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class _WriteFailureMixin:
    """两个故障时点共用的夹具准备、故障注入与核对流程。

    allow_writes: 故障前允许成功落盘的底层 write 次数
                  （0=表头尚未写成；2=表头与第一行已写成）。
    partial_rows: 故障瞬间磁盘半成品按 CSV 解析应恰好对应的逻辑行。
    """

    allow_writes = None
    partial_rows = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        # 准备完成时的结构与数据基线，tearDown 中逐一核对
        self._baseline = self._snapshot_db()

        # 输出放在独立的已存在子目录中，目标起初不存在
        self.out_dir = os.path.join(self.tmpdir, "输出 目录")
        os.mkdir(self.out_dir)
        self.out_path = os.path.join(self.out_dir, "result.csv")
        self.assertFalse(os.path.exists(self.out_path))

        # 目录内预置的无关文件：失败清理与随后重试都不得改动其字节
        self.other_path = os.path.join(self.out_dir, "其他文件.txt")
        self.other_bytes = '预置内容：中文，逗号,与"引号"\n'.encode("utf-8")
        with _BUILTIN_OPEN(self.other_path, "wb") as f:
            f.write(self.other_bytes)

        self._fake = None
        self.captured_args = None
        self.captured_kwargs = None
        self.existed_at_failure = None
        self.partial_bytes_at_failure = None
        self._injected = False
        self._had_open_attr = hasattr(report, "open")
        self._saved_open = getattr(report, "open", None)

    def tearDown(self):
        self._stop_failure()
        # 用例结束前重新只读读取源库，核对两表结构及全部数据与准备时一致
        self.assertEqual(self._snapshot_db(), self._baseline)
        self._tmp.cleanup()

    def _prepare_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE people ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE notes ("
                "person_id INTEGER PRIMARY KEY, note TEXT)"
            )
            conn.executemany(
                "INSERT INTO people (id, name) VALUES (?, ?)",
                [(1, "小明"), (2, "小红")],
            )
            # 两人的备注分别为 NULL 与含逗号、双引号的中文
            conn.executemany(
                "INSERT INTO notes (person_id, note) VALUES (?, ?)",
                [(1, None), (2, NOTE_VALUE)],
            )
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
        """以只读方式重新读取源库的表结构与两表全部数据。"""
        uri = "file:%s?mode=ro" % os.path.abspath(self.db_path)
        conn = sqlite3.connect(uri, uri=True)
        try:
            schema = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall()
            people = conn.execute(
                "SELECT id, name FROM people ORDER BY id"
            ).fetchall()
            notes = conn.execute(
                "SELECT person_id, note FROM notes ORDER BY person_id"
            ).fetchall()
        finally:
            conn.close()
        return {"schema": schema, "people": people, "notes": notes}

    # -- 故障注入 --------------------------------------------------------

    def _start_failure(self):
        """把 report 模块内的裸 open 替换为故障替身（仅接管输出目标）。"""
        def fake_open(path, *args, **kwargs):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(self.out_path):
                self._fake = _FailingOutputFile(
                    os.fspath(path),
                    self.allow_writes,
                    FAIL_MESSAGE,
                    self._capture_failure_moment,
                )
                self.captured_args = args
                self.captured_kwargs = kwargs
                return self._fake
            # 注入窗口内不应有其他 open；保留透传以防实现细节变化
            return _BUILTIN_OPEN(path, *args, **kwargs)

        report.open = fake_open
        self._injected = True

    def _stop_failure(self):
        if not self._injected:
            return
        if self._had_open_attr:
            report.open = self._saved_open
        else:
            del report.open
        self._injected = False

    def _capture_failure_moment(self):
        """故障触发瞬间、半成品被清理之前记录真实磁盘状态。

        只记录、不断言：此处若抛出非 OSError 异常会干扰被测的异常路径。
        """
        self.existed_at_failure = os.path.exists(self.out_path)
        with _BUILTIN_OPEN(self.out_path, "rb") as f:
            self.partial_bytes_at_failure = f.read()

    def _expected_partial_bytes(self):
        buf = io.StringIO()
        csv.writer(buf).writerows(self.partial_rows)
        return buf.getvalue().encode("utf-8")

    # -- 完整时序：注入故障 -> 核对失败与清理 -> 恢复后重试 --------------

    def _check_failure_then_retry(self):
        # 1) 注入确定性写入故障并调用；SQL 合法、目录已存在、目标不存在
        self._start_failure()
        with self.assertRaises(ValueError) as ctx:
            report.export_csv(self.db_path, JOIN_SQL, self.out_path)

        # 故障确实在预期时点触发，且目标是以独占新建模式 "x" 打开的
        self.assertTrue(self._fake.tripped)
        mode = (
            self.captured_args[0]
            if self.captured_args
            else self.captured_kwargs.get("mode")
        )
        self.assertEqual(mode, "x")

        # 错误文本包含统一前缀、目标路径与原始故障消息；调用无返回值
        message = str(ctx.exception)
        self.assertIn("无法写入输出文件", message)
        self.assertIn(self.out_path, message)
        self.assertIn(FAIL_MESSAGE, message)

        # 2) 失败瞬间：目标已被真实创建，磁盘半成品字节恰好对应本时点
        self.assertTrue(self.existed_at_failure)
        self.assertEqual(
            self.partial_bytes_at_failure, self._expected_partial_bytes()
        )

        # 3) 调用结束后：半成品被删除，目标路径不存在
        self.assertFalse(os.path.exists(self.out_path))

        # 同目录其他预置文件字节保持不变
        with _BUILTIN_OPEN(self.other_path, "rb") as f:
            self.assertEqual(f.read(), self.other_bytes)

        # 目录保持可写：可以正常新建并删除探测文件
        probe = os.path.join(self.out_dir, "写入探测.tmp")
        with _BUILTIN_OPEN(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
        self.assertFalse(os.path.exists(probe))

        # 4) 恢复正常写入条件后，用同一源库、同一查询、同一路径再次导出
        self._stop_failure()
        self.assertFalse(os.path.exists(self.out_path))
        count = report.export_csv(self.db_path, JOIN_SQL, self.out_path)
        self.assertEqual(count, 2)

        with _BUILTIN_OPEN(
            self.out_path, "r", encoding="utf-8", newline=""
        ) as f:
            text = f.read()
        rows = list(csv.reader(io.StringIO(text)))
        # 查询列顺序保留；NULL 为空字段；中文及字段内逗号、引号完整读回
        self.assertEqual(rows, EXPECTED_ROWS)
        # CSV 原文层面：含逗号/引号的字段被整体加引号、内部引号双写
        self.assertIn('"中文,含""引号"""', text)

        # 重试后无关文件字节仍保持不变
        with _BUILTIN_OPEN(self.other_path, "rb") as f:
            self.assertEqual(f.read(), self.other_bytes)


class TestHeaderNotYetWrittenFailure(_WriteFailureMixin, unittest.TestCase):
    """目标已创建但表头尚未写成：磁盘上是 0 字节半成品。"""

    allow_writes = 0
    partial_rows = []

    def test_zero_byte_partial_is_reported_cleaned_and_retry_succeeds(self):
        self._check_failure_then_retry()


class TestMidDataWriteFailure(_WriteFailureMixin, unittest.TestCase):
    """表头已写入、第一行数据已落盘，第二行数据写入失败。"""

    allow_writes = 2
    partial_rows = [["姓名", "备注"], ["小明", ""]]

    def test_half_written_partial_is_reported_cleaned_and_retry_succeeds(self):
        self._check_failure_then_retry()


if __name__ == "__main__":
    unittest.main()
