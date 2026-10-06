#!/usr/bin/env python3
"""只读 SQL 报告工作台：执行一条 SELECT 查询并导出带列名的 CSV。

用法：
    python report.py --db <SQLite 文件路径> --sql <SELECT 语句> --output <CSV 路径>

- 数据库以只读方式打开，文件缺失时报错而不创建。
- 仅接受一条 SELECT 语句（允许首尾空白、大小写变化、结尾分号）；
  WITH、PRAGMA、写入语句及多语句输入一律拒绝。
- CSV 为无 BOM 的 UTF-8，首行为查询结果的列名（保留 SQL 别名），
  NULL 写作空字段；目标路径已存在时拒绝覆盖。
"""

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="对 SQLite 数据库执行一条 SELECT 查询，并将结果导出为带列名的 CSV。"
    )
    parser.add_argument("--db", required=True, help="已有 SQLite 数据库文件路径")
    parser.add_argument("--sql", required=True, help="要执行的一条 SELECT 语句")
    parser.add_argument("--output", required=True, help="要生成的 CSV 文件路径（不得已存在）")
    return parser.parse_args(argv)


def normalize_select(sql):
    """校验并返回规范化后的单条 SELECT 语句；不合法时抛出 ValueError。"""
    if not isinstance(sql, str):
        raise ValueError("SQL 必须是文本")
    text = sql.strip()
    if not text:
        raise ValueError("SQL 为空")
    # 允许一个结尾分号
    if text.endswith(";"):
        text = text[: -len(";")].strip()
    if not text:
        raise ValueError("SQL 为空")
    # 剩余内容中再出现分号即视为多语句
    if ";" in text:
        raise ValueError("只接受一条语句，不允许多语句输入")
    # 仅接受以 SELECT 开头的单条查询（大小写不限），
    # WITH、PRAGMA 及一切写入语句因此都被拒绝。
    first = text.split(None, 1)[0].upper() if text.split(None, 1) else ""
    if first != "SELECT":
        raise ValueError("只支持单条 SELECT 查询，拒绝 WITH/PRAGMA/写入等其他语句")
    return text


def open_readonly(db_path):
    """以只读模式打开已有数据库；文件缺失或无法打开时抛出异常，绝不创建。"""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"数据库文件不存在：{db_path}")
    uri = Path(db_path).absolute().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def run_query(conn, sql):
    cursor = conn.execute(sql)
    columns = [desc[0] for desc in cursor.description or []]
    rows = cursor.fetchall()
    return columns, rows


def write_csv(output_path, columns, rows):
    """以独占创建方式写出 CSV；任何失败都不留下残缺文件，也不触碰既有文件。"""
    try:
        f = open(output_path, "x", encoding="utf-8", newline="")
    except FileExistsError:
        # 目标早已存在，本次未创建任何文件，直接向上报告，绝不动原文件。
        raise
    try:
        with f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows(rows)
    except BaseException:
        # 文件是本次新建的，写入失败时清理残缺文件。
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise


def main(argv=None):
    args = parse_args(argv)

    try:
        sql = normalize_select(args.sql)
    except ValueError as exc:
        print(f"错误：SQL 被拒绝：{exc}", file=sys.stderr)
        return 1

    try:
        conn = open_readonly(args.db)
    except (FileNotFoundError, sqlite3.Error, OSError) as exc:
        print(f"错误：无法打开源数据库：{exc}", file=sys.stderr)
        return 1

    try:
        try:
            columns, rows = run_query(conn, sql)
        except sqlite3.Error as exc:
            print(f"错误：查询执行失败：{exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    try:
        write_csv(args.output, columns, rows)
    except FileExistsError:
        print(f"错误：目标文件已存在，拒绝覆盖：{args.output}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"错误：输出目录不存在：{args.output}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"错误：无法写入输出文件：{exc}", file=sys.stderr)
        return 1

    print(f"已导出 {len(rows)} 行数据到 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
