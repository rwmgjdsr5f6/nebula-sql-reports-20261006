#!/usr/bin/env python3
"""export_csv 与 preview_csv 两条入口的 CSV 表达一致性回归测试。

同一查询经文件导出与终端预览应得到完全一致的 CSV 表达：列名、列
顺序、行顺序、引号与记录分隔符相同，NULL 只替换为 null_text，空
字符串、零值及与标记同形的普通文本保持原样，中文、逗号、双引号
与字段内换行完整还原。比较一律按 CSV 逻辑记录（csv.reader 解析）
进行，字段内换行不被当作额外数据行。

只依赖 Python 标准库；每个用例在临时目录中自行准备 people/notes
样例库，用例结束后重新只读打开源库核对表结构与全部数据未变，
全部临时文件随之清理。

在项目根目录执行：
    python -m unittest discover
"""

import csv
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import report

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PY = os.path.join(HERE, "report.py")

# 固定排序的联表查询：列名、列顺序与行顺序由查询本身决定
JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id=n.person_id ORDER BY p.id"
)
HEADERS = ["姓名", "备注"]

NULL_MARKER = "未填写"
# 同时包含中文、逗号与双引号，验证 CSV 解析后完整还原
QUOTED_NOTE = '中文,含"引号"'
# 字段内换行：解析后仍是一条逻辑记录的一个字段
MULTILINE_NOTE = "第一行\n第二行"

# 合法 SELECT，但 fetchall 求值第二项结果时 SQLite 报整数溢出
OVERFLOW_SQL = "SELECT 1 AS 数值 UNION ALL SELECT abs(-9223372036854775808)"
OVERFLOW_REASON = "integer overflow"
SQL_EXEC_FAIL_REASON = "SQL 执行失败"


