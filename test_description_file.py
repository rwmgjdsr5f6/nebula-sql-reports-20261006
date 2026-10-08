#!/usr/bin/env python3
"""--description-file 说明文件的可重复回归测试。

只依赖 Python 标准库；每个用例在临时目录中自行准备 people/notes
样例库、query.sql 查询文件与说明文件，用例结束后重新只读打开源库
核对表结构与全部数据未变，并核对查询文件与说明文件字节相对基线
不变，随后清理全部临时文件。

覆盖要点：
- read_description_file：UTF-8、只剥开头恰一个 BOM、其余文字原样
  保留（不裁剪首尾空白）、空文件读为 ""、中文/空格/换行路径、缺失/
  目录/不可读/编码错误；
- 命令行：说明文件只服务 --format html 的文件导出；与 --description
  互斥（含空串）、重复提供、空路径、缺路径、CSV/默认格式、--preview、
  --tables、--describe 均在读取任何输入文件前拒绝；文件错误时不开
  源库、不建输出、既有目标字节不变；空文件输出与省略说明逐字节一致；
  相同内容经 --description 与 --description-file 导出逐字节一致。

在项目根目录执行：
    python -m unittest discover
或单独运行：
    python -m unittest test_description_file
"""

import html
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import report

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PY = os.path.join(HERE, "report.py")

# 与验收相同的 people/notes 合成样例：小明备注为 NULL，小红备注含逗号与引号
NOTE_VALUE = '中文,含"引号"'

DESC_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id = n.person_id "
    "WHERE p.name = :who ORDER BY p.id"
)

# 验收说明文件的两行内容：一行中文口径，一行 <script> 形态（只按文字显示）
NOTES_TEXT = "筛选小明\n<script>仅说明</script>\n"
NOTES_ESCAPED = html.escape(NOTES_TEXT)

# 说明区域的固定位置边界：主标题之后、结果表格之前
DESC_H1 = "<h1>查询报告</h1>"


def _description_block(text):
    """取出主标题与结果表格之间的页面片段（说明区域所在位置）。"""
    head_end = text.index(DESC_H1) + len(DESC_H1)
    table_start = text.index("<table>")
    return text[head_end:table_start]


