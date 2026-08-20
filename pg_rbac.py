#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
psycopg2-binary pyyaml  rich prompt_toolkit
PostgreSQL RBAC 权限管理工具 v3.0
模块划分：角色管理、用户管理、权限管理、权限查询
支持：配置文件连接、正则查表授权、Dry-run生成SQL、权限级联查询
使用 rich 美化终端输出
"""

import argparse
import re
import shlex
import sys
from pathlib import Path

import psycopg2
import yaml
from psycopg2 import sql
from prompt_toolkit import PromptSession, print_formatted_text

# PostgreSQL 合法权限关键字白名单
_VALID_PRIVS = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER",
    "USAGE", "CREATE", "CONNECT", "TEMPORARY", "EXECUTE",
    "ALL", "ALL PRIVILEGES",
}
_VALID_OBJ_TYPES = {"TABLES", "SEQUENCES", "FUNCTIONS", "TYPES"}


def _validate_privileges(privileges, context=""):
    for p in privileges.split(","):
        p = p.strip().upper()
        if p not in _VALID_PRIVS:
            raise ValueError("非法权限 '{}'{}, 允许: {}".format(p, " (" + context + ")" if context else "", ", ".join(sorted(_VALID_PRIVS))))


def _validate_obj_type(obj_type):
    obj_type = obj_type.upper()
    if obj_type not in _VALID_OBJ_TYPES:
        raise ValueError("非法对象类型 '{}', 允许: {}".format(obj_type, ", ".join(sorted(_VALID_OBJ_TYPES))))
    return obj_type


from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from datetime import datetime, timezone
from collections import OrderedDict
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console()


class SQLRenderer:
    """
    SQL 渲染器 - 在没有数据库连接的情况下序列化 psycopg2.sql 对象
    用于 dry-run 模式，避免对数据库连接的依赖
    """

    def render(self, query, params=None):
        if isinstance(query, str):
            result = query
        elif isinstance(query, sql.Identifier):
            parts = []
            for part in query._wrapped:
                escaped = part.replace('"', '""')
                parts.append(f'"{escaped}"')
            result = '.'.join(parts)
        elif isinstance(query, sql.Literal):
            if query._wrapped is None:
                result = 'NULL'
            else:
                value = str(query._wrapped)
                escaped = value.replace("'", "''")
                result = f"'{escaped}'"
        elif isinstance(query, sql.Composed):
            parts = []
            for part in query.seq:
                if isinstance(part, (sql.Identifier, sql.Literal, sql.Composed)):
                    parts.append(self.render(part))
                elif isinstance(part, sql.SQL):
                    if hasattr(part, '_wrapped'):
                        parts.append(part._wrapped)
                    else:
                        part_str = str(part)
                        if part_str.startswith("SQL('") and part_str.endswith("')"):
                            parts.append(part_str[5:-2])
                        else:
                            parts.append(part_str)
                elif isinstance(part, sql.Placeholder):
                    parts.append('%s')
                else:
                    parts.append(str(part))
            result = ''.join(parts)
        elif isinstance(query, sql.SQL):
            if hasattr(query, '_wrapped'):
                result = query._wrapped
            else:
                query_str = str(query)
                if query_str.startswith("SQL('") and query_str.endswith("')"):
                    result = query_str[5:-2]
                else:
                    result = query_str
        else:
            result = str(query)

        if params:
            if isinstance(params, (list, tuple)):
                params_list = list(params)
                for param in params_list:
                    if isinstance(param, str):
                        escaped = param.replace("'", "''")
                        result = result.replace('%s', f"'{escaped}'", 1)
                    elif param is None:
                        result = result.replace('%s', 'NULL', 1)
                    else:
                        result = result.replace('%s', str(param), 1)

        return result


class PgRbacManager:
    def __init__(self, config_path="config.yaml", dry_run=False):
        self.dry_run = dry_run
        self.config = self._load_config(config_path)
        self.conn = None
        if not dry_run:
            try:
                self._connect()
            except Exception:
                raise

    def _load_config(self, config_path):
        if not Path(config_path).exists():
            console.print(f"[bold red][ERROR] 配置文件不存在: {config_path}[/bold red]")
            raise FileNotFoundError(config_path)
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _connect(self):
        db = self.config["database"]
        try:
            self.conn = psycopg2.connect(
                host=db["host"],
                port=db["port"],
                user=db["user"],
                password=db["password"],
                dbname=db["dbname"],
            )
            self.conn.autocommit = self.config["settings"].get("autocommit", False)
        except Exception as e:
            console.print(f"[bold red][ERROR] 数据库连接失败: {e}[/bold red]")
            raise

    def _switch_db(self, dbname):
        """切换到指定数据库（表/序列/函数权限只能对当前连接的库操作）"""
        current_db = self.conn.info.dbname if self.conn else None
        if current_db == dbname:
            return
        old_conn = self.conn
        db = self.config["database"]
        try:
            self.conn = psycopg2.connect(
                host=db["host"],
                port=db["port"],
                user=db["user"],
                password=db["password"],
                dbname=dbname,
            )
            self.conn.autocommit = self.config["settings"].get("autocommit", False)
            if old_conn:
                old_conn.close()
            console.print(f"[dim]已切换到数据库: {dbname}[/dim]")
        except Exception as e:
            self.conn = old_conn
            console.print(f"[bold red][ERROR] 切换数据库失败: {e}[/bold red]")
            raise

    def _execute(self, query, params=None, fetch=False):
        if self.dry_run:
            renderer = SQLRenderer()
            query_str = renderer.render(query, params)
            console.print(f"[dim]-- SQL: {query_str}[/dim]")
            return [] if fetch else True

        try:
            query_str = query.as_string(self.conn) if isinstance(query, sql.Composed) else query

            with self.conn.cursor() as cur:
                cur.execute(query, params)

                if fetch:
                    result = cur.fetchall()
                    self.conn.commit()
                    return result

                self.conn.commit()
                return True

        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            console.print(f"[bold red][ERROR] 执行失败: {e}[/bold red]")
            console.print(f"[red]  SQL: {query_str if 'query_str' in locals() else query}[/red]")
            if params:
                console.print(f"[red]  参数: {params}[/red]")
            return None

    # ============================================================
    # 第一部分：角色管理
    # ============================================================
    def role_create(self, role_name, comment=None):
        query = sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role_name))
        if self._execute(query):
            if not self.dry_run:
                console.print(f"[bold green][OK][/bold green] 角色 [cyan]{role_name}[/cyan] 创建成功")
            if comment:
                c_query = sql.SQL("COMMENT ON ROLE {} IS %s").format(sql.Identifier(role_name))
                self._execute(c_query, (comment,))

    def role_drop(self, role_name):
        query = sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name))
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 角色 [cyan]{role_name}[/cyan] 已删除")

    def role_list(self):
        query = """
            SELECT rolname, rolsuper
            FROM pg_roles
            WHERE rolcanlogin = false
              AND rolname NOT LIKE 'pg_%'
            ORDER BY rolname
        """
        rows = self._execute(query, fetch=True)
        if not rows:
            console.print("[dim]无自定义角色[/dim]")
            return

        table = Table(title="角色列表", show_lines=False, border_style="blue")
        table.add_column("角色名", style="cyan", no_wrap=True)
        table.add_column("超级用户", style="white")

        for row in rows:
            super_str = "[bold red]是[/bold red]" if row[1] else "否"
            table.add_row(row[0], super_str)

        console.print(table)

    # ============================================================
    # 第二部分：用户管理
    # ============================================================
    def user_create(self, username, password, comment=None):
        query = sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(username))
        if self._execute(query, (password,)):
            if not self.dry_run:
                console.print(f"[bold green][OK][/bold green] 用户 [cyan]{username}[/cyan] 创建成功")
            if comment:
                c_query = sql.SQL("COMMENT ON ROLE {} IS %s").format(sql.Identifier(username))
                self._execute(c_query, (comment,))

    def user_drop(self, username):
        query = sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(username))
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 用户 [cyan]{username}[/cyan] 已删除")

    def user_change_password(self, username, password):
        query = sql.SQL("ALTER ROLE {} WITH PASSWORD %s").format(sql.Identifier(username))
        if self._execute(query, (password,)) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 用户 [cyan]{username}[/cyan] 密码已修改")

    def user_disable(self, username):
        query = sql.SQL("ALTER ROLE {} VALID UNTIL '1970-01-01'").format(sql.Identifier(username))
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 用户 [cyan]{username}[/cyan] 已禁用（密码已过期）")

    def user_enable(self, username):
        query = sql.SQL("ALTER ROLE {} VALID UNTIL 'infinity'").format(sql.Identifier(username))
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 用户 [cyan]{username}[/cyan] 已启用（永不过期）")

    def user_grant_role(self, username, role_name):
        query = sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(role_name),
            sql.Identifier(username),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 用户 [cyan]{username}[/cyan] 已继承角色 [magenta]{role_name}[/magenta]")

    def user_revoke_role(self, username, role_name):
        query = sql.SQL("REVOKE {} FROM {}").format(
            sql.Identifier(role_name),
            sql.Identifier(username),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 已从用户 [cyan]{username}[/cyan] 回收角色 [magenta]{role_name}[/magenta]")

    def user_list(self):
        query = """
            SELECT r.rolname, r.rolsuper, r.rolinherit,
                   ARRAY(
                       SELECT m2.roleid::regrole::text
                       FROM pg_auth_members m2
                       WHERE m2.member = r.oid
                   ) AS member_of,
                   r.rolvaliduntil
            FROM pg_roles r
            WHERE r.rolcanlogin = true
              AND r.rolname NOT LIKE 'pg_%'
            ORDER BY r.rolname
        """
        rows = self._execute(query, fetch=True)
        if not rows:
            console.print("[dim]无可登录用户[/dim]")
            return

        table = Table(title="用户列表", show_lines=False, border_style="green")
        table.add_column("用户名", style="cyan", no_wrap=True)
        table.add_column("状态", style="white")
        table.add_column("过期时间", style="dim")
        table.add_column("超级用户", style="white")
        table.add_column("继承权限", style="white")
        table.add_column("所属角色", style="magenta")

        for row in rows:
            valid_until = row[4]
            if valid_until is None:
                status = "[bold green]启用[/bold green]"
                expire_str = "永不过期"
            else:
                if isinstance(valid_until, datetime):
                    if valid_until.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
                        status = "[bold red]已禁用[/bold red]"
                    else:
                        status = "[bold green]启用[/bold green]"
                    expire_str = str(valid_until)
                else:
                    status = "[bold green]启用[/bold green]"
                    expire_str = str(valid_until)
            roles = ", ".join(row[3]) if row[3] else "[dim]无[/dim]"
            super_str = "[bold red]是[/bold red]" if row[1] else "否"
            inherit_str = "是" if row[2] else "否"
            table.add_row(row[0], status, expire_str, super_str, inherit_str, roles)

        console.print(table)

    # ============================================================
    # 第三部分：权限管理
    # ============================================================
    def priv_grant_database(self, role_name, dbname, privileges):
        _validate_privileges(privileges, "grant-db")
        query = sql.SQL("GRANT {} ON DATABASE {} TO {}").format(
            sql.SQL(privileges),
            sql.Identifier(dbname),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 角色 [cyan]{role_name}[/cyan] 获得数据库 [yellow]{dbname}[/yellow] 的 [bold]{privileges}[/bold] 权限")

    def priv_revoke_database(self, role_name, dbname, privileges):
        _validate_privileges(privileges, "revoke-db")
        query = sql.SQL("REVOKE {} ON DATABASE {} FROM {}").format(
            sql.SQL(privileges),
            sql.Identifier(dbname),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 已回收角色 [cyan]{role_name}[/cyan] 在数据库 [yellow]{dbname}[/yellow] 的 [bold]{privileges}[/bold] 权限")

    def priv_grant_schema(self, role_name, schema_name, privileges):
        _validate_privileges(privileges, "grant-schema")
        query = sql.SQL("GRANT {} ON SCHEMA {} TO {}").format(
            sql.SQL(privileges),
            sql.Identifier(schema_name),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 角色 [cyan]{role_name}[/cyan] 获得 Schema [yellow]{schema_name}[/yellow] 的 [bold]{privileges}[/bold] 权限")

    def priv_revoke_schema(self, role_name, schema_name, privileges):
        _validate_privileges(privileges, "revoke-schema")
        query = sql.SQL("REVOKE {} ON SCHEMA {} FROM {}").format(
            sql.SQL(privileges),
            sql.Identifier(schema_name),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 已回收角色 [cyan]{role_name}[/cyan] 在 Schema [yellow]{schema_name}[/yellow] 的 [bold]{privileges}[/bold] 权限")

    def _get_tables_by_pattern(self, schema_name, table_pattern):
        query = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
        """
        rows = self._execute(query, (schema_name,), fetch=True)
        if not rows:
            return []
        pattern = re.compile(table_pattern)
        return [row[0] for row in rows if pattern.match(row[0])]

    def priv_grant_table(self, role_name, schema_name, table_name, privileges):
        _validate_privileges(privileges, "grant-table")
        query = sql.SQL("GRANT {} ON TABLE {}.{} TO {}").format(
            sql.SQL(privileges),
            sql.Identifier(schema_name),
            sql.Identifier(table_name),
            sql.Identifier(role_name),
        )
        result = self._execute(query)
        if result and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] GRANT {privileges} ON TABLE {schema_name}.{table_name} TO {role_name}")

    def priv_grant_tables_by_regex(self, role_name, schema_name, table_pattern, privileges):
        _validate_privileges(privileges, "grant-table-regex")
        if self.dry_run:
            console.print(f"[dim]-- 将对 Schema {schema_name} 下匹配正则 '{table_pattern}' 的表授予 {privileges} 权限[/dim]")
            console.print("[dim]-- 注意：Dry-run模式下无法枚举实际表名，请执行后查看效果[/dim]")
            return

        tables = self._get_tables_by_pattern(schema_name, table_pattern)
        if not tables:
            console.print(f"[bold yellow][WARN][/bold yellow] Schema [yellow]{schema_name}[/yellow] 下没有匹配正则 [magenta]'{table_pattern}'[/magenta] 的表")
            return

        preview = ", ".join(tables[:5]) + ("..." if len(tables) > 5 else "")
        console.print(f"匹配到 [bold]{len(tables)}[/bold] 张表: {preview}")
        for table in tables:
            self.priv_grant_table(role_name, schema_name, table, privileges)
        console.print(f"[bold green][OK][/bold green] 共授权 [bold]{len(tables)}[/bold] 张表")

    def priv_revoke_table(self, role_name, schema_name, table_name, privileges):
        _validate_privileges(privileges, "revoke-table")
        query = sql.SQL("REVOKE {} ON TABLE {}.{} FROM {}").format(
            sql.SQL(privileges),
            sql.Identifier(schema_name),
            sql.Identifier(table_name),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 已回收角色 [cyan]{role_name}[/cyan] 在表 [yellow]{schema_name}.{table_name}[/yellow] 的 [bold]{privileges}[/bold] 权限")

    def priv_revoke_tables_by_regex(self, role_name, schema_name, table_pattern, privileges):
        _validate_privileges(privileges, "revoke-table-regex")
        if self.dry_run:
            console.print(f"[dim]-- 将对 Schema {schema_name} 下匹配正则 '{table_pattern}' 的表回收 {privileges} 权限[/dim]")
            console.print("[dim]-- 注意：Dry-run模式下无法枚举实际表名，请执行后查看效果[/dim]")
            return

        tables = self._get_tables_by_pattern(schema_name, table_pattern)
        if not tables:
            console.print(f"[bold yellow][WARN][/bold yellow] Schema [yellow]{schema_name}[/yellow] 下没有匹配正则 [magenta]'{table_pattern}'[/magenta] 的表")
            return

        preview = ", ".join(tables[:5]) + ("..." if len(tables) > 5 else "")
        console.print(f"匹配到 [bold]{len(tables)}[/bold] 张表: {preview}")
        for table in tables:
            self.priv_revoke_table(role_name, schema_name, table, privileges)
        console.print(f"[bold green][OK][/bold green] 共回收 [bold]{len(tables)}[/bold] 张表的权限")

    def priv_grant_all_tables(self, role_name, schema_name, privileges):
        _validate_privileges(privileges, "grant-all-tables")
        query = sql.SQL("GRANT {} ON ALL TABLES IN SCHEMA {} TO {}").format(
            sql.SQL(privileges),
            sql.Identifier(schema_name),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 角色 [cyan]{role_name}[/cyan] 已获得 Schema [yellow]{schema_name}[/yellow] 下所有表的 [bold]{privileges}[/bold] 权限")

    def priv_revoke_all_tables(self, role_name, schema_name, privileges):
        _validate_privileges(privileges, "revoke-all-tables")
        query = sql.SQL("REVOKE {} ON ALL TABLES IN SCHEMA {} FROM {}").format(
            sql.SQL(privileges),
            sql.Identifier(schema_name),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 已回收角色 [cyan]{role_name}[/cyan] 在 Schema [yellow]{schema_name}[/yellow] 下所有表的 [bold]{privileges}[/bold] 权限")

    def priv_set_default(self, role_name, schema_name, privileges, target_role=None, obj_type="TABLES"):
        _validate_privileges(privileges, "set-default")
        obj_type = _validate_obj_type(obj_type)
        target = sql.Identifier(target_role) if target_role else sql.SQL("CURRENT_USER")
        query = sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT {} ON {} TO {}"
        ).format(
            target,
            sql.Identifier(schema_name),
            sql.SQL(privileges),
            sql.SQL(obj_type),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 已设置 Schema [yellow]{schema_name}[/yellow] 下新建{obj_type}的默认权限")

    def priv_revoke_default(self, role_name, schema_name, privileges, target_role=None, obj_type="TABLES"):
        _validate_privileges(privileges, "revoke-default")
        obj_type = _validate_obj_type(obj_type)
        target = sql.Identifier(target_role) if target_role else sql.SQL("CURRENT_USER")
        query = sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} REVOKE {} ON {} FROM {}"
        ).format(
            target,
            sql.Identifier(schema_name),
            sql.SQL(privileges),
            sql.SQL(obj_type),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 已回收 Schema [yellow]{schema_name}[/yellow] 下新建{obj_type}的默认权限")

    # --- 序列权限 ---
    def priv_grant_all_sequences(self, role_name, schema_name, privileges):
        _validate_privileges(privileges, "grant-all-sequences")
        query = sql.SQL("GRANT {} ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
            sql.SQL(privileges),
            sql.Identifier(schema_name),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 角色 [cyan]{role_name}[/cyan] 已获得 Schema [yellow]{schema_name}[/yellow] 下所有序列的 [bold]{privileges}[/bold] 权限")

    def priv_revoke_all_sequences(self, role_name, schema_name, privileges):
        _validate_privileges(privileges, "revoke-all-sequences")
        query = sql.SQL("REVOKE {} ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(
            sql.SQL(privileges),
            sql.Identifier(schema_name),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 已回收角色 [cyan]{role_name}[/cyan] 在 Schema [yellow]{schema_name}[/yellow] 下所有序列的 [bold]{privileges}[/bold] 权限")

    # --- 函数权限 ---
    def priv_grant_all_functions(self, role_name, schema_name, privileges):
        _validate_privileges(privileges, "grant-all-functions")
        query = sql.SQL("GRANT {} ON ALL FUNCTIONS IN SCHEMA {} TO {}").format(
            sql.SQL(privileges),
            sql.Identifier(schema_name),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 角色 [cyan]{role_name}[/cyan] 已获得 Schema [yellow]{schema_name}[/yellow] 下所有函数的 [bold]{privileges}[/bold] 权限")

    def priv_revoke_all_functions(self, role_name, schema_name, privileges):
        _validate_privileges(privileges, "revoke-all-functions")
        query = sql.SQL("REVOKE {} ON ALL FUNCTIONS IN SCHEMA {} FROM {}").format(
            sql.SQL(privileges),
            sql.Identifier(schema_name),
            sql.Identifier(role_name),
        )
        if self._execute(query) and not self.dry_run:
            console.print(f"[bold green][OK][/bold green] 已回收角色 [cyan]{role_name}[/cyan] 在 Schema [yellow]{schema_name}[/yellow] 下所有函数的 [bold]{privileges}[/bold] 权限")

    # --- 类型权限（枚举/复合/域/范围） ---
    def priv_grant_all_types(self, role_name, schema_name, privileges):
        _validate_privileges(privileges, "grant-all-types")
        """授权Schema下所有类型的权限（PG 不支持 ON ALL TYPES IN SCHEMA 语法，需逐个授权）"""
        if self.dry_run:
            q = sql.SQL("GRANT {} ON TYPE {} TO {}").format(
                sql.SQL(privileges),
                sql.Identifier("<type_name>"),
                sql.Identifier(role_name),
            )
            renderer = SQLRenderer()
            console.print(f"[dim]-- SQL: {renderer.render(q)}[/dim]")
            console.print("[dim]-- 注：PG 不支持 GRANT ... ON ALL TYPES IN SCHEMA，实际执行时逐个类型授权[/dim]")
            return

        type_query = """
            SELECT t.typname
            FROM pg_type t
            JOIN pg_namespace n ON t.typnamespace = n.oid
            WHERE n.nspname = %s
              AND t.typtype IN ('d', 'c', 'e')
            ORDER BY t.typname
        """
        types = self._execute(type_query, (schema_name,), fetch=True)
        if not types:
            console.print(f"[dim]Schema [yellow]{schema_name}[/yellow] 下无可授权的用户自定义类型[/dim]")
            return

        for tp in types:
            q = sql.SQL("GRANT {} ON TYPE {} TO {}").format(
                sql.SQL(privileges),
                sql.Identifier(tp[0]),
                sql.Identifier(role_name),
            )
            self._execute(q)
        console.print(f"[bold green][OK][/bold green] 角色 [cyan]{role_name}[/cyan] 已获得 Schema [yellow]{schema_name}[/yellow] 下 {len(types)} 个类型的 [bold]{privileges}[/bold] 权限")

    def priv_revoke_all_types(self, role_name, schema_name, privileges):
        _validate_privileges(privileges, "revoke-all-types")
        """回收Schema下所有类型的权限（PG 不支持 ON ALL TYPES IN SCHEMA 语法，需逐个回收）"""
        if self.dry_run:
            q = sql.SQL("REVOKE {} ON TYPE {} FROM {}").format(
                sql.SQL(privileges),
                sql.Identifier("<type_name>"),
                sql.Identifier(role_name),
            )
            renderer = SQLRenderer()
            console.print(f"[dim]-- SQL: {renderer.render(q)}[/dim]")
            console.print("[dim]-- 注：PG 不支持 REVOKE ... ON ALL TYPES IN SCHEMA，实际执行时逐个类型回收[/dim]")
            return

        type_query = """
            SELECT t.typname
            FROM pg_type t
            JOIN pg_namespace n ON t.typnamespace = n.oid
            WHERE n.nspname = %s
              AND t.typtype IN ('d', 'c', 'e')
            ORDER BY t.typname
        """
        types = self._execute(type_query, (schema_name,), fetch=True)
        if not types:
            console.print(f"[dim]Schema [yellow]{schema_name}[/yellow] 下无可回收的用户自定义类型[/dim]")
            return

        for tp in types:
            q = sql.SQL("REVOKE {} ON TYPE {} FROM {}").format(
                sql.SQL(privileges),
                sql.Identifier(tp[0]),
                sql.Identifier(role_name),
            )
            self._execute(q)
        console.print(f"[bold green][OK][/bold green] 已回收角色 [cyan]{role_name}[/cyan] 在 Schema [yellow]{schema_name}[/yellow] 下 {len(types)} 个类型的 [bold]{privileges}[/bold] 权限")

    # --- 语言权限（PL等过程语言） ---
    def priv_grant_all_languages(self, role_name, privileges):
        _validate_privileges(privileges, "grant-all-languages")
        """授权所有过程语言的权限（语言是数据库级对象，不依赖Schema，不支持 ON ALL LANGUAGES 语法）"""
        if self.dry_run:
            # dry-run 模式下无法查询数据库，生成代表性 SQL
            q = sql.SQL("GRANT {} ON LANGUAGE {} TO {}").format(
                sql.SQL(privileges),
                sql.Identifier("<language_name>"),
                sql.Identifier(role_name),
            )
            renderer = SQLRenderer()
            console.print(f"[dim]-- SQL: {renderer.render(q)}[/dim]")
            console.print("[dim]-- 注：PG 不支持 GRANT ... ON ALL LANGUAGES，实际执行时逐个语言授权（如 plpgsql、plpython3u 等）[/dim]")
            return

        lang_query = """
            SELECT lanname FROM pg_language
            WHERE lanispl = true
            ORDER BY lanname
        """
        langs = self._execute(lang_query, fetch=True)
        if not langs:
            console.print("[dim]无可授权的用户自定义过程语言[/dim]")
            return

        for lang in langs:
            q = sql.SQL("GRANT {} ON LANGUAGE {} TO {}").format(
                sql.SQL(privileges),
                sql.Identifier(lang[0]),
                sql.Identifier(role_name),
            )
            self._execute(q)
        console.print(f"[bold green][OK][/bold green] 角色 [cyan]{role_name}[/cyan] 已获得 {len(langs)} 种过程语言的 [bold]{privileges}[/bold] 权限")

    def priv_revoke_all_languages(self, role_name, privileges):
        _validate_privileges(privileges, "revoke-all-languages")
        """回收所有过程语言的权限"""
        if self.dry_run:
            q = sql.SQL("REVOKE {} ON LANGUAGE {} FROM {}").format(
                sql.SQL(privileges),
                sql.Identifier("<language_name>"),
                sql.Identifier(role_name),
            )
            renderer = SQLRenderer()
            console.print(f"[dim]-- SQL: {renderer.render(q)}[/dim]")
            console.print("[dim]-- 注：PG 不支持 REVOKE ... ON ALL LANGUAGES，实际执行时逐个语言回收[/dim]")
            return

        lang_query = """
            SELECT lanname FROM pg_language
            WHERE lanispl = true
            ORDER BY lanname
        """
        langs = self._execute(lang_query, fetch=True)
        if not langs:
            console.print("[dim]无可回收的用户自定义过程语言[/dim]")
            return

        for lang in langs:
            q = sql.SQL("REVOKE {} ON LANGUAGE {} FROM {}").format(
                sql.SQL(privileges),
                sql.Identifier(lang[0]),
                sql.Identifier(role_name),
            )
            self._execute(q)
        console.print(f"[bold green][OK][/bold green] 已回收角色 [cyan]{role_name}[/cyan] 在 {len(langs)} 种过程语言的 [bold]{privileges}[/bold] 权限")

    # ============================================================
    # 第四部分：权限查询（级联）
    # ============================================================
    def _get_role_hierarchy(self, principal):
        query = """
            WITH RECURSIVE role_hierarchy AS (
                SELECT
                    r.oid,
                    r.rolname,
                    r.rolcanlogin,
                    r.rolsuper,
                    r.rolinherit
                FROM pg_roles r
                WHERE r.rolname = %s

                UNION

                SELECT
                    r.oid,
                    r.rolname,
                    r.rolcanlogin,
                    r.rolsuper,
                    r.rolinherit
                FROM pg_roles r
                JOIN pg_auth_members m ON m.roleid = r.oid
                JOIN role_hierarchy rh ON rh.oid = m.member
            )
            SELECT DISTINCT
                rolname, oid, rolcanlogin, rolsuper, rolinherit
            FROM role_hierarchy
            ORDER BY rolname
        """
        rows = self._execute(query, (principal,), fetch=True)
        return rows or []

    def _print_principal_header(self, principal, role_rows):
        if not role_rows:
            console.print(f"[bold red][ERROR] 角色/用户不存在: {principal}[/bold red]")
            return False

        root = next((r for r in role_rows if r[0] == principal), role_rows[0])
        inherited = [r for r in role_rows if r[0] != principal]
        is_super = bool(root[3])

        info_lines = [
            f"[cyan]主体[/cyan]:    [bold]{principal}[/bold]",
            f"[cyan]类型[/cyan]:    {'用户(可登录)' if root[2] else '角色(权限组)'}",
            f"[cyan]超级用户[/cyan]: {'[bold red]是[/bold red]' if is_super else '否'}",
            f"[cyan]继承权限[/cyan]: {'是' if root[4] else '否'}",
        ]
        console.print(Panel("\n".join(info_lines), title="主体信息", border_style="cyan", padding=(0, 2)))

        # 角色链表格
        chain_table = Table(title="有效角色链（包含主体自身）", show_lines=False, border_style="blue")
        chain_table.add_column("角色/用户", style="cyan")
        chain_table.add_column("类型", style="white")
        chain_table.add_column("标记", style="bold red")

        for row in role_rows:
            kind = "用户" if row[2] else "角色"
            marker = "SUPERUSER" if row[3] else ""
            chain_table.add_row(row[0], kind, marker)

        console.print(chain_table)

        if inherited:
            console.print(f"\n[dim]直接/间接继承角色数量: {len(inherited)}[/dim]")
        else:
            console.print("\n[dim]未继承其他角色[/dim]")

        if is_super:
            console.print(Panel(
                "当前主体是 PostgreSQL SUPERUSER。\n"
                "超级用户拥有数据库、Schema、表等对象的隐含完整权限，\n"
                "这些权限不会作为普通 GRANT ACL 出现在权限表中。",
                title="[bold red]SUPERUSER 提示[/bold red]",
                border_style="red",
                padding=(0, 2),
            ))

        return is_super

    def _query_database_permissions(self, role_names):
        query = """
            SELECT
                d.datname,
                p.privilege_type,
                COALESCE(r.rolname, 'PUBLIC') AS grantee
            FROM pg_database d
            CROSS JOIN LATERAL aclexplode(
                COALESCE(
                    d.datacl,
                    acldefault('d', d.datdba)
                )
            ) p
            JOIN pg_roles r ON r.oid = p.grantee
            WHERE r.rolname = ANY(%s)
            ORDER BY d.datname, r.rolname, p.privilege_type
        """
        return self._execute(query, (role_names,), fetch=True) or []

    def _query_schema_permissions(self, role_names):
        query = """
            SELECT
                n.nspname,
                p.privilege_type,
                r.rolname AS grantee
            FROM pg_namespace n
            CROSS JOIN LATERAL aclexplode(
                COALESCE(
                    n.nspacl,
                    acldefault('n', n.nspowner)
                )
            ) p
            JOIN pg_roles r ON r.oid = p.grantee
            WHERE r.rolname = ANY(%s)
            ORDER BY n.nspname, r.rolname, p.privilege_type
        """
        return self._execute(query, (role_names,), fetch=True) or []

    def _query_table_permissions(self, role_names, table_filter=None):
        query = """
            SELECT
                n.nspname AS table_schema,
                c.relname AS table_name,
                CASE c.relkind
                    WHEN 'r' THEN 'TABLE'
                    WHEN 'p' THEN 'PARTITIONED TABLE'
                    WHEN 'v' THEN 'VIEW'
                    WHEN 'm' THEN 'MATERIALIZED VIEW'
                    WHEN 'f' THEN 'FOREIGN TABLE'
                    ELSE c.relkind
                END AS obj_kind,
                p.privilege_type,
                r.rolname AS grantee
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL aclexplode(
                COALESCE(
                    c.relacl,
                    acldefault(
                        CASE
                            WHEN c.relkind = 'S' THEN 'S'::"char"
                            ELSE 'r'::"char"
                        END,
                        c.relowner
                    )
                )
            ) p
            LEFT JOIN pg_roles r ON r.oid = p.grantee
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND (
                    r.rolname = ANY(%s)
                    OR p.grantee = 0
                  )
              AND (c.relname LIKE %s OR %s IS NULL)
            ORDER BY n.nspname, c.relname, p.privilege_type, COALESCE(r.rolname, 'PUBLIC')
        """
        return self._execute(query, (role_names, table_filter, table_filter), fetch=True) or []

    def _query_sequence_permissions(self, role_names, table_filter=None):
        query = """
            SELECT
                n.nspname AS seq_schema,
                c.relname AS seq_name,
                p.privilege_type,
                COALESCE(r.rolname, 'PUBLIC') AS grantee
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL aclexplode(
                COALESCE(
                    c.relacl,
                    acldefault('S'::"char", c.relowner)
                )
            ) p
            LEFT JOIN pg_roles r ON r.oid = p.grantee
            WHERE c.relkind = 'S'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND (
                    r.rolname = ANY(%s)
                    OR p.grantee = 0
                  )
            ORDER BY n.nspname, c.relname, p.privilege_type, COALESCE(r.rolname, 'PUBLIC')
        """
        return self._execute(query, (role_names,), fetch=True) or []

    def _query_function_permissions(self, role_names):
        query = """
            SELECT
                n.nspname AS func_schema,
                p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' AS func_name,
                p2.privilege_type,
                COALESCE(r.rolname, 'PUBLIC') AS grantee
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            CROSS JOIN LATERAL aclexplode(
                COALESCE(
                    p.proacl,
                    acldefault('f', p.proowner)
                )
            ) p2
            LEFT JOIN pg_roles r ON r.oid = p2.grantee
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND (
                    r.rolname = ANY(%s)
                    OR p2.grantee = 0
                  )
            ORDER BY n.nspname, p.proname, p2.privilege_type, COALESCE(r.rolname, 'PUBLIC')
        """
        return self._execute(query, (role_names,), fetch=True) or []

    def _query_type_permissions(self, role_names):
        query = """
            SELECT
                n.nspname AS type_schema,
                t.typname AS type_name,
                p.privilege_type,
                COALESCE(r.rolname, 'PUBLIC') AS grantee
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            CROSS JOIN LATERAL aclexplode(
                COALESCE(
                    t.typacl,
                    acldefault('T', t.typowner)
                )
            ) p
            LEFT JOIN pg_roles r ON r.oid = p.grantee
            WHERE t.typtype IN ('e', 'c', 'd', 'r')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND (
                    r.rolname = ANY(%s)
                    OR p.grantee = 0
                  )
            ORDER BY n.nspname, t.typname, p.privilege_type, COALESCE(r.rolname, 'PUBLIC')
        """
        return self._execute(query, (role_names,), fetch=True) or []

    def _query_language_permissions(self, role_names):
        query = """
            SELECT
                l.lanname AS lang_name,
                p.privilege_type,
                COALESCE(r.rolname, 'PUBLIC') AS grantee
            FROM pg_language l
            CROSS JOIN LATERAL aclexplode(
                COALESCE(
                    l.lanacl,
                    acldefault('l', l.lanowner)
                )
            ) p
            LEFT JOIN pg_roles r ON r.oid = p.grantee
            WHERE l.lanispl = true
              AND (
                    r.rolname = ANY(%s)
                    OR p.grantee = 0
                  )
            ORDER BY l.lanname, p.privilege_type, COALESCE(r.rolname, 'PUBLIC')
        """
        return self._execute(query, (role_names,), fetch=True) or []

    def _query_default_privileges(self, role_names):
        query = """
            SELECT
                n.nspname AS schema_name,
                CASE d.defaclobjtype
                    WHEN 'r' THEN 'TABLES'
                    WHEN 'S' THEN 'SEQUENCES'
                    WHEN 'f' THEN 'FUNCTIONS'
                    WHEN 'T' THEN 'TYPES'
                    ELSE d.defaclobjtype
                END AS obj_type,
                p.privilege_type,
                COALESCE(gr.rolname, 'PUBLIC') AS grantee,
                COALESCE(tr.rolname, 'CURRENT_USER') AS target_role
            FROM pg_default_acl d
            JOIN pg_namespace n ON n.oid = d.defaclnamespace
            CROSS JOIN LATERAL aclexplode(d.defaclacl) p
            LEFT JOIN pg_roles gr ON gr.oid = p.grantee
            LEFT JOIN pg_roles tr ON tr.oid = d.defaclrole
            WHERE (
                    gr.rolname = ANY(%s)
                    OR p.grantee = 0
                  )
            ORDER BY n.nspname, d.defaclobjtype, p.privilege_type, COALESCE(gr.rolname, 'PUBLIC')
        """
        return self._execute(query, (role_names,), fetch=True) or []

    def _print_permission_rows(self, rows, title, headers):
        if not rows:
            console.print(Panel(f"[dim]{title}[/dim]\n\n  无显式 ACL 权限记录", border_style="dim", padding=(0, 2)))
            return

        # 合并同一对象的权限（按非权限列分组，权限列用逗号拼接）
        perm_idx = headers.index("权限") if "权限" in headers else -1
        if perm_idx >= 0:
            merged = OrderedDict()
            for row in rows:
                key = tuple(v for i, v in enumerate(row) if i != perm_idx)
                if key not in merged:
                    merged[key] = []
                merged[key].append(str(row[perm_idx]))
            rows = [key[:perm_idx] + (','.join(merged[key]),) + key[perm_idx:] for key in merged]

        table = Table(title=title, show_lines=False, border_style="blue", padding=(0, 1))
        for header in headers:
            table.add_column(header, style="white")

        for row in rows:
            str_row = [str(v) for v in row]
            styled_row = []
            for i, val in enumerate(str_row):
                if headers[i] == "权限":
                    perms = val.split(',')
                    styled_perms = []
                    for p in perms:
                        p = p.strip()
                        if p in (
                            "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE",
                            "USAGE", "CREATE", "CONNECT", "TEMPORARY",
                            "REFERENCES", "TRIGGER", "EXECUTE",
                        ):
                            styled_perms.append(f"[bold green]{p}[/bold green]")
                        else:
                            styled_perms.append(p)
                    styled_row.append(','.join(styled_perms))
                else:
                    styled_row.append(val)
            table.add_row(*styled_row)

        console.print(table)

    def _print_superuser_effective_permissions(self, principal):
        db_query = """
            SELECT datname
            FROM pg_database
            WHERE datallowconn = true
            ORDER BY datname
        """
        databases = self._execute(db_query, fetch=True) or []

        schema_query = """
            SELECT nspname
            FROM pg_namespace
            WHERE nspname NOT LIKE 'pg_toast%'
              AND nspname <> 'information_schema'
            ORDER BY nspname
        """
        schemas = self._execute(schema_query, fetch=True) or []

        perm_table = Table(title=f"{principal} 的 SUPERUSER 有效权限", show_lines=False, border_style="red")
        perm_table.add_column("层级", style="cyan")
        perm_table.add_column("有效权限", style="bold green")
        perm_table.add_row("DATABASE", "全部数据库（受连接条件等系统配置限制）")
        perm_table.add_row("SCHEMA", "全部 Schema")
        perm_table.add_row("TABLE", "所有表/视图/物化视图等对象")
        perm_table.add_row("DML", "SELECT, INSERT, UPDATE, DELETE, TRUNCATE")
        perm_table.add_row("DDL", "CREATE, ALTER, DROP 等对象管理权限")
        perm_table.add_row("其他", "REFERENCES, TRIGGER 等")
        console.print(perm_table)

        db_list = ", ".join(row[0] for row in databases) if databases else "[dim]无[/dim]"
        schema_list = ", ".join(row[0] for row in schemas) if schemas else "[dim]无[/dim]"
        console.print(Panel(
            f"[cyan]可见数据库:[/cyan]\n  {db_list}\n\n[cyan]当前实例可见 Schema:[/cyan]\n  {schema_list}",
            title="SUPERUSER 可见对象",
            border_style="red",
            padding=(0, 2),
        ))

    def query_principal_permissions(self, principal, table_filter=None, verbose=False):
        role_rows = self._get_role_hierarchy(principal)
        if not role_rows:
            console.print(f"[bold red][ERROR] 角色/用户不存在: {principal}[/bold red]")
            return

        is_super = self._print_principal_header(principal, role_rows)

        role_names = [r[0] for r in role_rows]

        if is_super:
            self._print_superuser_effective_permissions(principal)

        db_privs = self._query_database_permissions(role_names)
        self._print_permission_rows(
            db_privs,
            "数据库级显式 ACL 权限",
            ("数据库", "权限", "授权给"),
        )

        schema_privs = self._query_schema_permissions(role_names)
        self._print_permission_rows(
            schema_privs,
            "Schema 级显式 ACL 权限",
            ("Schema", "权限", "授权给"),
        )

        table_privs = self._query_table_permissions(role_names, table_filter)
        self._print_permission_rows(
            table_privs,
            "表/视图级显式 ACL 权限",
            ("Schema", "对象名", "类型", "权限", "授权给"),
        )

        if verbose:
            seq_privs = self._query_sequence_permissions(role_names)
            self._print_permission_rows(
                seq_privs,
                "序列级显式 ACL 权限",
                ("Schema", "序列名", "权限", "授权给"),
            )

            func_privs = self._query_function_permissions(role_names)
            self._print_permission_rows(
                func_privs,
                "函数级显式 ACL 权限",
                ("Schema", "函数名", "权限", "授权给"),
            )

            type_privs = self._query_type_permissions(role_names)
            self._print_permission_rows(
                type_privs,
                "类型级显式 ACL 权限",
                ("Schema", "类型名", "权限", "授权给"),
            )

            lang_privs = self._query_language_permissions(role_names)
            self._print_permission_rows(
                lang_privs,
                "语言级显式 ACL 权限",
                ("语言名", "权限", "授权给"),
            )

            default_privs = self._query_default_privileges(role_names)
            self._print_permission_rows(
                default_privs,
                "默认权限 (ALTER DEFAULT PRIVILEGES)",
                ("Schema", "对象类型", "权限", "授权给", "建表者"),
            )

        console.print(Panel(
            "1. [cyan]显式 ACL 权限[/cyan]表示 PostgreSQL ACL 中实际存在的 GRANT（包括 PUBLIC ACL）。\n"
            "2. 主体直接 GRANT 的权限也会统计，不再只看继承角色。\n"
            "3. 角色继承会递归展开，例如 user -> role_a -> role_b。\n"
            "4. SUPERUSER 的权限属于系统隐含权限，不依赖 ACL。\n"
            "5. 类型列: T=TABLE(普通表), V=VIEW(视图), M=MATERIALIZED VIEW(物化视图), P=PARTITIONED TABLE(分区表), F=FOREIGN TABLE(外部表)",
            title="查询说明",
            border_style="dim",
            padding=(0, 2),
        ))

    def query_user_permissions(self, username, table_filter=None, verbose=False):
        self.query_principal_permissions(username, table_filter, verbose)

    def query_role_permissions(self, role_name, table_filter=None, verbose=False):
        self.query_principal_permissions(role_name, table_filter, verbose)

    # ============================================================
    # 第五部分：模板（一键配置）
    # ============================================================
    def _get_user_schemas(self):
        """获取所有用户Schema（排除系统Schema）"""
        query = """
            SELECT nspname
            FROM pg_namespace
            WHERE nspname NOT LIKE 'pg_%%'
              AND nspname != 'information_schema'
            ORDER BY nspname
        """
        rows = self._execute(query, fetch=True)
        return [row[0] for row in rows] if rows else []

    def template_backup(self, username, password, dbname, owner_role=None):
        """备份账号模板：自动授权该库所有Schema的SELECT(表+序列)+USAGE(序列)"""
        self._switch_db(dbname)
        role_name = f"r_{username}_backup"

        if self.dry_run:
            schemas = ["<all_user_schemas>"]
        else:
            schemas = self._get_user_schemas()

        console.print(Panel(
            f"[cyan]用户名[/cyan]: {username}\n"
            f"[cyan]角色名[/cyan]: {role_name}\n"
            f"[cyan]数据库[/cyan]: {dbname}\n"
            f"[cyan]Schema[/cyan]: {', '.join(schemas)}\n"
            f"[cyan]建表者[/cyan]: {owner_role}\n"
            f"[cyan]权限[/cyan]: CONNECT, USAGE(所有Schema), SELECT(表), SELECT+USAGE(序列)",
            title="备份账号模板", border_style="yellow", padding=(0, 2),
        ))

        console.print(Rule("创建角色和用户", style="blue"))
        self.role_create(role_name, comment="备份只读角色")
        self.user_create(username, password, comment="备份账号")
        self.user_grant_role(username, role_name)

        console.print(Rule("授权数据库", style="blue"))
        self.priv_grant_database(role_name, dbname, "CONNECT")

        for schema in schemas:
            console.print(Rule(f"授权 Schema: {schema}", style="blue"))
            self.priv_grant_schema(role_name, schema, "USAGE")
            self.priv_grant_all_tables(role_name, schema, "SELECT")
            self.priv_grant_all_sequences(role_name, schema, "SELECT,USAGE")
            self.priv_set_default(role_name, schema, "SELECT", owner_role, "TABLES")
            self.priv_set_default(role_name, schema, "SELECT,USAGE", owner_role, "SEQUENCES")

        if not self.dry_run:
            console.print(Panel(
                f"[bold green]备份账号创建完成！[/bold green]\n\n"
                f"已授权 Schema: {', '.join(schemas)}\n\n"
                f"使用方式：\n"
                f"  pg_dump -U {username} -d {dbname} > backup.sql\n"
                f"  pg_dumpall -U {username} > cluster_backup.sql\n\n"
                f"[yellow]注意:[/yellow]\n"
                f"  1. 默认权限只对 {owner_role} 创建的新对象生效\n"
                f"     若有其他角色也会建表，需额外执行 set-default 指定其他建表者\n"
                f"  2. 已存在的老表已通过 GRANT ALL TABLES 授权\n"
                f"     未来新建的表走默认权限自动授权",
                border_style="green", padding=(0, 2),
            ))

    def template_readonly(self, username, password, dbname, schema_name, owner_role=None):
        """只读用户模板：CONNECT + USAGE + SELECT(表+序列)"""
        self._switch_db(dbname)
        role_name = f"r_{username}_readonly"
        console.print(Panel(
            f"[cyan]用户名[/cyan]: {username}\n"
            f"[cyan]角色名[/cyan]: {role_name}\n"
            f"[cyan]数据库[/cyan]: {dbname}\n"
            f"[cyan]Schema[/cyan]: {schema_name}\n"
            f"[cyan]建表者[/cyan]: {owner_role}\n"
            f"[cyan]权限[/cyan]: CONNECT, USAGE, SELECT(表+序列)",
            title="只读用户模板", border_style="yellow", padding=(0, 2),
        ))

        console.print(Rule("创建角色和用户", style="blue"))
        self.role_create(role_name, comment="只读角色")
        self.user_create(username, password, comment="只读用户")
        self.user_grant_role(username, role_name)

        console.print(Rule("授权数据库和Schema", style="blue"))
        self.priv_grant_database(role_name, dbname, "CONNECT")
        self.priv_grant_schema(role_name, schema_name, "USAGE")

        console.print(Rule("授权对象权限", style="blue"))
        self.priv_grant_all_tables(role_name, schema_name, "SELECT")
        self.priv_grant_all_sequences(role_name, schema_name, "SELECT")

        console.print(Rule("设置默认权限（未来新建对象）", style="blue"))
        self.priv_set_default(role_name, schema_name, "SELECT", owner_role, "TABLES")
        self.priv_set_default(role_name, schema_name, "SELECT", owner_role, "SEQUENCES")

        if not self.dry_run:
            console.print(Panel(
                f"[bold green]只读用户创建完成！[/bold green]\n\n"
                f"用户 [cyan]{username}[/cyan] 只能执行 SELECT 查询\n\n"
                f"[yellow]注意:[/yellow]\n"
                f"  1. 默认权限只对 {owner_role} 创建的新对象生效\n"
                f"     若有其他角色也会建表，需额外执行 set-default 指定其他建表者\n"
                f"  2. 已存在的老表已通过 GRANT ALL TABLES 授权\n"
                f"     未来新建的表走默认权限自动授权",
                border_style="green", padding=(0, 2),
            ))

    def template_readwrite(self, username, password, dbname, schema_name, owner_role=None):
        """读写用户模板：CONNECT+TEMPORARY, USAGE+CREATE(Schema), ALL PRIVILEGES(表+序列+函数+类型), USAGE(语言)"""
        self._switch_db(dbname)
        role_name = f"r_{username}_readwrite"
        console.print(Panel(
            f"[cyan]用户名[/cyan]: {username}\n"
            f"[cyan]角色名[/cyan]: {role_name}\n"
            f"[cyan]数据库[/cyan]: {dbname}\n"
            f"[cyan]Schema[/cyan]: {schema_name}\n"
            f"[cyan]建表者[/cyan]: {owner_role}\n"
            f"[cyan]权限[/cyan]: CONNECT+TEMPORARY, USAGE+CREATE(Schema), ALL PRIVILEGES(表+序列+函数+类型), USAGE(语言)",
            title="读写用户模板", border_style="yellow", padding=(0, 2),
        ))

        console.print(Rule("创建角色和用户", style="blue"))
        self.role_create(role_name, comment="读写角色")
        self.user_create(username, password, comment="读写用户")
        self.user_grant_role(username, role_name)

        console.print(Rule("授权数据库和Schema", style="blue"))
        self.priv_grant_database(role_name, dbname, "CONNECT,TEMPORARY")
        self.priv_grant_schema(role_name, schema_name, "USAGE,CREATE")

        console.print(Rule("授权对象权限", style="blue"))
        self.priv_grant_all_tables(role_name, schema_name, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER")
        self.priv_grant_all_sequences(role_name, schema_name, "USAGE,SELECT,UPDATE")
        self.priv_grant_all_functions(role_name, schema_name, "EXECUTE")
        self.priv_grant_all_types(role_name, schema_name, "USAGE")
        self.priv_grant_all_languages(role_name, "USAGE")

        console.print(Rule("设置默认权限（未来新建对象）", style="blue"))
        self.priv_set_default(role_name, schema_name, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER", owner_role, "TABLES")
        self.priv_set_default(role_name, schema_name, "USAGE,SELECT,UPDATE", owner_role, "SEQUENCES")
        self.priv_set_default(role_name, schema_name, "EXECUTE", owner_role, "FUNCTIONS")
        self.priv_set_default(role_name, schema_name, "USAGE", owner_role, "TYPES")

        if not self.dry_run:
            console.print(Panel(
                f"[bold green]读写用户创建完成！[/bold green]\n\n"
                f"用户 [cyan]{username}[/cyan] 拥有全部表权限(含TRUNCATE/TRIGGER)和Schema创建权限\n\n"
                f"[yellow]注意:[/yellow]\n"
                f"  1. 默认权限只对 {owner_role} 创建的新对象生效\n"
                f"     若有其他角色也会建表，需额外执行 set-default 指定其他建表者\n"
                f"  2. 已存在的老表已通过 GRANT ALL TABLES 授权\n"
                f"     未来新建的表走默认权限自动授权",
                border_style="green", padding=(0, 2),
            ))

    def template_dml(self, username, password, dbname, schema_name, owner_role=None):
        """DML应用写入模板：CONNECT, USAGE, SELECT+INSERT+UPDATE+DELETE(表), USAGE+SELECT+UPDATE(序列), EXECUTE(函数), USAGE(类型)"""
        self._switch_db(dbname)
        role_name = f"r_{username}_dml"
        console.print(Panel(
            f"[cyan]用户名[/cyan]: {username}\n"
            f"[cyan]角色名[/cyan]: {role_name}\n"
            f"[cyan]数据库[/cyan]: {dbname}\n"
            f"[cyan]Schema[/cyan]: {schema_name}\n"
            f"[cyan]建表者[/cyan]: {owner_role}\n"
            f"[cyan]权限[/cyan]: CONNECT, USAGE, SELECT+INSERT+UPDATE+DELETE(表), USAGE+SELECT+UPDATE(序列), EXECUTE(函数), USAGE(类型)",
            title="DML应用写入模板", border_style="yellow", padding=(0, 2),
        ))

        console.print(Rule("创建角色和用户", style="blue"))
        self.role_create(role_name, comment="DML写入角色")
        self.user_create(username, password, comment="DML应用用户")
        self.user_grant_role(username, role_name)

        console.print(Rule("授权数据库和Schema", style="blue"))
        self.priv_grant_database(role_name, dbname, "CONNECT")
        self.priv_grant_schema(role_name, schema_name, "USAGE")

        console.print(Rule("授权对象权限", style="blue"))
        self.priv_grant_all_tables(role_name, schema_name, "SELECT,INSERT,UPDATE,DELETE")
        self.priv_grant_all_sequences(role_name, schema_name, "USAGE,SELECT,UPDATE")
        self.priv_grant_all_functions(role_name, schema_name, "EXECUTE")
        self.priv_grant_all_types(role_name, schema_name, "USAGE")

        console.print(Rule("设置默认权限（未来新建对象）", style="blue"))
        self.priv_set_default(role_name, schema_name, "SELECT,INSERT,UPDATE,DELETE", owner_role, "TABLES")
        self.priv_set_default(role_name, schema_name, "USAGE,SELECT,UPDATE", owner_role, "SEQUENCES")
        self.priv_set_default(role_name, schema_name, "EXECUTE", owner_role, "FUNCTIONS")
        self.priv_set_default(role_name, schema_name, "USAGE", owner_role, "TYPES")

        if not self.dry_run:
            console.print(Panel(
                f"[bold green]DML应用用户创建完成！[/bold green]\n\n"
                f"用户 [cyan]{username}[/cyan] 可增删改查数据，不能建对象/清表/建触发器\n\n"
                f"[yellow]注意:[/yellow]\n"
                f"  1. 默认权限只对 {owner_role} 创建的新对象生效\n"
                f"     若有其他角色也会建表，需额外执行 set-default 指定其他建表者\n"
                f"  2. 已存在的老表已通过 GRANT ALL TABLES 授权\n"
                f"     未来新建的表走默认权限自动授权",
                border_style="green", padding=(0, 2),
            ))

    def template_dba(self, username, password, dbname, schema_name, owner_role=None):
        """DBA管理员模板：全部数据权限 + CREATEROLE + pg_read_all_stats"""
        self._switch_db(dbname)
        role_name = f"r_{username}_dba"
        console.print(Panel(
            f"[cyan]用户名[/cyan]: {username}\n"
            f"[cyan]角色名[/cyan]: {role_name}\n"
            f"[cyan]数据库[/cyan]: {dbname}\n"
            f"[cyan]Schema[/cyan]: {schema_name}\n"
            f"[cyan]建表者[/cyan]: {owner_role}\n"
            f"[cyan]权限[/cyan]: CONNECT+CREATE+TEMPORARY, USAGE+CREATE(Schema), ALL PRIVILEGES(表+序列+函数+类型), USAGE(语言), CREATEROLE, pg_read_all_stats",
            title="DBA管理员模板", border_style="yellow", padding=(0, 2),
        ))

        console.print(Rule("创建角色和用户", style="blue"))
        self.role_create(role_name, comment="DBA管理员角色")
        self.user_create(username, password, comment="DBA管理员")
        self.user_grant_role(username, role_name)

        console.print(Rule("授权CREATEROLE属性", style="blue"))
        alter_sql = sql.SQL("ALTER ROLE {} CREATEROLE").format(sql.Identifier(username))
        self._execute(alter_sql)

        console.print(Rule("授权数据库和Schema", style="blue"))
        self.priv_grant_database(role_name, dbname, "CONNECT,CREATE,TEMPORARY")
        self.priv_grant_schema(role_name, schema_name, "USAGE,CREATE")

        console.print(Rule("授权对象权限", style="blue"))
        self.priv_grant_all_tables(role_name, schema_name, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER")
        self.priv_grant_all_sequences(role_name, schema_name, "USAGE,SELECT,UPDATE")
        self.priv_grant_all_functions(role_name, schema_name, "EXECUTE")
        self.priv_grant_all_types(role_name, schema_name, "USAGE")
        self.priv_grant_all_languages(role_name, "USAGE")

        console.print(Rule("设置默认权限（未来新建对象）", style="blue"))
        self.priv_set_default(role_name, schema_name, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER", owner_role, "TABLES")
        self.priv_set_default(role_name, schema_name, "USAGE,SELECT,UPDATE", owner_role, "SEQUENCES")
        self.priv_set_default(role_name, schema_name, "EXECUTE", owner_role, "FUNCTIONS")
        self.priv_set_default(role_name, schema_name, "USAGE", owner_role, "TYPES")

        console.print(Rule("授权监控角色", style="blue"))
        try:
            self.user_grant_role(username, "pg_read_all_stats")
        except Exception as e:
            console.print(f"[yellow][WARN] pg_read_all_stats 授权失败 (PG<10可能不存在): {e}[/yellow]")

        if not self.dry_run:
            console.print(Panel(
                f"[bold green]DBA管理员创建完成！[/bold green]\n\n"
                f"用户 [cyan]{username}[/cyan] 拥有全部数据权限、CREATEROLE属性和pg_read_all_stats监控角色\n\n"
                f"[yellow]注意:[/yellow]\n"
                f"  1. 默认权限只对 {owner_role} 创建的新对象生效\n"
                f"     若有其他角色也会建表，需额外执行 set-default 指定其他建表者\n"
                f"  2. 已存在的老表已通过 GRANT ALL TABLES 授权\n"
                f"     未来新建的表走默认权限自动授权\n"
                f"  3. CREATEROLE允许该用户创建/删除/修改其他角色和用户",
                border_style="green", padding=(0, 2),
            ))

    def template_apply(self, role_name, perm_type, dbname, schema_name, owner_role=None, append=False):
        """对已有角色批量应用权限模板（只读/读写/备份），不创建新用户"""
        # 切换到目标数据库（表权限只能对当前连接的库操作）
        self._switch_db(dbname)
        schemas = [schema_name]

        perm_labels = {
            "readonly": "只读（SELECT表+序列）",
            "dml": "DML应用写入（SELECT+INSERT+UPDATE+DELETE表 + 序列 + 函数 + 类型）",
            "readwrite": "读写（ALL PRIVILEGES + CREATE Schema）",
            "dba": "DBA管理员（ALL PRIVILEGES + CREATE Schema + CREATEROLE + pg_read_all_stats）",
            "backup": "备份（SELECT表 + SELECT+USAGE序列）",
        }

        mode_label = "追加权限" if append else "替换权限"
        title = f"应用权限模板（{mode_label}）"

        console.print(Panel(
            f"[cyan]角色名[/cyan]: {role_name}\n"
            f"[cyan]操作[/cyan]: {mode_label}\n"
            f"[cyan]模式[/cyan]: {perm_labels.get(perm_type, perm_type)}\n"
            f"[cyan]数据库[/cyan]: {dbname}\n"
            f"[cyan]Schema[/cyan]: {', '.join(schemas)}\n"
            f"[cyan]建表者[/cyan]: {owner_role}",
            title=title, border_style="yellow", padding=(0, 2),
        ))

        # 替换模式：先回收旧权限；追加模式：跳过回收
        if not append:
            console.print(Rule("回收旧权限", style="red"))
            for schema in schemas:
                self.priv_revoke_all_tables(role_name, schema, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER")
                self.priv_revoke_all_sequences(role_name, schema, "USAGE,SELECT,UPDATE")
                self.priv_revoke_all_functions(role_name, schema, "EXECUTE")
                self.priv_revoke_all_types(role_name, schema, "USAGE")
            self.priv_revoke_all_languages(role_name, "USAGE")

        # 授予新权限
        console.print(Rule("授予新权限", style="blue"))

        # 数据库级权限
        if perm_type == "dba":
            self.priv_grant_database(role_name, dbname, "CONNECT,CREATE,TEMPORARY")
        elif perm_type == "readwrite":
            self.priv_grant_database(role_name, dbname, "CONNECT,TEMPORARY")
        else:
            self.priv_grant_database(role_name, dbname, "CONNECT")

        for schema in schemas:
            # Schema级权限
            if perm_type in ("readwrite", "dba"):
                self.priv_grant_schema(role_name, schema, "USAGE,CREATE")
            else:
                self.priv_grant_schema(role_name, schema, "USAGE")

            if perm_type == "readonly":
                self.priv_grant_all_tables(role_name, schema, "SELECT")
                self.priv_grant_all_sequences(role_name, schema, "SELECT")
                self.priv_set_default(role_name, schema, "SELECT", owner_role, "TABLES")
                self.priv_set_default(role_name, schema, "SELECT", owner_role, "SEQUENCES")

            elif perm_type == "dml":
                self.priv_grant_all_tables(role_name, schema, "SELECT,INSERT,UPDATE,DELETE")
                self.priv_grant_all_sequences(role_name, schema, "USAGE,SELECT,UPDATE")
                self.priv_grant_all_functions(role_name, schema, "EXECUTE")
                self.priv_grant_all_types(role_name, schema, "USAGE")
                self.priv_set_default(role_name, schema, "SELECT,INSERT,UPDATE,DELETE", owner_role, "TABLES")
                self.priv_set_default(role_name, schema, "USAGE,SELECT,UPDATE", owner_role, "SEQUENCES")
                self.priv_set_default(role_name, schema, "EXECUTE", owner_role, "FUNCTIONS")
                self.priv_set_default(role_name, schema, "USAGE", owner_role, "TYPES")

            elif perm_type == "readwrite":
                self.priv_grant_all_tables(role_name, schema, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER")
                self.priv_grant_all_sequences(role_name, schema, "USAGE,SELECT,UPDATE")
                self.priv_grant_all_functions(role_name, schema, "EXECUTE")
                self.priv_grant_all_types(role_name, schema, "USAGE")
                self.priv_set_default(role_name, schema, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER", owner_role, "TABLES")
                self.priv_set_default(role_name, schema, "USAGE,SELECT,UPDATE", owner_role, "SEQUENCES")
                self.priv_set_default(role_name, schema, "EXECUTE", owner_role, "FUNCTIONS")
                self.priv_set_default(role_name, schema, "USAGE", owner_role, "TYPES")

            elif perm_type == "dba":
                self.priv_grant_all_tables(role_name, schema, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER")
                self.priv_grant_all_sequences(role_name, schema, "USAGE,SELECT,UPDATE")
                self.priv_grant_all_functions(role_name, schema, "EXECUTE")
                self.priv_grant_all_types(role_name, schema, "USAGE")
                self.priv_set_default(role_name, schema, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER", owner_role, "TABLES")
                self.priv_set_default(role_name, schema, "USAGE,SELECT,UPDATE", owner_role, "SEQUENCES")
                self.priv_set_default(role_name, schema, "EXECUTE", owner_role, "FUNCTIONS")
                self.priv_set_default(role_name, schema, "USAGE", owner_role, "TYPES")

            elif perm_type == "backup":
                self.priv_grant_all_tables(role_name, schema, "SELECT")
                self.priv_grant_all_sequences(role_name, schema, "SELECT,USAGE")
                self.priv_set_default(role_name, schema, "SELECT", owner_role, "TABLES")
                self.priv_set_default(role_name, schema, "SELECT,USAGE", owner_role, "SEQUENCES")

        # language 是数据库级对象，只对读写/DBA模板授予
        if perm_type in ("readwrite", "dba"):
            self.priv_grant_all_languages(role_name, "USAGE")

        # DBA特有：CREATEROLE + pg_read_all_stats
        if perm_type == "dba":
            alter_sql = sql.SQL("ALTER ROLE {} CREATEROLE").format(sql.Identifier(role_name))
            self._execute(alter_sql)
            try:
                self.user_grant_role(role_name, "pg_read_all_stats")
            except Exception as e:
                console.print(f"[yellow][WARN] pg_read_all_stats 授权失败 (PG<10可能不存在): {e}[/yellow]")

        if not self.dry_run:
            console.print(Panel(
                f"[bold green]权限模板{mode_label}完成！[/bold green]\n\n"
                f"角色 [cyan]{role_name}[/cyan] 已{mode_label}为 [cyan]{perm_labels.get(perm_type, perm_type)}[/cyan]\n"
                f"数据库: {dbname}\n"
                f"Schema: {', '.join(schemas)}",
                border_style="green", padding=(0, 2),
            ))

    def template_revoke_all(self, role_name, dbname, schema_name=None):
        """一键回收角色在指定数据库Schema下的所有权限（schema_name=None时回收所有用户Schema）"""
        self._switch_db(dbname)

        if schema_name:
            schemas = [schema_name]
        else:
            if self.dry_run:
                schemas = ["<all_user_schemas>"]
            else:
                schemas = self._get_user_schemas()

        schema_display = ', '.join(schemas)

        console.print(Panel(
            f"[cyan]角色名[/cyan]: {role_name}\n"
            f"[cyan]数据库[/cyan]: {dbname}\n"
            f"[cyan]Schema[/cyan]: {schema_display}\n"
            f"[cyan]操作[/cyan]: 回收所有权限",
            title="一键回收权限", border_style="red", padding=(0, 2),
        ))

        console.print(Rule("回收数据库权限", style="red"))
        self.priv_revoke_database(role_name, dbname, "CONNECT,CREATE,TEMPORARY")

        for schema in schemas:
            console.print(Rule(f"回收 Schema: {schema}", style="red"))
            self.priv_revoke_schema(role_name, schema, "USAGE,CREATE")
            self.priv_revoke_all_tables(role_name, schema, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER")
            self.priv_revoke_all_sequences(role_name, schema, "USAGE,SELECT,UPDATE")
            self.priv_revoke_all_functions(role_name, schema, "EXECUTE")
            self.priv_revoke_all_types(role_name, schema, "USAGE")
            self._revoke_default_privileges(role_name, schema)

        console.print(Rule("回收语言权限", style="red"))
        self.priv_revoke_all_languages(role_name, "USAGE")

        if not self.dry_run:
            console.print(Panel(
                f"[bold green]权限回收完成！[/bold green]\n\n"
                f"角色 [cyan]{role_name}[/cyan] 在数据库 [yellow]{dbname}[/yellow] Schema [yellow]{schema_display}[/yellow] 下的所有权限已回收",
                border_style="green", padding=(0, 2),
            ))

    def _revoke_default_privileges(self, role_name, schema_name):
        """查询pg_default_acl，回收该Schema下授予目标角色的所有默认权限"""
        if self.dry_run:
            console.print("[dim]-- Dry-run模式：无法查询pg_default_acl，跳过默认权限回收[/dim]")
            console.print("[dim]-- 实际执行时会自动查询并回收所有相关默认权限[/dim]")
            return

        query = """
            SELECT
                r.rolname AS owner_role,
                da.defaclobjtype AS obj_type,
                array_agg(DISTINCT acl.privilege_type) AS privileges
            FROM pg_default_acl da
            JOIN pg_roles r ON r.oid = da.defaclrole
            CROSS JOIN LATERAL aclexplode(da.defaclacl) acl
            JOIN pg_roles ar ON ar.oid = acl.grantee
            WHERE da.defaclnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s)
              AND ar.rolname = %s
            GROUP BY r.rolname, da.defaclobjtype
            ORDER BY r.rolname, da.defaclobjtype
        """
        rows = self._execute(query, (schema_name, role_name), fetch=True)
        if not rows:
            console.print("[dim]无需回收默认权限（无相关默认权限记录）[/dim]")
            return

        obj_type_map = {'r': 'TABLES', 'S': 'SEQUENCES', 'f': 'FUNCTIONS', 'T': 'TYPES'}

        for owner_role, obj_type_code, privileges in rows:
            obj_type = obj_type_map.get(obj_type_code, 'TABLES')
            priv_str = ','.join(privileges)

            query_revoke = sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} REVOKE {} ON {} FROM {}"
            ).format(
                sql.Identifier(owner_role),
                sql.Identifier(schema_name),
                sql.SQL(priv_str),
                sql.SQL(obj_type),
                sql.Identifier(role_name),
            )
            if self._execute(query_revoke):
                console.print(f"[bold green][OK][/bold green] 已回�.�� {owner_role} 在 Schema {schema_name} 下新建{obj_type}的默认权限 ({priv_str})")

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None


def _print_rich_help(context):
    """用 Rich 渲染美观的帮助信息。context 是 -h 之前的命令行参数列表。"""
    module = context[0] if len(context) >= 1 else None
    action = context[1] if len(context) >= 2 else None

    # ── 顶层帮助 ──
    if module is None:
        console.print(Rule("[bold blue]PostgreSQL RBAC 权限管理工具[/bold blue]"))
        console.print()
        console.print("[bold]用法:[/bold]  pg_rbac.py [选项] <模块> <子命令> [参数]")
        console.print()
        console.print("[bold]全局选项:[/bold]")
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("选项", style="cyan")
        t.add_column("说明")
        t.add_row("-c, --config", "配置文件路径 (默认 config.yaml)")
        t.add_row("--dry-run", "只生成SQL不执行")
        t.add_row("-h, --help", "显示帮助信息")
        console.print(t)
        console.print()
        console.print("[bold]功能模块:[/bold]")
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("模块", style="bold green")
        t.add_column("说明")
        t.add_row("role", "角色管理（权限组，无登录权限）")
        t.add_row("user", "用户管理（可登录账号）")
        t.add_row("priv", "权限管理（授权/回收/默认权限）")
        t.add_row("query", "权限查询（查看用户/角色权限）")
        t.add_row("template", "一键模板（自动创建角色+用户+权限）")
        console.print(t)
        console.print()
        console.print("[dim]使用 pg_rbac.py <模块> -h 查看模块详细帮助[/dim]")
        return

    # ── role 模块 ──
    if module == "role":
        console.print(Rule("[bold blue]role - 角色管理[/bold blue]"))
        console.print()
        console.print("[bold]用法:[/bold]  pg_rbac.py role <子命令> [参数]")
        console.print()
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("子命令", style="bold green")
        t.add_column("说明")
        t.add_row("create <角色名> [-m 备注]", "创建角色")
        t.add_row("list", "列出所有自定义角色")
        t.add_row("drop <角色名>", "删除角色")
        console.print(t)
        return

    # ── user 模块 ──
    if module == "user":
        console.print(Rule("[bold blue]user - 用户管理[/bold blue]"))
        console.print()
        console.print("[bold]用法:[/bold]  pg_rbac.py user <子命令> [参数]")
        console.print()
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("子命令", style="bold green")
        t.add_column("说明")
        t.add_row("create <用户名> -p 密码 [-m 备注]", "创建可登录用户")
        t.add_row("drop <用户名>", "删除用户")
        t.add_row("passwd <用户名> -p 新密码", "修改用户密码")
        t.add_row("disable <用户名>", "禁用用户（禁止登录）")
        t.add_row("enable <用户名>", "启用用户（允许登录）")
        t.add_row("grant-role <用户名> <角色名>", "给用户授予角色")
        t.add_row("revoke-role <用户名> <角色名>", "回收用户的角色")
        t.add_row("list", "列出所有可登录用户及角色")
        console.print(t)
        return

    # ── priv 模块 ──
    if module == "priv":
        console.print(Rule("[bold blue]priv - 权限管理[/bold blue]"))
        console.print()
        console.print("[bold]用法:[/bold]  pg_rbac.py priv <子命令> [参数]")
        console.print()
        console.print("[dim]通用选项: -d, --dbname <数据库名>  切换到指定数据库执行[/dim]")
        console.print()

        # 授权组
        console.print("[bold yellow]━━ 授权命令 (grant-*) ━━[/bold yellow]")
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("子命令", style="bold green")
        t.add_column("说明")
        t.add_row("grant-db <角色> <库名> [-p 权限]", "数据库级授权 (权限: CONNECT,CREATE,TEMPORARY)")
        t.add_row("grant-schema <角色> <Schema> [-p 权限]", "Schema级授权 (权限: USAGE,CREATE)")
        t.add_row("grant-table <角色> <Schema> <表> [-p 权限]", "单表授权 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)")
        t.add_row("grant-table-regex <角色> <Schema> <正则> [-p 权限]", "按正则批量授权表 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)")
        t.add_row("grant-all-tables <角色> <Schema> [-p 权限]", "授权Schema下所有表 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)")
        t.add_row("grant-all-sequences <角色> <Schema> [-p 权限]", "授权Schema下所有序列 (权限: USAGE,SELECT,UPDATE)")
        t.add_row("grant-all-functions <角色> <Schema> [-p 权限]", "授权Schema下所有函数 (权限: EXECUTE)")
        t.add_row("grant-all-types <角色> <Schema> [-p 权限]", "授权Schema下所有类型 (权限: USAGE)")
        t.add_row("grant-all-languages <角色> [-p 权限]", "授权所有过程语言 (权限: USAGE)")
        console.print(t)
        console.print()

        # 默认权限
        console.print("[bold yellow]━━ 默认权限 (set-default / revoke-default) ━━[/bold yellow]")
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("子命令", style="bold green")
        t.add_column("说明")
        t.add_row("set-default <角色> <Schema>  [-T 类型] [-p 权限] [-o 建表者]", "设置新建对象的默认权限")
        t.add_row("revoke-default <角色> <Schema>  [-T 类型] -p 权限 [-o 建表者]", "回收新建对象的默认权限")
        console.print(t)
        console.print("[dim]  -T TABLES      -p 可选: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER (默认SELECT)[/dim]")
        console.print("[dim]  -T SEQUENCES   -p 可选: USAGE,SELECT,UPDATE (默认SELECT)[/dim]")
        console.print("[dim]  -T FUNCTIONS   -p 可选: EXECUTE (默认EXECUTE)[/dim]")
        console.print("[dim]  -T TYPES       -p 可选: USAGE (默认USAGE)[/dim]")
        console.print()

        # 回收组
        console.print("[bold yellow]━━ 回收命令 (revoke-*) ━━[/bold yellow]")
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("子命令", style="bold green")
        t.add_column("说明")
        t.add_row("revoke-db <角色> <库名> -p 权限", "回收数据库权限 (权限: CONNECT,CREATE,TEMPORARY)")
        t.add_row("revoke-schema <角色> <Schema> -p 权限", "回收Schema权限 (权限: USAGE,CREATE)")
        t.add_row("revoke-table <角色> <Schema> <表> -p 权限", "单表回收权限 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)")
        t.add_row("revoke-table-regex <角色> <Schema> <正则> -p 权限", "按正则批量回收表权限 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)")
        t.add_row("revoke-all-tables <角色> <Schema> -p 权限", "回收Schema下所有表权限 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)")
        t.add_row("revoke-all-sequences <角色> <Schema> -p 权限", "回收Schema下所有序列权限 (权限: USAGE,SELECT,UPDATE)")
        t.add_row("revoke-all-functions <角色> <Schema> -p 权限", "回收Schema下所有函数权限 (权限: EXECUTE)")
        t.add_row("revoke-all-types <角色> <Schema> -p 权限", "回收Schema下所有类型权限 (权限: USAGE)")
        t.add_row("revoke-all-languages <角色> -p 权限", "回收所有过程语言权限 (权限: USAGE)")
        console.print(t)
        return

    # ── query 模块 ──
    if module == "query":
        console.print(Rule("[bold blue]query - 权限查询[/bold blue]"))
        console.print()
        console.print("[bold]用法:[/bold]  pg_rbac.py query <子命令> [参数]")
        console.print()
        console.print("[dim]通用选项: -d, --dbname <数据库名>  切换到指定数据库查询[/dim]")
        console.print()
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("子命令", style="bold green")
        t.add_column("说明")
        t.add_row("user <用户名> [-f 表筛选] [-v]", "查询用户完整权限（级联所有继承角色）")
        t.add_row("role <角色名> [-f 表筛选] [-v]", "查询角色完整权限（级联所有继承角色）")
        console.print(t)
        console.print()
        console.print("[dim]  -f, --table-filter  表名筛选 (SQL LIKE, 如 'ac_%')[/dim]")
        console.print("[dim]  -v, --verbose        显示全部权限（含序列/函数/类型/语言）[/dim]")
        return

    # ── template 模块 ──
    if module == "template":
        console.print(Rule("[bold blue]template - 一键模板[/bold blue]"))
        console.print()
        console.print("[bold]用法:[/bold]  pg_rbac.py template <子命令> [参数]")
        console.print()
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("子命令", style="bold green")
        t.add_column("说明")
        t.add_row("backup <用户名> -p 密码 -d 库名 -o 建表者", "创建备份账号模板 (SELECT表 + SELECT,USAGE序列)")
        t.add_row("readonly <用户名> -p 密码 -d 库名 -s Schema -o 建表者", "创建只读用户模板 (SELECT表+序列)")
        t.add_row("dml <用户名> -p 密码 -d 库名 -s Schema -o 建表者", "创建DML应用写入模板 (SELECT,INSERT,UPDATE,DELETE表+序列+函数+类型)")
        t.add_row("readwrite <用户名> -p 密码 -d 库名 -s Schema -o 建表者", "创建读写用户模板 (ALL PRIVILEGES+CREATE Schema+语言)")
        t.add_row("dba <用户名> -p 密码 -d 库名 -s Schema -o 建表者", "创建DBA管理员模板 (ALL PRIVILEGES+CREATEROLE+pg_read_all_stats)")
        t.add_row("apply <角色名> -t 模式 -d 库名 -s Schema -o 建表者", "对已有角色应用权限模板")
        t.add_row("apply-append <角色名> -t 模式 -d 库名 -s Schema -o 建表者", "对已有角色追加权限")
        t.add_row("revoke-all <角色名> -d 库名 [-s Schema]", "一键回收角色所有权限")
        console.print(t)
        console.print()
        console.print("[dim]  -t 权限模式: readonly / dml / readwrite / dba / backup[/dim]")
        console.print("[dim]  -o, --owner-role  建表者角色（用于设置默认权限）[/dim]")
        return

    # ── 未知模块 ──
    console.print(f"[yellow]未知模块: {module}[/yellow]")
    console.print("[dim]使用 pg_rbac.py -h 查看所有模块[/dim]")


def main():
    # 拦截 -h/--help，用 Rich 渲染美观帮助
    if "-h" in sys.argv or "--help" in sys.argv:
        argv = sys.argv[1:]
        help_idx = -1
        for i, a in enumerate(argv):
            if a in ("-h", "--help"):
                help_idx = i
                break
        # 过滤掉全局选项（如 --dry-run, -c config.yaml），只保留模块名
        context = [a for a in argv[:help_idx] if not a.startswith('-')]
        _print_rich_help(context)
        sys.exit(0)

    parser = argparse.ArgumentParser(
        description="PostgreSQL RBAC 权限管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只生成SQL不执行")
    parser.add_argument("-h", "--help", action="store_true", help="显示帮助信息")

    subparsers = parser.add_subparsers(dest="module", help="功能模块")

    # 公共参数：数据库切换（用于 priv/query 模块的子命令）
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument("-d", "--dbname", dest="switch_db", help="切换到指定数据库执行（不指定则用配置文件默认库）")

    # ---------- 模块1：角色管理 ----------
    role_parser = subparsers.add_parser("role", help="角色管理（权限组，无登录权限）")
    role_sub = role_parser.add_subparsers(dest="action")

    role_create = role_sub.add_parser("create", help="创建角色")
    role_create.add_argument("role_name", help="角色名称")
    role_create.add_argument("-m", "--comment", help="角色备注说明")

    role_sub.add_parser("list", help="列出所有自定义角色和用户")

    role_drop = role_sub.add_parser("drop", help="删除角色")
    role_drop.add_argument("role_name", help="角色名称")

    # ---------- 模块2：用户管理 ----------
    user_parser = subparsers.add_parser("user", help="用户管理（可登录账号）")
    user_sub = user_parser.add_subparsers(dest="action")

    user_create = user_sub.add_parser("create", help="创建可登录用户")
    user_create.add_argument("username", help="用户名")
    user_create.add_argument("-p", "--password", required=True, help="用户登录密码")
    user_create.add_argument("-m", "--comment", help="用户备注说明")

    user_drop = user_sub.add_parser("drop", help="删除用户")
    user_drop.add_argument("username", help="用户名")

    user_passwd = user_sub.add_parser("passwd", help="修改用户密码")
    user_passwd.add_argument("username", help="用户名")
    user_passwd.add_argument("-p", "--password", required=True, help="新密码")

    user_disable = user_sub.add_parser("disable", help="禁用用户（禁止登录）")
    user_disable.add_argument("username", help="用户名")

    user_enable = user_sub.add_parser("enable", help="启用用户（允许登录）")
    user_enable.add_argument("username", help="用户名")

    user_grant = user_sub.add_parser("grant-role", help="给用户授予角色（继承权限）")
    user_grant.add_argument("username", help="用户名")
    user_grant.add_argument("role_name", help="角色名")

    user_sub.add_parser("list", help="列出所有可登录用户及其角色")

    user_revoke = user_sub.add_parser("revoke-role", help="回收用户的角色")
    user_revoke.add_argument("username", help="用户名")
    user_revoke.add_argument("role_name", help="角色名")

    # ---------- 模块3：权限管理 ----------
    priv_parser = subparsers.add_parser("priv", help="权限管理（数据库/Schema/表三级授权）")
    priv_sub = priv_parser.add_subparsers(dest="action")

    # ===== 授权命令（grant-*） =====

    # --- 数据库级授权 ---
    g_db = priv_sub.add_parser("grant-db", help="数据库级授权 (权限: CONNECT,CREATE,TEMPORARY)")
    g_db.add_argument("role_name", help="角色名")
    g_db.add_argument("dbname", help="数据库名")
    g_db.add_argument(
        "-p", "--priv",
        default="CONNECT",
        help="权限类型，多个用逗号分隔。可选值：CONNECT, CREATE, TEMPORARY",
    )

    # --- Schema级授权 ---
    g_schema = priv_sub.add_parser("grant-schema", help="Schema级授权 (权限: USAGE,CREATE)", parents=[db_parent])
    g_schema.add_argument("role_name", help="角色名")
    g_schema.add_argument("schema_name", help="Schema名称")
    g_schema.add_argument(
        "-p", "--priv",
        default="USAGE",
        help="权限类型，多个用逗号分隔。可选值：USAGE, CREATE",
    )

    # --- 单表授权 ---
    g_table = priv_sub.add_parser("grant-table", help="单表授权 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)", parents=[db_parent])
    g_table.add_argument("role_name", help="角色名")
    g_table.add_argument("schema_name", help="Schema名称")
    g_table.add_argument("table_name", help="表名")
    g_table.add_argument(
        "-p", "--priv",
        default="SELECT",
        help="权限类型，多个用逗号分隔。可选值：SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
    )

    # --- 表正则批量授权 ---
    g_table_regex = priv_sub.add_parser("grant-table-regex", help="按正则批量授权表 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)", parents=[db_parent])
    g_table_regex.add_argument("role_name", help="角色名")
    g_table_regex.add_argument("schema_name", help="Schema名称")
    g_table_regex.add_argument("table_pattern", help="表名正则表达式，如 t_.* 匹配所有t_开头的表")
    g_table_regex.add_argument(
        "-p", "--priv",
        default="SELECT",
        help="权限类型，多个用逗号分隔。可选值：SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
    )

    # --- 全表授权 ---
    g_all_tables = priv_sub.add_parser("grant-all-tables", help="授权Schema下所有表的权限 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)", parents=[db_parent])
    g_all_tables.add_argument("role_name", help="角色名")
    g_all_tables.add_argument("schema_name", help="Schema名称")
    g_all_tables.add_argument(
        "-p", "--priv",
        default="SELECT",
        help="权限类型，多个用逗号分隔。可选值：SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
    )

    # --- 序列授权 ---
    g_all_seq = priv_sub.add_parser("grant-all-sequences", help="授权Schema下所有序列的权限 (权限: USAGE,SELECT,UPDATE)", parents=[db_parent])
    g_all_seq.add_argument("role_name", help="角色名")
    g_all_seq.add_argument("schema_name", help="Schema名称")
    g_all_seq.add_argument(
        "-p", "--priv",
        default="SELECT",
        help="权限类型，多个用逗号分隔。可选值：USAGE,SELECT,UPDATE",
    )

    # --- 函数授权 ---
    g_all_func = priv_sub.add_parser("grant-all-functions", help="授权Schema下所有函数的权限 (权限: EXECUTE)", parents=[db_parent])
    g_all_func.add_argument("role_name", help="角色名")
    g_all_func.add_argument("schema_name", help="Schema名称")
    g_all_func.add_argument(
        "-p", "--priv",
        default="EXECUTE",
        help="权限类型。可选值：EXECUTE",
    )

    # --- 类型授权 ---
    g_all_type = priv_sub.add_parser("grant-all-types", help="授权Schema下所有类型的权限 (权限: USAGE)", parents=[db_parent])
    g_all_type.add_argument("role_name", help="角色名")
    g_all_type.add_argument("schema_name", help="Schema名称")
    g_all_type.add_argument(
        "-p", "--priv",
        default="USAGE",
        help="权限类型。可选值：USAGE",
    )

    # --- 语言授权 ---
    g_all_lang = priv_sub.add_parser("grant-all-languages", help="授权所有过程语言的权限 (权限: USAGE)")
    g_all_lang.add_argument("role_name", help="角色名")
    g_all_lang.add_argument(
        "-p", "--priv",
        default="USAGE",
        help="权限类型。可选值：USAGE",
    )

    # ===== 默认权限 =====

    set_def = priv_sub.add_parser("set-default", help="设置Schema下新建对象的默认权限", parents=[db_parent])
    set_def.add_argument("role_name", help="角色名")
    set_def.add_argument("schema_name", help="Schema名称")
    set_def.add_argument(
        "-p", "--priv",
        default="SELECT",
        help="权限类型，多个用逗号分隔。TABLES:SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER SEQUENCES:USAGE,SELECT,UPDATE FUNCTIONS:EXECUTE TYPES:USAGE",
    )
    set_def.add_argument("-o", "--target-role", help="建表者角色，默认为当前连接用户")
    set_def.add_argument(
        "-T", "--obj-type",
        default="TABLES",
        choices=["TABLES", "SEQUENCES", "FUNCTIONS", "TYPES"],
        help="对象类型：TABLES(表)、SEQUENCES(序列)、FUNCTIONS(函数)、TYPES(类型)，默认TABLES",
    )

    # --- 回收默认权限 ---
    rev_def = priv_sub.add_parser("revoke-default", help="回收Schema下新建对象的默认权限", parents=[db_parent])
    rev_def.add_argument("role_name", help="角色名")
    rev_def.add_argument("schema_name", help="Schema名称")
    rev_def.add_argument(
        "-p", "--priv",
        required=True,
        help="权限类型，多个用逗号分隔。TABLES:SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER SEQUENCES:USAGE,SELECT,UPDATE FUNCTIONS:EXECUTE TYPES:USAGE",
    )
    rev_def.add_argument("-o", "--target-role", help="建表者角色，默认为当前连接用户")
    rev_def.add_argument(
        "-T", "--obj-type",
        default="TABLES",
        choices=["TABLES", "SEQUENCES", "FUNCTIONS", "TYPES"],
        help="对象类型：TABLES(表)、SEQUENCES(序列)、FUNCTIONS(函数)、TYPES(类型)，默认TABLES",
    )

    # ===== 回收命令（revoke-*） =====

    # --- 数据库级回收 ---
    r_db = priv_sub.add_parser("revoke-db", help="回收数据库权限 (权限: CONNECT,CREATE,TEMPORARY)")
    r_db.add_argument("role_name", help="角色名")
    r_db.add_argument("dbname", help="数据库名")
    r_db.add_argument(
        "-p", "--priv",
        required=True,
        help="权限类型，多个用逗号分隔。可选值：CONNECT, CREATE, TEMPORARY",
    )

    # --- Schema级回收 ---
    r_schema = priv_sub.add_parser("revoke-schema", help="回收Schema权限 (权限: USAGE,CREATE)", parents=[db_parent])
    r_schema.add_argument("role_name", help="角色名")
    r_schema.add_argument("schema_name", help="Schema名称")
    r_schema.add_argument(
        "-p", "--priv",
        required=True,
        help="权限类型，多个用逗号分隔。可选值：USAGE, CREATE",
    )

    # --- 单表回收 ---
    r_table = priv_sub.add_parser("revoke-table", help="单表回收权限 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)", parents=[db_parent])
    r_table.add_argument("role_name", help="角色名")
    r_table.add_argument("schema_name", help="Schema名称")
    r_table.add_argument("table_name", help="表名")
    r_table.add_argument(
        "-p", "--priv",
        required=True,
        help="权限类型，多个用逗号分隔。可选值：SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
    )

    # --- 表正则批量回收 ---
    r_table_regex = priv_sub.add_parser("revoke-table-regex", help="按正则批量回收表权限 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)", parents=[db_parent])
    r_table_regex.add_argument("role_name", help="角色名")
    r_table_regex.add_argument("schema_name", help="Schema名称")
    r_table_regex.add_argument("table_pattern", help="表名正则表达式，如 t_.* 匹配所有t_开头的表")
    r_table_regex.add_argument(
        "-p", "--priv",
        required=True,
        help="权限类型，多个用逗号分隔。可选值：SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
    )

    # --- 全表回收 ---
    r_all_tables = priv_sub.add_parser("revoke-all-tables", help="回收Schema下所有表的权限 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)", parents=[db_parent])
    r_all_tables.add_argument("role_name", help="角色名")
    r_all_tables.add_argument("schema_name", help="Schema名称")
    r_all_tables.add_argument(
        "-p", "--priv",
        required=True,
        help="权限类型，多个用逗号分隔。可选值：SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
    )

    # --- 序列回收 ---
    r_all_seq = priv_sub.add_parser("revoke-all-sequences", help="回收Schema下所有序列的权限 (权限: USAGE,SELECT,UPDATE)", parents=[db_parent])
    r_all_seq.add_argument("role_name", help="角色名")
    r_all_seq.add_argument("schema_name", help="Schema名称")
    r_all_seq.add_argument(
        "-p", "--priv",
        required=True,
        help="权限类型，多个用逗号分隔。可选值：USAGE,SELECT,UPDATE",
    )

    # --- 函数回收 ---
    r_all_func = priv_sub.add_parser("revoke-all-functions", help="回收Schema下所有函数的权限 (权限: EXECUTE)", parents=[db_parent])
    r_all_func.add_argument("role_name", help="角色名")
    r_all_func.add_argument("schema_name", help="Schema名称")
    r_all_func.add_argument(
        "-p", "--priv",
        required=True,
        help="权限类型。可选值：EXECUTE",
    )

    # --- 类型回收 ---
    r_all_type = priv_sub.add_parser("revoke-all-types", help="回收Schema下所有类型的权限 (权限: USAGE)", parents=[db_parent])
    r_all_type.add_argument("role_name", help="角色名")
    r_all_type.add_argument("schema_name", help="Schema名称")
    r_all_type.add_argument(
        "-p", "--priv",
        required=True,
        help="权限类型。可选值：USAGE",
    )

    # --- 语言回收 ---
    r_all_lang = priv_sub.add_parser("revoke-all-languages", help="回收所有过程语言的权限 (权限: USAGE)")
    r_all_lang.add_argument("role_name", help="角色名")
    r_all_lang.add_argument(
        "-p", "--priv",
        required=True,
        help="权限类型。可选值：USAGE",
    )

    # ---------- 模块4：权限查询 ----------
    query_parser = subparsers.add_parser("query", help="权限查询")
    query_sub = query_parser.add_subparsers(dest="action")

    q_user = query_sub.add_parser("user", help="查询用户完整权限（级联所有继承角色）", parents=[db_parent])
    q_user.add_argument("username", help="用户名")
    q_user.add_argument("-f", "--table-filter", dest="table_filter", help="表名筛选（SQL LIKE语法，如 ac_%%）")
    q_user.add_argument("-v", "--verbose", action="store_true", help="显示全部权限（含序列/函数/类型/语言）")

    q_role = query_sub.add_parser("role", help="查询角色完整权限（级联所有继承角色）", parents=[db_parent])
    q_role.add_argument("role_name", help="角色名")
    q_role.add_argument("-f", "--table-filter", dest="table_filter", help="表名筛选（SQL LIKE语法，如 ac_%%）")
    q_role.add_argument("-v", "--verbose", action="store_true", help="显示全部权限（含序列/函数/类型/语言）")

    # ---------- 模块5：模板 ----------
    tpl_parser = subparsers.add_parser("template", help="一键模板（自动创建角色+用户+权限）")
    tpl_sub = tpl_parser.add_subparsers(dest="action")

    tpl_backup = tpl_sub.add_parser("backup", help="创建备份账号模板（SELECT表+SELECT,USAGE序列，不含函数/类型/语言）")
    tpl_backup.add_argument("username", help="用户名，如 db_backup")
    tpl_backup.add_argument("-p", "--password", required=True, help="用户密码")
    tpl_backup.add_argument("-d", "--dbname", required=True, help="数据库名")
    tpl_backup.add_argument("-o", "--owner-role", required=True, help="建表者角色（必须指定实际建表用户，不能省略）")

    tpl_ro = tpl_sub.add_parser("readonly", help="创建只读用户模板（SELECT表+序列，不含函数/类型/语言）")
    tpl_ro.add_argument("username", help="用户名，如 app_readonly")
    tpl_ro.add_argument("-p", "--password", required=True, help="用户密码")
    tpl_ro.add_argument("-d", "--dbname", required=True, help="数据库名")
    tpl_ro.add_argument("-s", "--schema", required=True, help="Schema名称")
    tpl_ro.add_argument("-o", "--owner-role", required=True, help="建表者角色（必须指定实际建表用户，不能省略）")

    tpl_rw = tpl_sub.add_parser("readwrite", help="创建读写用户模板（ALL PRIVILEGES+CREATE Schema，含DDL+DML）")
    tpl_rw.add_argument("username", help="用户名，如 app_readwrite")
    tpl_rw.add_argument("-p", "--password", required=True, help="用户密码")
    tpl_rw.add_argument("-d", "--dbname", required=True, help="数据库名")
    tpl_rw.add_argument("-s", "--schema", required=True, help="Schema名称")
    tpl_rw.add_argument("-o", "--owner-role", required=True, help="建表者角色（必须指定实际建表用户，不能省略）")

    tpl_dml = tpl_sub.add_parser("dml", help="创建DML应用写入模板（SELECT,INSERT,UPDATE,DELETE，不含DDL）")
    tpl_dml.add_argument("username", help="用户名，如 app_writer")
    tpl_dml.add_argument("-p", "--password", required=True, help="用户密码")
    tpl_dml.add_argument("-d", "--dbname", required=True, help="数据库名")
    tpl_dml.add_argument("-s", "--schema", required=True, help="Schema名称")
    tpl_dml.add_argument("-o", "--owner-role", required=True, help="建表者角色（必须指定实际建表用户，不能省略）")

    tpl_dba = tpl_sub.add_parser("dba", help="创建DBA管理员模板（ALL PRIVILEGES+CREATEROLE+pg_read_all_stats）")
    tpl_dba.add_argument("username", help="用户名，如 dba_admin")
    tpl_dba.add_argument("-p", "--password", required=True, help="用户密码")
    tpl_dba.add_argument("-d", "--dbname", required=True, help="数据库名")
    tpl_dba.add_argument("-s", "--schema", required=True, help="Schema名称")
    tpl_dba.add_argument("-o", "--owner-role", required=True, help="建表者角色（必须指定实际建表用户，不能省略）")

    tpl_apply = tpl_sub.add_parser(
        "apply",
        help="对已有角色批量应用权限模板（不创建新用户，先回收旧权限再授予新权限）",
    )
    tpl_apply.add_argument("role_name", help="已存在的角色名，如 pre_role_rw")
    tpl_apply.add_argument(
        "-t", "--type",
        required=True,
        choices=["readonly", "dml", "readwrite", "dba", "backup"],
        help="权限模式：readonly(只读)、dml(应用写入)、readwrite(读写)、dba(DBA管理员)、backup(备份)",
    )
    tpl_apply.add_argument("-d", "--dbname", required=True, help="数据库名")
    tpl_apply.add_argument(
        "-s", "--schema",
        required=True,
        help="Schema名称",
    )
    tpl_apply.add_argument("-o", "--owner-role", required=True, help="建表者角色（必须指定实际建表用户，不能省略）")

    tpl_append = tpl_sub.add_parser(
        "apply-append",
        help="对已有角色追加权限（不回收旧权限，保留其他库/Schema的权限）",
    )
    tpl_append.add_argument("role_name", help="已存在的角色名，如 pre_role_rw")
    tpl_append.add_argument(
        "-t", "--type",
        required=True,
        choices=["readonly", "dml", "readwrite", "dba", "backup"],
        help="权限模式：readonly(只读)、dml(应用写入)、readwrite(读写)、dba(DBA管理员)、backup(备份)",
    )
    tpl_append.add_argument("-d", "--dbname", required=True, help="数据库名")
    tpl_append.add_argument("-s", "--schema", required=True, help="Schema名称")
    tpl_append.add_argument("-o", "--owner-role", required=True, help="建表者角色（必须指定实际建表用户，不能省略）")

    tpl_revoke = tpl_sub.add_parser(
        "revoke-all",
        help="一键回收角色在指定数据库Schema下的所有权限（表/序列/函数/默认权限/Schema/数据库）",
    )
    tpl_revoke.add_argument("role_name", help="角色名，如 pre_role_rw")
    tpl_revoke.add_argument("-d", "--dbname", required=True, help="数据库名")
    tpl_revoke.add_argument("-s", "--schema", help="Schema名称（不指定则回收所有用户Schema）")

    args = parser.parse_args()

    if not args.module:
        parser.print_help()
        sys.exit(1)
    if not getattr(args, "action", None):
        parser.parse_args([args.module, "-h"])

    console.print(Rule(f"[bold blue]PostgreSQL RBAC 权限管理工具[/bold blue]"))

    mgr = PgRbacManager(config_path=args.config, dry_run=args.dry_run)

    try:
        # ---- 角色模块 ----
        if args.module == "role":
            if args.action == "create":
                mgr.role_create(args.role_name, args.comment)
            elif args.action == "drop":
                mgr.role_drop(args.role_name)
            elif args.action == "list":
                mgr.role_list()

        # ---- 用户模块 ----
        elif args.module == "user":
            if args.action == "create":
                mgr.user_create(args.username, args.password, args.comment)
            elif args.action == "drop":
                mgr.user_drop(args.username)
            elif args.action == "passwd":
                mgr.user_change_password(args.username, args.password)
            elif args.action == "disable":
                mgr.user_disable(args.username)
            elif args.action == "enable":
                mgr.user_enable(args.username)
            elif args.action == "grant-role":
                mgr.user_grant_role(args.username, args.role_name)
            elif args.action == "revoke-role":
                mgr.user_revoke_role(args.username, args.role_name)
            elif args.action == "list":
                mgr.user_list()

        # ---- 权限模块 ----
        elif args.module == "priv":
            switch_db = getattr(args, "switch_db", None)
            if switch_db:
                mgr._switch_db(switch_db)
            if args.action == "grant-db":
                mgr.priv_grant_database(args.role_name, args.dbname, args.priv)
            elif args.action == "revoke-db":
                mgr.priv_revoke_database(args.role_name, args.dbname, args.priv)
            elif args.action == "grant-schema":
                mgr.priv_grant_schema(args.role_name, args.schema_name, args.priv)
            elif args.action == "revoke-schema":
                mgr.priv_revoke_schema(args.role_name, args.schema_name, args.priv)
            elif args.action == "grant-table":
                mgr.priv_grant_table(args.role_name, args.schema_name, args.table_name, args.priv)
            elif args.action == "revoke-table":
                mgr.priv_revoke_table(args.role_name, args.schema_name, args.table_name, args.priv)
            elif args.action == "grant-table-regex":
                mgr.priv_grant_tables_by_regex(args.role_name, args.schema_name, args.table_pattern, args.priv)
            elif args.action == "revoke-table-regex":
                mgr.priv_revoke_tables_by_regex(args.role_name, args.schema_name, args.table_pattern, args.priv)
            elif args.action == "set-default":
                mgr.priv_set_default(args.role_name, args.schema_name, args.priv, args.target_role, args.obj_type)
            elif args.action == "revoke-default":
                mgr.priv_revoke_default(args.role_name, args.schema_name, args.priv, args.target_role, args.obj_type)
            elif args.action == "grant-all-tables":
                mgr.priv_grant_all_tables(args.role_name, args.schema_name, args.priv)
            elif args.action == "revoke-all-tables":
                mgr.priv_revoke_all_tables(args.role_name, args.schema_name, args.priv)
            elif args.action == "grant-all-sequences":
                mgr.priv_grant_all_sequences(args.role_name, args.schema_name, args.priv)
            elif args.action == "revoke-all-sequences":
                mgr.priv_revoke_all_sequences(args.role_name, args.schema_name, args.priv)
            elif args.action == "grant-all-functions":
                mgr.priv_grant_all_functions(args.role_name, args.schema_name, args.priv)
            elif args.action == "revoke-all-functions":
                mgr.priv_revoke_all_functions(args.role_name, args.schema_name, args.priv)
            elif args.action == "grant-all-types":
                mgr.priv_grant_all_types(args.role_name, args.schema_name, args.priv)
            elif args.action == "revoke-all-types":
                mgr.priv_revoke_all_types(args.role_name, args.schema_name, args.priv)
            elif args.action == "grant-all-languages":
                mgr.priv_grant_all_languages(args.role_name, args.priv)
            elif args.action == "revoke-all-languages":
                mgr.priv_revoke_all_languages(args.role_name, args.priv)

        # ---- 查询模块 ----
        elif args.module == "query":
            # query 是纯只读操作，dry-run 模式下也应正常连接数据库查询
            if mgr.dry_run:
                mgr.dry_run = False
                mgr._connect()
            switch_db = getattr(args, "switch_db", None)
            if switch_db:
                mgr._switch_db(switch_db)
            if args.action == "user":
                mgr.query_user_permissions(args.username, getattr(args, "table_filter", None), getattr(args, "verbose", False))
            elif args.action == "role":
                mgr.query_role_permissions(args.role_name, getattr(args, "table_filter", None), getattr(args, "verbose", False))

        # ---- 模板模块 ----
        elif args.module == "template":
            if args.action == "backup":
                mgr.template_backup(args.username, args.password, args.dbname, args.owner_role)
            elif args.action == "readonly":
                mgr.template_readonly(args.username, args.password, args.dbname, args.schema, args.owner_role)
            elif args.action == "dml":
                mgr.template_dml(args.username, args.password, args.dbname, args.schema, args.owner_role)
            elif args.action == "readwrite":
                mgr.template_readwrite(args.username, args.password, args.dbname, args.schema, args.owner_role)
            elif args.action == "dba":
                mgr.template_dba(args.username, args.password, args.dbname, args.schema, args.owner_role)
            elif args.action == "apply":
                mgr.template_apply(
                    args.role_name, args.type, args.dbname, args.schema, args.owner_role
                )
            elif args.action == "apply-append":
                mgr.template_apply(
                    args.role_name, args.type, args.dbname, args.schema, args.owner_role,
                    append=True,
                )
            elif args.action == "revoke-all":
                mgr.template_revoke_all(args.role_name, args.dbname, args.schema)

    finally:
        mgr.close()


# ═══════════════════════════════════════════════════════════════
# REPL 交互模式 (prompt_toolkit + rich)
# ═══════════════════════════════════════════════════════════════

class RbacCompleter(Completer):
    """RBAC 命令补全器"""

    COMMANDS = {
        "role": {
            "subcommands": ["list", "create", "drop"],
            "options": ["-c"],
        },
        "user": {
            "subcommands": ["list", "create", "drop", "passwd", "enable", "disable", "grant-role", "revoke-role"],
            "options": ["-p", "-d", "-m", "-c"],
        },
        "priv": {
            "subcommands": [
                "grant-db", "grant-schema", "grant-table", "grant-table-regex",
                "revoke-db", "revoke-schema", "revoke-table", "revoke-table-regex",
                "grant-all-tables", "grant-all-sequences", "grant-all-functions",
                "grant-all-types", "grant-all-languages",
                "revoke-all-tables", "revoke-all-sequences", "revoke-all-functions",
                "revoke-all-types", "revoke-all-languages",
                "set-default", "revoke-default",
            ],
            "options": ["-p", "-d", "-s", "-o", "-T", "-c"],
        },
        "query": {
            "subcommands": ["user", "role"],
            "options": ["-d", "-f", "-v", "-c"],
        },
        "template": {
            "subcommands": ["backup", "readonly", "dml", "readwrite", "dba", "apply", "apply-append", "revoke-all"],
            "options": ["-p", "-d", "-s", "-o", "-t", "-c"],
        },
    }

    BUILTIN = ["help", "exit", "quit", "use", "status", "dry-run"]

    PRIV_HINTS = {
        "grant-db": "CONNECT,CREATE,TEMPORARY",
        "revoke-db": "CONNECT,CREATE,TEMPORARY",
        "grant-schema": "USAGE,CREATE",
        "revoke-schema": "USAGE,CREATE",
        "grant-table": "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
        "revoke-table": "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
        "grant-table-regex": "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
        "revoke-table-regex": "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
        "grant-all-tables": "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
        "revoke-all-tables": "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
        "grant-all-sequences": "USAGE,SELECT,UPDATE",
        "revoke-all-sequences": "USAGE,SELECT,UPDATE",
        "grant-all-functions": "EXECUTE",
        "revoke-all-functions": "EXECUTE",
        "grant-all-types": "USAGE",
        "revoke-all-types": "USAGE",
        "grant-all-languages": "USAGE",
        "revoke-all-languages": "USAGE",
        "set-default": "-T TABLES:SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER | SEQUENCES:USAGE,SELECT,UPDATE | FUNCTIONS:EXECUTE | TYPES:USAGE",
        "revoke-default": "-T TABLES:SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER | SEQUENCES:USAGE,SELECT,UPDATE | FUNCTIONS:EXECUTE | TYPES:USAGE",
    }

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        parts = text.split()
        cursor_in_word = not text.endswith(" ")

        if not parts:
            for cmd in list(self.COMMANDS.keys()) + self.BUILTIN:
                yield Completion(cmd, start_position=0)
            return

        if cursor_in_word:
            current = parts[-1]
            prefix_parts = parts[:-1]
        else:
            current = ""
            prefix_parts = parts

        # 第一级：模块名
        if len(prefix_parts) == 0:
            for cmd in list(self.COMMANDS.keys()) + self.BUILTIN:
                if cmd.startswith(current):
                    yield Completion(cmd, start_position=-len(current))
            return

        module = prefix_parts[0]

        # 内置命令补全
        if module in ("help",):
            for cmd in list(self.COMMANDS.keys()) + self.BUILTIN:
                if cmd.startswith(current):
                    yield Completion(cmd, start_position=-len(current))
            return

        if module not in self.COMMANDS:
            return

        cmd_info = self.COMMANDS[module]

        # 第二级：子命令
        if len(prefix_parts) == 1:
            for sub in cmd_info["subcommands"]:
                if sub.startswith(current):
                    yield Completion(sub, start_position=-len(current))
            for opt in cmd_info["options"]:
                if opt.startswith(current):
                    yield Completion(opt, start_position=-len(current))
            return

        subcommand = prefix_parts[1]

        # 后续：选项补全
        if current.startswith("-"):
            for opt in cmd_info["options"]:
                if opt.startswith(current):
                    yield Completion(opt, start_position=-len(current))
            return

        # -T 选项值补全（支持逗号分隔续补）
        if prefix_parts[-1] == "-T" or (cursor_in_word and "," in current):
            # 检测是否在 -T 的值中（逗号后续补）
            in_T = prefix_parts[-1] == "-T" or (
                len(prefix_parts) >= 2 and prefix_parts[-2] == "-T"
            )
            if in_T:
                for val in ["TABLES", "SEQUENCES", "FUNCTIONS", "TYPES"]:
                    if val.startswith(current.split(",")[-1]):
                        yield Completion(val, start_position=-len(current.split(",")[-1]))
                return

        # -t 选项值补全
        if prefix_parts[-1] == "-t":
            for val in ["readonly", "dml", "readwrite", "dba", "backup"]:
                if val.startswith(current):
                    yield Completion(val, start_position=-len(current))
            return

        # --priv/-p 选项值补全（支持逗号分隔续补）
        # 场景1: -p SELECT  场景2: -p SELECT,INSERT  场景3: -p SELECT,
        in_priv = prefix_parts[-1] in ("-p", "--priv") or (
            len(prefix_parts) >= 2 and prefix_parts[-2] in ("-p", "--priv")
        )
        if in_priv:
            # set-default 特殊处理：根据 -T 类型提示对应权限
            SET_DEFAULT_PRIV_MAP = {
                "TABLES": "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER",
                "SEQUENCES": "USAGE,SELECT,UPDATE",
                "FUNCTIONS": "EXECUTE",
                "TYPES": "USAGE",
            }
            if subcommand in ("set-default", "revoke-default"):
                # 查找已输入的 -T 值
                t_val = None
                for i, p in enumerate(parts):
                    if p == "-T" and i + 1 < len(parts):
                        t_val = parts[i + 1]
                        break
                priv_str = SET_DEFAULT_PRIV_MAP.get(t_val, "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,USAGE,SELECT,UPDATE,EXECUTE,USAGE")
            else:
                key = subcommand
                priv_str = self.PRIV_HINTS.get(key, "")

            if priv_str:
                last_part = current.split(",")[-1] if current else ""
                chosen = set(current.split(",")) if current else set()
                chosen.discard("")
                for priv in priv_str.split(","):
                    priv = priv.strip()
                    if priv.startswith(last_part) and priv not in chosen:
                        yield Completion(priv, start_position=-len(last_part))
            return


class RbacLexer(Lexer):
    """简单语法高亮 — 必须覆盖行中每个字符（含空格），否则光标定位错乱"""

    KEYWORDS = {"role", "user", "priv", "query", "template", "help", "exit", "quit", "use", "status", "dry-run"}
    OPTIONS = {"-c", "-d", "-s", "-p", "-o", "-t", "-T", "-f", "-v", "-m", "--config", "--dbname", "--schema",
               "--password", "--owner-role", "--type", "--obj-type", "--table-filter", "--verbose", "--comment", "--priv", "--target-role"}

    # 匹配: 连续空格 | 连续非空格
    _TOKEN_RE = re.compile(r"(\s+|\S+)")

    def lex_document(self, document):
        def get_line(lineno):
            line = document.lines[lineno]
            tokens = []
            for part in self._TOKEN_RE.findall(line):
                if part.isspace():
                    tokens.append(("", part))
                elif part in self.KEYWORDS:
                    tokens.append(("class:keyword", part))
                elif part in self.OPTIONS or part.startswith("-"):
                    tokens.append(("class:option", part))
                else:
                    tokens.append(("", part))
            return tokens if tokens else []
        return get_line


REPL_STYLE = Style.from_dict({
    "keyword": "#00ffff bold",
    "option": "#ffff00",
    "prompt": "#00ff00 bold",
})


def run_repl(config_path="config.yaml", dry_run=False):
    """启动交互式 REPL"""
    global console
    # 创建管理器（连接数据库）
    try:
        mgr = PgRbacManager(config_path=config_path, dry_run=dry_run)
    except Exception as e:
        console.print(f"[bold red][ERROR] 初始化失败: {e}[/bold red]")
        console.print("[yellow]尝试以 Dry-run 模式启动...[/yellow]")
        try:
            mgr = PgRbacManager(config_path=config_path, dry_run=True)
            dry_run = True
        except Exception as e2:
            console.print(f"[bold red][ERROR] Dry-run 模式也无法启动: {e2}[/bold red]")
            return

    current_db = mgr.config.get("database", {}).get("dbname", "")
    is_dry_run = dry_run

    completer = RbacCompleter()
    history = InMemoryHistory()
    session = PromptSession(
        completer=completer,
        history=history,
        lexer=RbacLexer(),
        style=REPL_STYLE,
    )

    # rich 输出通过 stderr，避免与 prompt_toolkit 的 stdout 渲染冲突
    # 替换全局 console 使 PgRbacManager 方法输出走 stderr
    repl_console = Console(stderr=True, force_terminal=True)
    orig_console = console
    console = repl_console
    mode_label = "Dry-run" if is_dry_run else "Execution"
    print_formatted_text(HTML(
        f"\n<b><style fg='cyan'>PostgreSQL RBAC Shell</style></b>\n"
        f"  Database: <style fg='green'>{current_db}</style>\n"
        f"  Mode:     <style fg='yellow'>{mode_label}</style>\n"
        f"  Type <style fg='cyan'>help</style> for commands, <style fg='cyan'>exit</style> to quit\n"
    ))

    while True:
        try:
            mode_tag = "[DRY]" if is_dry_run else ""
            line = session.prompt(f"pg_rbac[{current_db}]{mode_tag}> ")
        except KeyboardInterrupt:
            continue
        except EOFError:
            break

        line = line.strip()
        if not line:
            continue

        try:
            parts = shlex.split(line)
        except ValueError as e:
            console.print(f"[red]Parse error: {e}[/red]")
            continue

        cmd = parts[0].lower()

        # 内置命令
        if cmd in ("exit", "quit"):
            mgr.close()
            break

        if cmd == "help":
            _print_rich_help(parts[1:] if len(parts) > 1 else [])
            continue

        if cmd == "status":
            mode_str = "Dry-run" if is_dry_run else "Execution"
            print_formatted_text(HTML(
                f"\n<b><style fg='blue'>Status</style></b>\n"
                f"  Database: <style fg='green'>{current_db}</style>\n"
                f"  Mode:     <style fg='yellow'>{mode_str}</style>\n"
                f"  Config:   {config_path}\n"
            ))
            continue

        if cmd == "dry-run":
            is_dry_run = not is_dry_run
            mgr.dry_run = is_dry_run
            if not is_dry_run and mgr.conn is None:
                try:
                    mgr._connect()
                except Exception:
                    console.print("[red]Connection failed, keeping Dry-run mode[/red]")
                    is_dry_run = True
                    mgr.dry_run = True
                    continue
            mode_str = "Dry-run (SQL only)" if is_dry_run else "Execution mode"
            color = "yellow" if is_dry_run else "green"
            print_formatted_text(HTML(f"Mode: <style fg='{color}'>{mode_str}</style>"))
            continue

        if cmd == "use":
            if len(parts) < 2:
                console.print("[red]Usage: use <database>[/red]")
                continue
            new_db = parts[1]
            try:
                if mgr.conn is None:
                    mgr._connect()
                mgr._switch_db(new_db)
                current_db = new_db
                print_formatted_text(HTML(f"Switched to: <style fg='green'>{current_db}</style>"))
            except Exception as e:
                console.print(f"[red]Switch failed: {e}[/red]")
            continue

        # 业务命令 — 复用 main() 中的 dispatch 逻辑
        try:
            _repl_dispatch(mgr, parts, current_db, is_dry_run)
        except SystemExit:
            # argparse 在出错时会调 sys.exit，在 REPL 中需要捕获
            continue
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")



def _repl_dispatch(mgr, parts, current_db, is_dry_run):
    """REPL 命令分发 — 解析参数并调用对应方法"""
    module = parts[0].lower()
    args_parts = parts[1:]

    if module == "role":
        if not args_parts or "-h" in args_parts or "--help" in args_parts:
            _print_rich_help(["role"])
            return
        action = args_parts[0]
        if action == "list":
            mgr.role_list()
        elif action == "create":
            if len(args_parts) < 2:
                console.print("[red]Usage: role create <role> [-m comment][/red]")
                return
            comment = None
            if "-m" in args_parts:
                idx = args_parts.index("-m")
                comment = args_parts[idx + 1] if idx + 1 < len(args_parts) else None
            mgr.role_create(args_parts[1], comment=comment)
        elif action == "drop":
            if len(args_parts) < 2:
                console.print("[red]Usage: role drop <role>[/red]")
                return
            mgr.role_drop(args_parts[1])
        else:
            console.print(f"[red]Unknown subcommand: role {action}[/red]")

    elif module == "user":
        if not args_parts or "-h" in args_parts or "--help" in args_parts:
            _print_rich_help(["user"])
            return
        action = args_parts[0]
        if action == "list":
            mgr.user_list()
        elif action == "create":
            pwd = _get_opt(args_parts, "-p")
            comment = _get_opt(args_parts, "-m")
            if len(args_parts) < 2 or not pwd:
                console.print("[red]Usage: user create <user> -p pwd [-m comment][/red]")
                return
            mgr.user_create(args_parts[1], pwd, comment=comment)
        elif action == "drop":
            if len(args_parts) < 2:
                console.print("[red]Usage: user drop <user>[/red]")
                return
            mgr.user_drop(args_parts[1])
        elif action == "passwd":
            pwd = _get_opt(args_parts, "-p")
            if len(args_parts) < 2 or not pwd:
                console.print("[red]Usage: user passwd <user> -p pwd[/red]")
                return
            mgr.user_change_password(args_parts[1], pwd)
        elif action == "enable":
            if len(args_parts) < 2:
                console.print("[red]Usage: user enable <user>[/red]")
                return
            mgr.user_enable(args_parts[1])
        elif action == "disable":
            if len(args_parts) < 2:
                console.print("[red]Usage: user disable <user>[/red]")
                return
            mgr.user_disable(args_parts[1])
        elif action == "grant-role":
            if len(args_parts) < 3:
                console.print("[red]Usage: user grant-role <user> <role>[/red]")
                return
            mgr.user_grant_role(args_parts[1], args_parts[2])
        elif action == "revoke-role":
            if len(args_parts) < 3:
                console.print("[red]Usage: user revoke-role <user> <role>[/red]")
                return
            mgr.user_revoke_role(args_parts[1], args_parts[2])
        else:
            console.print(f"[red]Unknown subcommand: user {action}[/red]")

    elif module == "priv":
        if not args_parts:
            console.print("[red]Usage: priv grant-*|revoke-*|set-default|revoke-default ...[/red]")
            return
        _repl_dispatch_priv(mgr, args_parts, current_db)

    elif module == "query":
        if not args_parts:
            console.print("[red]Usage: query user|role <name> [-f filter] [-v][/red]")
            return
        _repl_dispatch_query(mgr, args_parts, current_db)

    elif module == "template":
        if not args_parts:
            console.print("[red]Usage: template readonly|dml|readwrite|dba|backup|apply|revoke-all ...[/red]")
            return
        _repl_dispatch_template(mgr, args_parts, current_db)

    else:
        console.print(f"[red]Unknown command: {module}, type help for commands[/red]")


def _get_opt(parts, opt_name, default=None):
    """从参数列表中获取选项值"""
    if opt_name in parts:
        idx = parts.index(opt_name)
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return default


def _repl_dispatch_priv(mgr, args_parts, current_db):
    """REPL priv 子命令分发"""
    # -h/--help 帮助
    if "-h" in args_parts or "--help" in args_parts:
        _print_rich_help(["priv"])
        return

    action = args_parts[0]
    dbname = _get_opt(args_parts, "-d")
    if dbname:
        if mgr.conn is None:
            mgr._connect()
        mgr._switch_db(dbname)
    priv = _get_opt(args_parts, "-p")
    target_role = _get_opt(args_parts, "-o") or _get_opt(args_parts, "--target-role")
    obj_type = _get_opt(args_parts, "-T") or _get_opt(args_parts, "--obj-type") or "TABLES"
    try:
        obj_type = _validate_obj_type(obj_type)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return
    # 位置参数（跳过选项和选项值）
    pos = []
    skip = False
    for i, a in enumerate(args_parts[1:], 1):
        if skip:
            skip = False
            continue
        if a in ("-p", "-d", "-s", "-o", "-T", "--target-role", "--obj-type", "--priv", "--dbname", "--schema", "--owner-role"):
            skip = True
            continue
        if a.startswith("-"):
            continue
        pos.append(a)

    if action == "grant-db":
        if len(pos) < 2:
            console.print("[red]Usage: priv grant-db <role> <db> [-p priv][/red]")
            return
        mgr.priv_grant_database(pos[0], pos[1], priv or "CONNECT")
    elif action == "revoke-db":
        if len(pos) < 2:
            console.print("[red]Usage: priv revoke-db <role> <db> -p priv[/red]")
            return
        mgr.priv_revoke_database(pos[0], pos[1], priv or "ALL")
    elif action == "grant-schema":
        if len(pos) < 2:
            console.print("[red]Usage: priv grant-schema <role> <schema> [-p priv][/red]")
            return
        mgr.priv_grant_schema(pos[0], pos[1], priv or "USAGE")
    elif action == "revoke-schema":
        if len(pos) < 2:
            console.print("[red]Usage: priv revoke-schema <role> <schema> -p priv[/red]")
            return
        mgr.priv_revoke_schema(pos[0], pos[1], priv or "ALL")
    elif action == "grant-table":
        if len(pos) < 3:
            console.print("[red]Usage: priv grant-table <role> <schema> <table> [-p priv][/red]")
            return
        mgr.priv_grant_table(pos[0], pos[1], pos[2], priv or "SELECT")
    elif action == "revoke-table":
        if len(pos) < 3:
            console.print("[red]Usage: priv revoke-table <role> <schema> <table> -p priv[/red]")
            return
        mgr.priv_revoke_table(pos[0], pos[1], pos[2], priv or "ALL")
    elif action == "grant-table-regex":
        if len(pos) < 3:
            console.print("[red]Usage: priv grant-table-regex <role> <schema> <regex> [-p priv][/red]")
            return
        mgr.priv_grant_tables_by_regex(pos[0], pos[1], pos[2], priv or "SELECT")
    elif action == "revoke-table-regex":
        if len(pos) < 3:
            console.print("[red]Usage: priv revoke-table-regex <role> <schema> <regex> -p priv[/red]")
            return
        mgr.priv_revoke_tables_by_regex(pos[0], pos[1], pos[2], priv or "ALL")
    elif action == "grant-all-tables":
        if len(pos) < 2:
            console.print("[red]Usage: priv grant-all-tables <role> <schema> [-p priv][/red]")
            return
        mgr.priv_grant_all_tables(pos[0], pos[1], priv or "SELECT")
    elif action == "revoke-all-tables":
        if len(pos) < 2:
            console.print("[red]Usage: priv revoke-all-tables <role> <schema> -p priv[/red]")
            return
        mgr.priv_revoke_all_tables(pos[0], pos[1], priv or "ALL")
    elif action == "grant-all-sequences":
        if len(pos) < 2:
            console.print("[red]Usage: priv grant-all-sequences <role> <schema> [-p priv][/red]")
            return
        mgr.priv_grant_all_sequences(pos[0], pos[1], priv or "SELECT")
    elif action == "revoke-all-sequences":
        if len(pos) < 2:
            console.print("[red]Usage: priv revoke-all-sequences <role> <schema> -p priv[/red]")
            return
        mgr.priv_revoke_all_sequences(pos[0], pos[1], priv or "ALL")
    elif action == "grant-all-functions":
        if len(pos) < 2:
            console.print("[red]Usage: priv grant-all-functions <role> <schema> [-p priv][/red]")
            return
        mgr.priv_grant_all_functions(pos[0], pos[1], priv or "EXECUTE")
    elif action == "revoke-all-functions":
        if len(pos) < 2:
            console.print("[red]Usage: priv revoke-all-functions <role> <schema> -p priv[/red]")
            return
        mgr.priv_revoke_all_functions(pos[0], pos[1], priv or "ALL")
    elif action == "grant-all-types":
        if len(pos) < 2:
            console.print("[red]Usage: priv grant-all-types <role> <schema> [-p priv][/red]")
            return
        mgr.priv_grant_all_types(pos[0], pos[1], priv or "USAGE")
    elif action == "revoke-all-types":
        if len(pos) < 2:
            console.print("[red]Usage: priv revoke-all-types <role> <schema> -p priv[/red]")
            return
        mgr.priv_revoke_all_types(pos[0], pos[1], priv or "ALL")
    elif action == "grant-all-languages":
        if len(pos) < 1:
            console.print("[red]Usage: priv grant-all-languages <role> [-p priv][/red]")
            return
        mgr.priv_grant_all_languages(pos[0], priv or "USAGE")
    elif action == "revoke-all-languages":
        if len(pos) < 1:
            console.print("[red]Usage: priv revoke-all-languages <role> -p priv[/red]")
            return
        mgr.priv_revoke_all_languages(pos[0], priv or "ALL")
    elif action == "set-default":
        if len(pos) < 2:
            console.print("[red]Usage: priv set-default <role> <schema> [-p priv] [-o owner] [-T type][/red]")
            return
        mgr.priv_set_default(pos[0], pos[1], priv or "SELECT", target_role, obj_type)
    elif action == "revoke-default":
        if len(pos) < 2:
            console.print("[red]Usage: priv revoke-default <role> <schema> -p priv [-o owner] [-T type][/red]")
            return
        if not priv:
            console.print("[red]revoke-default 必须指定 -p 权限[/red]")
            return
        mgr.priv_revoke_default(pos[0], pos[1], priv, target_role, obj_type)
    else:
        console.print(f"[red]Unknown priv subcommand: {action}[/red]")


def _repl_dispatch_query(mgr, args_parts, current_db):
    """REPL query 子命令分发"""
    # -h/--help 帮助
    if "-h" in args_parts or "--help" in args_parts:
        _print_rich_help(["query"])
        return

    # query 是纯只读操作，dry-run 模式下也应正常连接数据库查询，但执行后需恢复 dry-run 状态
    saved_dry_run = mgr.dry_run
    if mgr.dry_run:
        mgr.dry_run = False
        if mgr.conn is None:
            mgr._connect()
    try:
        action = args_parts[0]
        dbname = _get_opt(args_parts, "-d")
        if dbname:
            if mgr.conn is None:
                mgr._connect()
            mgr._switch_db(dbname)
        table_filter = _get_opt(args_parts, "-f")
        verbose = "-v" in args_parts or "--verbose" in args_parts
        # 位置参数（跳过选项和选项值）
        pos = []
        skip = False
        for a in args_parts[1:]:
            if skip:
                skip = False
                continue
            if a in ("-d", "-f", "--dbname", "--table-filter"):
                skip = True
                continue
            if a.startswith("-"):
                continue
            pos.append(a)

        if action == "user":
            if not pos:
                console.print("[red]Usage: query user <name> [-f filter] [-v][/red]")
                return
            mgr.query_principal_permissions(pos[0], table_filter, verbose)
        elif action == "role":
            if not pos:
                console.print("[red]Usage: query role <name> [-f filter] [-v][/red]")
                return
            mgr.query_principal_permissions(pos[0], table_filter, verbose)
        else:
            console.print(f"[red]Unknown query subcommand: {action}[/red]")
    finally:
        mgr.dry_run = saved_dry_run


def _repl_dispatch_template(mgr, args_parts, current_db):
    """REPL template 子命令分发"""
    # -h/--help 帮助
    if "-h" in args_parts or "--help" in args_parts:
        _print_rich_help(["template"])
        return

    action = args_parts[0]
    password = _get_opt(args_parts, "-p")
    dbname = _get_opt(args_parts, "-d")
    schema = _get_opt(args_parts, "-s")
    owner_role = _get_opt(args_parts, "-o")
    perm_type = _get_opt(args_parts, "-t")
    # 位置参数（跳过选项和选项值）
    pos = []
    skip = False
    for i, a in enumerate(args_parts[1:], 1):
        if skip:
            skip = False
            continue
        if a in ("-p", "-d", "-s", "-o", "-t"):
            skip = True
            continue
        if a.startswith("-"):
            continue
        pos.append(a)

    if action == "backup":
        if not pos or not password or not dbname or not owner_role:
            console.print("[red]Usage: template backup <user> -p pwd -d db -o owner[/red]")
            return
        mgr.template_backup(pos[0], password, dbname, owner_role)
    elif action == "readonly":
        if not pos or not password or not dbname or not schema or not owner_role:
            console.print("[red]Usage: template readonly <user> -p pwd -d db -s schema -o owner[/red]")
            return
        mgr.template_readonly(pos[0], password, dbname, schema, owner_role)
    elif action == "dml":
        if not pos or not password or not dbname or not schema or not owner_role:
            console.print("[red]Usage: template dml <user> -p pwd -d db -s schema -o owner[/red]")
            return
        mgr.template_dml(pos[0], password, dbname, schema, owner_role)
    elif action == "readwrite":
        if not pos or not password or not dbname or not schema or not owner_role:
            console.print("[red]Usage: template readwrite <user> -p pwd -d db -s schema -o owner[/red]")
            return
        mgr.template_readwrite(pos[0], password, dbname, schema, owner_role)
    elif action == "dba":
        if not pos or not password or not dbname or not schema or not owner_role:
            console.print("[red]Usage: template dba <user> -p pwd -d db -s schema -o owner[/red]")
            return
        mgr.template_dba(pos[0], password, dbname, schema, owner_role)
    elif action == "apply":
        if not pos or not perm_type or not dbname or not schema or not owner_role:
            console.print("[red]Usage: template apply <role> -t type -d db -s schema -o owner[/red]")
            return
        mgr.template_apply(pos[0], perm_type, dbname, schema, owner_role)
    elif action == "apply-append":
        if not pos or not perm_type or not dbname or not schema or not owner_role:
            console.print("[red]Usage: template apply-append <role> -t type -d db -s schema -o owner[/red]")
            return
        mgr.template_apply(pos[0], perm_type, dbname, schema, owner_role, append=True)
    elif action == "revoke-all":
        if not pos or not dbname:
            console.print("[red]Usage: template revoke-all <role> -d db [-s schema][/red]")
            return
        mgr.template_revoke_all(pos[0], dbname, schema)
    else:
        console.print(f"[red]Unknown template subcommand: {action}[/red]")


if __name__ == "__main__":
    # 无参数或仅 --dry-run/-c → 交互式 REPL；其他 → 传统 CLI
    repl_flags = {"--dry-run", "-c"}
    has_subcommand = any(a for a in sys.argv[1:] if a not in repl_flags and not a.endswith(".yaml"))
    if not has_subcommand:
        dry_run = "--dry-run" in sys.argv
        config_idx = -1
        if "-c" in sys.argv:
            config_idx = sys.argv.index("-c")
        elif "--config" in sys.argv:
            config_idx = sys.argv.index("--config")
        config_path = sys.argv[config_idx + 1] if config_idx >= 0 and config_idx + 1 < len(sys.argv) else "config.yaml"
        run_repl(config_path=config_path, dry_run=dry_run)
    else:
        main()