class CsvConsistencyTestCase(unittest.TestCase):
    """文件导出与终端预览对相同查询的 CSV 表达一致性。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        # 准备完成时的结构、数据基线，tearDown 中逐一核对
        self._baseline = self._snapshot_db()

    def tearDown(self):
        # 每个用例（无论成功或失败）结束后，重新只读打开源库，
        # 核对两表结构及全部数据与准备完成时完全一致
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
                [(1, "小明"), (2, "小红"), (3, "小空"), (4, "小换"), (5, "小标")],
            )
            # 备注依次为：NULL、含逗号与双引号的中文、空字符串、
            # 字段内换行、与 NULL 标记同形的普通文本
            conn.executemany(
                "INSERT INTO notes (person_id, note) VALUES (?, ?)",
                [
                    (1, None),
                    (2, QUOTED_NOTE),
                    (3, ""),
                    (4, MULTILINE_NOTE),
                    (5, NULL_MARKER),
                ],
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

    @staticmethod
    def _parse_csv(text):
        """按 CSV 逻辑记录解析：字段内换行不会拆出额外记录。"""
        return list(csv.reader(io.StringIO(text)))

    def _export(self, sql, name="out.csv", null_text=NULL_MARKER):
        """调用 export_csv，返回 (数据行数, 文件文本, 逻辑记录列表)。"""
        out = os.path.join(self.tmpdir, name)
        count = report.export_csv(
            self.db_path, sql, out, null_text=null_text
        )
        with open(out, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        return count, text, self._parse_csv(text)

    def _preview(self, sql, limit, null_text=NULL_MARKER):
        """调用 preview_csv，返回 (展示行数, 标准输出文本, 逻辑记录列表)。"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            shown = report.preview_csv(
                self.db_path, sql, limit, null_text=null_text
            )
        text = buf.getvalue()
        return shown, text, self._parse_csv(text)

    def _run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path] + list(extra),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    # -- 预览是全量导出的前缀 ------------------------------------------------

    def test_preview_rows_are_prefix_of_full_export(self):
        export_count, _, export_rows = self._export(JOIN_SQL)
        # 导出返回全部数据行数（表头不计入）
        self.assertEqual(export_count, 5)

        # 预览一行对应小明的 NULL 备注；预览两行覆盖小红的含引号中文备注
        for limit in (1, 2):
            with self.subTest(limit=limit):
                shown, _, preview_rows = self._preview(JOIN_SQL, limit)

                # 预览返回实际展示的数据行数
                self.assertEqual(shown, limit)
                # 表头一致，数据行依次为全量导出的前 limit 条
                self.assertEqual(preview_rows[0], HEADERS)
                self.assertEqual(preview_rows, export_rows[:limit + 1])

        # 全量导出的首两行：小明的 NULL 成为标记，小红备注完整还原
        self.assertEqual(export_rows[1], ["小明", NULL_MARKER])
        self.assertEqual(export_rows[2], ["小红", QUOTED_NOTE])

    def test_preview_limit_beyond_count_matches_export_exactly(self):
        export_count, export_text, export_rows = self._export(JOIN_SQL)
        shown, preview_text, preview_rows = self._preview(JOIN_SQL, 100)

        # 预览上限超过结果数量时展示全部
        self.assertEqual(shown, 5)
        self.assertEqual(shown, export_count)
        # 两条入口的 CSV 表达逐字节一致，逻辑记录自然一致
        self.assertEqual(preview_text, export_text)
        self.assertEqual(preview_rows, export_rows)

    def test_zero_row_result_both_keep_header_only(self):
        sql = (
            "SELECT p.name AS 姓名, n.note AS 备注 "
            "FROM people p JOIN notes n ON p.id=n.person_id WHERE p.id < 0"
        )
        export_count, export_text, export_rows = self._export(sql)
        shown, preview_text, preview_rows = self._preview(sql, 5)

        # 零行结果：两条入口都只输出表头，返回 0
        self.assertEqual(export_count, 0)
        self.assertEqual(shown, 0)
        self.assertEqual(export_rows, [HEADERS])
        self.assertEqual(preview_rows, [HEADERS])
        self.assertEqual(preview_text, export_text)

    # -- NULL 标记只替换 NULL ------------------------------------------------

    def test_null_empty_string_and_marker_text_distinguished(self):
        _, _, export_rows = self._export(JOIN_SQL)
        _, _, preview_rows = self._preview(JOIN_SQL, 100)

        for rows in (export_rows, preview_rows):
            # NULL 被替换为标记
            self.assertEqual(rows[1], ["小明", NULL_MARKER])
            # 空字符串保持空字段，不被替换为标记
            self.assertEqual(rows[3], ["小空", ""])
            # 与标记同形的普通文本保持原样，不做额外转换
            self.assertEqual(rows[5], ["小标", NULL_MARKER])
        self.assertEqual(preview_rows, export_rows)

    def test_zero_value_and_empty_string_not_replaced(self):
        sql = "SELECT 0 AS 零值, '' AS 空串"
        export_count, _, export_rows = self._export(sql)
        shown, _, preview_rows = self._preview(sql, 1)

        self.assertEqual(export_count, 1)
        self.assertEqual(shown, 1)
        # 零值与空字符串都不是 NULL，保持原样不替换为标记
        self.assertEqual(export_rows, [["零值", "空串"], ["0", ""]])
        self.assertEqual(preview_rows, export_rows)

    # -- 字段内换行按逻辑记录比较 --------------------------------------------

    def test_embedded_newline_stays_one_logical_record(self):
        _, export_text, export_rows = self._export(JOIN_SQL)
        _, preview_text, preview_rows = self._preview(JOIN_SQL, 100)

        # 字段内换行完整还原为一个字段，两条入口一致
        self.assertEqual(export_rows[4], ["小换", MULTILINE_NOTE])
        self.assertEqual(preview_rows[4], ["小换", MULTILINE_NOTE])
        # 逻辑记录数仍是表头 + 5 条数据，字段内换行不算额外数据行
        self.assertEqual(len(export_rows), 6)
        self.assertEqual(len(preview_rows), 6)
        # 原始文本的物理行数确实多于逻辑记录数：比较必须按逻辑记录进行
        self.assertGreater(export_text.count("\n"), len(export_rows))
        self.assertEqual(preview_text, export_text)

    # -- 命令行入口：两种模式输出一致 ----------------------------------------

    def test_cli_export_and_preview_same_csv(self):
        out = os.path.join(self.tmpdir, "cli_out.csv")
        common = ["--sql", JOIN_SQL, "--null-text", NULL_MARKER]

        proc_export = self._run_cli(*common, "--output", out)
        self.assertEqual(proc_export.returncode, 0, proc_export.stderr)
        # 导出保留原成功提示，标准错误为空
        self.assertEqual(proc_export.stderr, "")
        self.assertIn("已导出 5 行数据", proc_export.stdout)

        proc_preview = self._run_cli(*common, "--preview", "100")
        self.assertEqual(proc_preview.returncode, 0, proc_preview.stderr)
        # 预览只输出 CSV：标准错误为空，不追加成功提示
        self.assertEqual(proc_preview.stderr, "")
        self.assertNotIn("已导出", proc_preview.stdout)

        with open(out, "r", encoding="utf-8", newline="") as f:
            export_rows = self._parse_csv(f.read())
        # 两条命令行入口的 CSV 逻辑记录一致（含字段内换行仍共 6 条记录）
        self.assertEqual(self._parse_csv(proc_preview.stdout), export_rows)
        self.assertEqual(len(export_rows), 6)
        self.assertEqual(export_rows[1], ["小明", NULL_MARKER])
        self.assertEqual(export_rows[2], ["小红", QUOTED_NOTE])
        self.assertEqual(export_rows[4], ["小换", MULTILINE_NOTE])

    # -- 输出时机：求值失败后两条入口都不产生输出 ----------------------------

    def test_overflow_rejected_by_both_entries(self):
        # 即使只预览一行，全部结果求值失败同样拒绝且标准输出为空
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(ValueError) as ctx:
                report.preview_csv(self.db_path, OVERFLOW_SQL, 1)
        self.assertIn(SQL_EXEC_FAIL_REASON, str(ctx.exception))
        self.assertIn(OVERFLOW_REASON, str(ctx.exception))
        self.assertEqual(buf.getvalue(), "")

        # 导出同样拒绝，且不产生文件
        out = os.path.join(self.tmpdir, "overflow.csv")
        with self.assertRaises(ValueError) as ctx:
            report.export_csv(self.db_path, OVERFLOW_SQL, out)
        self.assertIn(SQL_EXEC_FAIL_REASON, str(ctx.exception))
        self.assertFalse(os.path.exists(out))

    def test_cli_overflow_exit_one_both_modes(self):
        proc_preview = self._run_cli("--sql", OVERFLOW_SQL, "--preview", "1")
        self.assertEqual(proc_preview.returncode, 1)
        self.assertIn(SQL_EXEC_FAIL_REASON, proc_preview.stderr)
        self.assertIn(OVERFLOW_REASON, proc_preview.stderr)
        self.assertEqual(proc_preview.stdout, "")

        out = os.path.join(self.tmpdir, "cli_overflow.csv")
        proc_export = self._run_cli("--sql", OVERFLOW_SQL, "--output", out)
        self.assertEqual(proc_export.returncode, 1)
        self.assertIn(SQL_EXEC_FAIL_REASON, proc_export.stderr)
        self.assertIn(OVERFLOW_REASON, proc_export.stderr)
        self.assertNotIn("已导出", proc_export.stdout)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
