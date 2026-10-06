#!/usr/bin/env python3
"""以只读方式对 SQLite 执行一条 SELECT，并将结果导出为带列名的 CSV。

用法:
    python report.py --db DB.sqlite --sql "SELECT ..." --output out.csv

仅接受一条 SELECT 语句（允许首尾空白与结尾分号）；WITH、PRAGMA、写入
语句及多语句输入一律拒绝。数据库以只读方式打开，缺失时不会创建；输出
目标已存在时拒绝写入，绝不覆盖或截断。
"""

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path


def die(message):
    """向标准错误输出具体原因并以退出码 1 结束。"""
    print("错误: " + message, file=sys.stderr)
    sys.exit(1)


def _skip_quoted(sql, i):
    """若 sql[i] 是引号（'/\"/`）或方括号标识符，返回跳过该段后的下标；否则返回 None。"""
    n = len(sql)
    ch = sql[i]
    if ch in ("'", '"', "`"):
        quote = ch
        i += 1
        while i < n:
            if sql[i] == quote:
                # SQL 标准：引号在自身引号内双写表示字面引号
                if i + 1 < n and sql[i + 1] == quote:
                    i += 2
                    continue
                return i + 1
            i += 1
        return n  # 未闭合的引号：吞到末尾，交由 SQLite 报语法错误
    if ch == "[":
        i += 1
        while i < n:
            if sql[i] == "]":
                return i + 1
            i += 1
        return n
    return None


def strip_sql_comments(sql):
    """去除 SQL 中的 -- 行注释与 C 风格块注释，引号内的注释文本保持原样。"""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        skipped = _skip_quoted(sql, i)
        if skipped is not None:
            out.append(sql[i:skipped])
            i = skipped
            continue
        if sql[i] == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i + 2)
            if j == -1:
                break
            i = j
            continue
        if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def split_statements(sql, keep_empty=False):
    """按分号切分语句，字符串与标识符引号内的分号不切分。

    返回去掉首尾空白后的语句列表；keep_empty 时保留空段，用于识别
    "前导分号"、"多个分号"等多语句形态。
    """
    statements = []
    buf = []
    i = 0
    n = len(sql)
    while i < n:
        skipped = _skip_quoted(sql, i)
        if skipped is not None:
            buf.append(sql[i:skipped])
            i = skipped
            continue
        if sql[i] == ";":
            stmt = "".join(buf).strip()
            if stmt or keep_empty:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(sql[i])
        i += 1
    tail = "".join(buf).strip()
    if tail or keep_empty:
        statements.append(tail)
    return statements


def first_token(statement):
    """取语句首个词法 token（大写）及其后的文本；左括号原样返回。"""
    s = statement.lstrip()
    if s[:1] == "(":
        return "(", s
    end = 0
    while end < len(s) and (s[end].isalnum() or s[end] == "_"):
        end += 1
    return s[:end].upper(), s[end:].lstrip()


def unwrap_full_parentheses(statement):
    """若整条语句被一对外层括号包裹，返回括号内文本；否则返回 None。"""
    s = statement.strip()
    if not s.startswith("("):
        return None
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        skipped = _skip_quoted(s, i)
        if skipped is not None:
            i = skipped
            continue
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                if s[i + 1:].strip():
                    return None  # 闭合括号后还有多余内容
                return s[1:i].strip()
        i += 1
    return None  # 括号未闭合