class ReadDescriptionFileTestCase(unittest.TestCase):
    """read_description_file 的函数级校验。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, content):
        """以原始字节或文本写入临时文件，返回路径。"""
        path = os.path.join(self.tmpdir, name)
        if isinstance(content, str):
            content = content.encode("utf-8")
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_plain_text_returned_verbatim(self):
        path = self._write("notes.txt", NOTES_TEXT)
        self.assertEqual(report.read_description_file(path), NOTES_TEXT)

    def test_single_leading_bom_stripped(self):
        path = self._write(
            "bom.txt", b"\xef\xbb\xbf" + NOTES_TEXT.encode("utf-8")
        )
        self.assertEqual(report.read_description_file(path), NOTES_TEXT)

    def test_no_trimming_of_surrounding_whitespace(self):
        text = "  前后  空格与换行 \n\t "
        path = self._write("ws.txt", text)
        self.assertEqual(report.read_description_file(path), text)

    def test_chinese_spaces_and_newlines_preserved(self):
        text = "第一行  连续  空格\n第二行\n\n第四行"
        path = self._write("多行.txt", text)
        self.assertEqual(report.read_description_file(path), text)

    def test_empty_file_reads_as_empty_string(self):
        path = self._write("empty.txt", b"")
        self.assertEqual(report.read_description_file(path), "")

    def test_whitespace_only_file_preserved(self):
        path = self._write("only_ws.txt", "  \n\t  ")
        self.assertEqual(report.read_description_file(path), "  \n\t  ")

    def test_path_with_chinese_and_spaces(self):
        path = self._write("说明 文件.txt", NOTES_TEXT)
        self.assertEqual(report.read_description_file(path), NOTES_TEXT)

    def test_file_bytes_unchanged_after_read(self):
        raw = b"\xef\xbb\xbf" + NOTES_TEXT.encode("utf-8")
        path = self._write("keep.txt", raw)
        report.read_description_file(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_missing_file_rejected(self):
        missing = os.path.join(self.tmpdir, "nope.txt")
        with self.assertRaises(ValueError) as ctx:
            report.read_description_file(missing)
        self.assertIn("说明文件不存在", str(ctx.exception))
        self.assertIn(missing, str(ctx.exception))

    def test_directory_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            report.read_description_file(self.tmpdir)
        self.assertIn("目录", str(ctx.exception))
        self.assertIn(self.tmpdir, str(ctx.exception))

    def test_invalid_utf8_rejected(self):
        path = self._write("bad.txt", "筛选\n".encode("utf-8") + b"\xff\xfe")
        with self.assertRaises(ValueError) as ctx:
            report.read_description_file(path)
        self.assertIn("UTF-8", str(ctx.exception))
        self.assertIn(path, str(ctx.exception))

    def test_unreadable_file_rejected(self):
        path = self._write("noread.txt", NOTES_TEXT)
        os.chmod(path, 0)
        try:
            # root 可绕过权限位；普通用户下 open 必失败
            if os.geteuid() != 0:
                with self.assertRaises(ValueError):
                    report.read_description_file(path)
        finally:
            os.chmod(path, 0o600)


class DescriptionFileCliTestCase(unittest.TestCase):
    """--description-file 命令行入口的端到端行为。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self.sql_path = os.path.join(self.tmpdir, "query.sql")
        with open(self.sql_path, "w", encoding="utf-8") as f:
            f.write(DESC_SQL + ";\n")
        with open(self.sql_path, "rb") as f:
            self._sql_bytes = f.read()
        self._prepare_db()
        self._baseline = self._snapshot_db()

    def tearDown(self):
        self.assertEqual(self._snapshot_db(), self._baseline)
        # 查询文件始终只被读取
        with open(self.sql_path, "rb") as f:
            self.assertEqual(f.read(), self._sql_bytes)
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
            conn.executemany(
                "INSERT INTO notes (person_id, note) VALUES (?, ?)",
                [(1, None), (2, NOTE_VALUE)],
            )
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
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

    def _write_desc(self, name, content):
        """以原始字节或文本写入说明文件，返回路径。"""
        path = os.path.join(self.tmpdir, name)
        if isinstance(content, str):
            content = content.encode("utf-8")
        with open(path, "wb") as f:
            f.write(content)
        return path

    def _run_cli(self, *extra, fmt="html", output=None, sql_file=None):
        cmd = [sys.executable, REPORT_PY, "--db", self.db_path,
               "--sql-file", sql_file or self.sql_path]
        if fmt is not None:
            cmd += ["--format", fmt]
        if output is not None:
            cmd += ["--output", output]
        cmd += list(extra)
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8"
        )

    @staticmethod
    def _read_bytes(path):
        with open(path, "rb") as f:
            return f.read()

    # -- 验收场景：两行说明、无脚本、表格与 NULL 规则不变 -------------------

    def test_acceptance_two_lines_no_script_table_unchanged(self):
        notes = self._write_desc("notes.txt", NOTES_TEXT)
        out = os.path.join(self.tmpdir, "result.html")
        proc = self._run_cli(
            "--param", "who=小明", "--description-file", notes, output=out
        )
        # 退出码、行数提示与原 HTML 导出一致
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(proc.stdout, "已导出 1 行数据：%s\n" % out)

        text = self._read_bytes(out).decode("utf-8")
        # 两行说明按文字显示在主标题之后、结果表格之前
        block = _description_block(text)
        self.assertIn(NOTES_ESCAPED, block)
        self.assertIn("筛选小明", block)
        self.assertIn("&lt;script&gt;仅说明&lt;/script&gt;", block)
        # <script> 形态不产生标签，说明原文不出现于页面
        self.assertNotIn("<script", text)
        self.assertNotIn(NOTES_TEXT, text)
        # 说明不进入页面标题
        self.assertIn("<title>查询报告</title>", text)
        # 表格仍为姓名、备注两列与小明的一行；NULL 按原规则显示为空单元格
        self.assertIn("<tr><th>姓名</th><th>备注</th></tr>", text)
        self.assertIn("<tr><td>小明</td><td></td></tr>", text)
        self.assertNotIn("小红", text)
        # 说明文件只被读取，字节未变
        self.assertEqual(
            self._read_bytes(notes), NOTES_TEXT.encode("utf-8")
        )

    def test_same_content_via_description_byte_identical(self):
        notes = self._write_desc("notes.txt", NOTES_TEXT)
        out_file = os.path.join(self.tmpdir, "from_file.html")
        out_inline = os.path.join(self.tmpdir, "from_inline.html")
        p1 = self._run_cli(
            "--param", "who=小明", "--description-file", notes,
            output=out_file,
        )
        p2 = self._run_cli(
            "--param", "who=小明", "--description", NOTES_TEXT,
            output=out_inline,
        )
        self.assertEqual(p1.returncode, 0, p1.stderr)
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertEqual(self._read_bytes(out_file), self._read_bytes(out_inline))

    def test_empty_file_byte_identical_to_omitted_description(self):
        notes = self._write_desc("empty.txt", b"")
        out_file = os.path.join(self.tmpdir, "empty_desc.html")
        out_plain = os.path.join(self.tmpdir, "plain.html")
        p1 = self._run_cli(
            "--param", "who=小明", "--description-file", notes,
            output=out_file,
        )
        p2 = self._run_cli("--param", "who=小明", output=out_plain)
        self.assertEqual(p1.returncode, 0, p1.stderr)
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertEqual(self._read_bytes(out_file), self._read_bytes(out_plain))
        # 省略说明时主标题与表格之间没有任何说明区域
        text = self._read_bytes(out_plain).decode("utf-8")
        self.assertEqual(_description_block(text), "\n")

    def test_whitespace_only_file_still_shows_description_area(self):
        notes = self._write_desc("ws.txt", "  \n\t  ")
        out = os.path.join(self.tmpdir, "ws.html")
        proc = self._run_cli(
            "--param", "who=小明", "--description-file", notes, output=out
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = self._read_bytes(out).decode("utf-8")
        self.assertIn(html.escape("  \n\t  "), _description_block(text))

    def test_bom_file_and_chinese_space_path(self):
        notes = self._write_desc(
            "说明 文件.txt", b"\xef\xbb\xbf" + NOTES_TEXT.encode("utf-8")
        )
        out = os.path.join(self.tmpdir, "bom.html")
        proc = self._run_cli(
            "--param", "who=小明", "--description-file", notes, output=out
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = self._read_bytes(out).decode("utf-8")
        self.assertIn(NOTES_ESCAPED, _description_block(text))

    # -- 用法错误：读取任何输入文件前拒绝 -----------------------------------

    def _assert_usage_error(self, proc, out, *expect):
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("错误: ", proc.stderr)
        for fragment in expect:
            self.assertIn(fragment, proc.stderr)
        if out is not None:
            self.assertFalse(os.path.exists(out))

    def test_mutually_exclusive_with_description_even_empty(self):
        notes = self._write_desc("notes.txt", NOTES_TEXT)
        for desc_value in ("别的说明", ""):
            with self.subTest(desc_value=desc_value):
                out = os.path.join(self.tmpdir, "both.html")
                proc = self._run_cli(
                    "--param", "who=小明",
                    "--description", desc_value,
                    "--description-file", notes,
                    output=out,
                )
                self._assert_usage_error(
                    proc, out, "--description-file", "--description"
                )

    def test_mutual_exclusion_checked_before_reading_file(self):
        # 说明文件不存在也必须先报互斥，证明该判定不读取说明文件
        missing = os.path.join(self.tmpdir, "nope.txt")
        out = os.path.join(self.tmpdir, "both2.html")
        proc = self._run_cli(
            "--param", "who=小明",
            "--description", "x", "--description-file", missing,
            output=out,
        )
        self._assert_usage_error(proc, out, "不能同时使用")
        self.assertNotIn("说明文件不存在", proc.stderr)

    def test_repeated_description_file_rejected_without_reading(self):
        missing = os.path.join(self.tmpdir, "nope.txt")
        out = os.path.join(self.tmpdir, "dup.html")
        proc = self._run_cli(
            "--param", "who=小明",
            "--description-file", missing, "--description-file", missing,
            output=out,
        )
        self._assert_usage_error(proc, out, "只能提供一次")
        self.assertNotIn("说明文件不存在", proc.stderr)

    def test_empty_description_file_path_rejected(self):
        out = os.path.join(self.tmpdir, "empty_path.html")
        proc = self._run_cli(
            "--param", "who=小明", "--description-file", "", output=out
        )
        self._assert_usage_error(proc, out, "路径不能为空")

    def test_missing_description_file_value_rejected(self):
        out = os.path.join(self.tmpdir, "no_value.html")
        # --description-file 放在末尾使其后面无值可消费，触发缺值错误
        cmd = [
            sys.executable, REPORT_PY,
            "--db", self.db_path,
            "--sql-file", self.sql_path,
            "--param", "who=小明",
            "--format", "html",
            "--output", out,
            "--description-file",
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8"
        )
        self._assert_usage_error(proc, out, "--description-file")

    def test_csv_and_default_format_rejected_before_reading(self):
        # 说明文件不存在也必须先报格式错误，证明该判定不读取说明文件
        missing = os.path.join(self.tmpdir, "nope.txt")
        for fmt in ("csv", None):
            with self.subTest(fmt=fmt):
                out = os.path.join(self.tmpdir, "fmt.html")
                proc = self._run_cli(
                    "--param", "who=小明", "--description-file", missing,
                    output=out, fmt=fmt,
                )
                self._assert_usage_error(
                    proc, out, "--description-file", "html"
                )
                self.assertNotIn("说明文件不存在", proc.stderr)

    def test_preview_rejected_before_reading(self):
        missing = os.path.join(self.tmpdir, "nope.txt")
        proc = self._run_cli(
            "--param", "who=小明", "--description-file", missing,
            "--preview", "1", fmt=None,
        )
        self._assert_usage_error(proc, None, "--preview", "--description-file")
        self.assertNotIn("说明文件不存在", proc.stderr)

    def test_tables_and_describe_rejected_before_reading(self):
        missing = os.path.join(self.tmpdir, "nope.txt")
        for mode in (["--tables"], ["--describe", "people"]):
            with self.subTest(mode=mode):
                proc = subprocess.run(
                    [sys.executable, REPORT_PY, "--db", self.db_path]
                    + mode + ["--description-file", missing],
                    capture_output=True, text=True, encoding="utf-8",
                )
                self._assert_usage_error(
                    proc, None, mode[0], "--description-file"
                )
                self.assertNotIn("说明文件不存在", proc.stderr)

    # -- 文件错误：退出码 1、stdout 为空、不开源库、不建输出 -----------------

    def test_missing_file_error_no_output_no_db_created(self):
        missing_db = os.path.join(self.tmpdir, "missing.sqlite")
        missing_desc = os.path.join(self.tmpdir, "nope.txt")
        out = os.path.join(self.tmpdir, "out.html")
        proc = subprocess.run(
            [sys.executable, REPORT_PY,
             "--db", missing_db,
             "--sql-file", self.sql_path,
             "--param", "who=小明",
             "--format", "html",
             "--description-file", missing_desc,
             "--output", out],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("错误: "), proc.stderr)
        self.assertIn("说明文件不存在", proc.stderr)
        self.assertIn(missing_desc, proc.stderr)
        # 不打开源库（缺失库不会被创建）、不创建输出文件
        self.assertFalse(os.path.exists(missing_db))
        self.assertFalse(os.path.exists(out))

    def test_directory_error_exit_one_empty_stdout(self):
        out = os.path.join(self.tmpdir, "dir.html")
        proc = self._run_cli(
            "--param", "who=小明", "--description-file", self.tmpdir,
            output=out,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("错误: "), proc.stderr)
        self.assertIn("目录", proc.stderr)
        self.assertIn(self.tmpdir, proc.stderr)
        self.assertFalse(os.path.exists(out))

    def test_invalid_utf8_error_keeps_existing_target_bytes(self):
        notes = self._write_desc("bad.txt", "筛选\n".encode("utf-8") + b"\xff")
        out = os.path.join(self.tmpdir, "existing.html")
        original = "已有内容，不得变化\n".encode("utf-8")
        with open(out, "wb") as f:
            f.write(original)
        proc = self._run_cli(
            "--param", "who=小明", "--description-file", notes, output=out
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("错误: "), proc.stderr)
        self.assertIn("UTF-8", proc.stderr)
        self.assertIn(notes, proc.stderr)
        # 既有目标保持原字节
        self.assertEqual(self._read_bytes(out), original)

    def test_missing_file_error_keeps_existing_target_bytes(self):
        out = os.path.join(self.tmpdir, "existing2.html")
        original = "已有内容，不得变化\n".encode("utf-8")
        with open(out, "wb") as f:
            f.write(original)
        proc = self._run_cli(
            "--param", "who=小明",
            "--description-file", os.path.join(self.tmpdir, "nope.txt"),
            output=out,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("说明文件不存在", proc.stderr)
        self.assertEqual(self._read_bytes(out), original)

    # -- 未使用新选项时行为不变 ----------------------------------------------

    def test_without_description_file_html_export_unchanged(self):
        out = os.path.join(self.tmpdir, "plain.html")
        proc = self._run_cli("--param", "who=小明", output=out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(proc.stdout, "已导出 1 行数据：%s\n" % out)
        text = self._read_bytes(out).decode("utf-8")
        self.assertEqual(_description_block(text), "\n")
        self.assertIn("<tr><td>小明</td><td></td></tr>", text)


if __name__ == "__main__":
    unittest.main()