def validate_single_select(sql, _depth=0):
    """校验输入恰为一条 SELECT 语句，返回可执行的语句文本，不合法则抛 ValueError。"""
    if _depth > 20:
        raise ValueError("不支持的语句：括号嵌套过深")

    segments = split_statements(strip_sql_comments(sql), keep_empty=True)
    nonempty = [s for s in segments if s]
    if not nonempty:
        raise ValueError("SQL 为空：必须提供一条 SELECT 语句")
    if len(nonempty) > 1:
        raise ValueError("仅允许一条 SELECT 语句，检测到多条语句输入")
    # 只允许末尾一个分号：空段至多一个，且必须位于最后
    if len(segments) - 1 > 1 or segments[0] == "":
        raise ValueError("仅允许一条 SELECT 语句，检测到多余的分号或多条语句")

    statement = nonempty[0]
    keyword, rest = first_token(statement)

    if keyword == "(":
        inner = unwrap_full_parentheses(statement)
        if inner is None:
            raise ValueError("不支持的语句：仅允许 SELECT")
        validate_single_select(inner, _depth + 1)
        return statement  # 原文本括号合法，可直接交给 SQLite

    if keyword == "WITH":
        raise ValueError("不支持的语句：WITH/CTE 查询被拒绝，仅允许普通 SELECT")
    if keyword == "EXPLAIN":
        raise ValueError("不支持的语句：EXPLAIN 被拒绝，仅允许 SELECT")
    if keyword == "PRAGMA":
        raise ValueError("不支持的语句：PRAGMA 被拒绝，仅允许 SELECT")
    if keyword != "SELECT":
        raise ValueError("不支持的语句：仅允许 SELECT，但收到 %r" % (keyword or "非语句文本"))
    if not rest:
        raise ValueError("SQL 语法错误：SELECT 后缺少查询列")
    return statement


def open_readonly(db_path):
    """以只读模式打开 SQLite；文件缺失或损坏时抛 ValueError，且绝不会创建新库。"""
    uri = Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        # 连接惰性建立；读取 sqlite_master 会强制解析文件头与首页，
        # 从而暴露"文件不存在"与"不是数据库"等问题（SELECT 1 做不到）。
        conn.execute("SELECT count(*) FROM sqlite_master").fetchall()
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise ValueError("无法以只读方式打开数据库 %s：%s" % (db_path, exc))
    return conn


def export_csv(db_path, sql_text, output_path):
    """执行查询并将结果独占写入目标 CSV，返回数据行数。任何拒绝路径都不建文件。"""
    statement = validate_single_select(sql_text)

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if not os.path.isdir(output_dir):
        raise ValueError("输出目录不存在：%s" % output_dir)
    if os.path.exists(output_path):
        raise ValueError("输出目标已存在，拒绝覆盖：%s" % output_path)

    conn = open_readonly(db_path)
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(statement)
        except sqlite3.Error as exc:
            raise ValueError("SQL 执行失败：%s" % exc)
        if cursor.description is None:
            raise ValueError("不支持的语句：仅允许返回结果集的 SELECT")
        headers = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
    finally:
        conn.close()

    # "x" = 独占新建：目标已存在则直接失败，从根本上杜绝覆盖或截断
    try:
        with open(output_path, "x", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(["" if value is None else value for value in row])
    except FileExistsError:
        raise ValueError("输出目标已存在，拒绝覆盖：%s" % output_path)
    except (OSError, UnicodeError, TypeError) as exc:
        # 到此的文件必为本调用刚创建的半成品，清理后报错；既存文件不可能被触及
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise ValueError("无法写入输出文件 %s：%s" % (output_path, exc))

    return len(rows)


class _Parser(argparse.ArgumentParser):
    """参数用法错误也按本工具的约定使用退出码 1（argparse 默认是 2）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        die("参数错误：%s" % message)


def parse_args(argv):
    parser = _Parser(
        description="对 SQLite 执行一条只读 SELECT 并导出带列名的 CSV"
    )
    parser.add_argument("--db", required=True, help="已有 SQLite 数据库文件路径")
    parser.add_argument("--sql", required=True, help="一条 SELECT 查询文本")
    parser.add_argument("--output", required=True, help="输出 CSV 路径（不得已存在）")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        row_count = export_csv(args.db, args.sql, args.output)
    except ValueError as exc:
        die(str(exc))
    except sqlite3.Error as exc:
        die("数据库错误：%s" % exc)
    except OSError as exc:
        die("文件错误：%s" % exc)
    print("已导出 %d 行数据：%s" % (row_count, args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
