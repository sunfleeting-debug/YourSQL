"""阶段 2：语法分析器（递归下降）。

文法见 docs/grammar.md（与本项目根 grammar.md 一致），要点：
  * 表达式优先级：NOT > 比较 > AND > OR，括号内可改变优先级
  * 每条语句以 ';' 结束（缺分号是语法错误）
  * 语法错误输出：位置（行 + 列）+ 实际符号 + 期望符号集合
"""

from __future__ import annotations

from typing import NoReturn, Optional

from database_system.sql_compiler import ast_nodes as ast
from database_system.sql_compiler.lexer import Token
from database_system.utils.constants import (
    BOOL_TYPE,
    COMPARISON_OPS,
    INT_TYPE,
    NULL_TYPE,
    TokenType,
    varchar_type,
    normalize_operator,
)
from database_system.utils.errors import ParseError

STATEMENT_KEYWORDS = ("CREATE", "INSERT", "SELECT", "DELETE", "UPDATE", "DROP", "EXPLAIN")


def _describe(tok: Token) -> str:
    if tok.type == TokenType.EOF:
        return "end of input"
    return f"'{tok.lexeme}'"


def _fmt_expected(expected: set) -> str:
    if not expected:
        return "nothing"
    return "{" + ", ".join(sorted(expected)) + "}"


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0
        self._expected: set[str] = set()
        self.spans: list = []  # 每条语句在 Token 流中的 [起始, 结束] 下标

    # ------------------------------ 基础工具 ------------------------------

    def _peek(self, k: int = 0) -> Token:
        idx = min(self.pos + k, len(self.tokens) - 1)
        return self.tokens[idx]

    def _advance(self) -> Token:
        tok = self._peek()
        if tok.type != TokenType.EOF:
            self.pos += 1
        return tok

    def _at_end(self) -> bool:
        return self._peek().type == TokenType.EOF

    def _check_kw(self, *keywords: str) -> bool:
        tok = self._peek()
        return tok.type == TokenType.KEYWORD and tok.value in keywords

    def _match_kw(self, *keywords: str) -> Optional[Token]:
        if self._check_kw(*keywords):
            return self._advance()
        return None

    def _expect_kw(self, *keywords: str) -> Token:
        if self._check_kw(*keywords):
            return self._advance()
        tok = self._peek()
        self._expected.update(keywords)
        self._fail(tok)

    def _check_op(self, ch: str) -> bool:
        tok = self._peek()
        return tok.type == TokenType.OPERATOR and tok.lexeme == ch

    def _check_delim(self, ch: str) -> bool:
        tok = self._peek()
        return tok.type == TokenType.DELIMITER and tok.lexeme == ch

    def _match_delim(self, ch: str) -> Optional[Token]:
        if self._check_delim(ch):
            return self._advance()
        return None

    def _expect_delim(self, ch: str) -> Token:
        if self._check_delim(ch):
            return self._advance()
        tok = self._peek()
        self._expected.add(f"'{ch}'")
        self._fail(tok)

    def _expect_ident(self) -> Token:
        if self._peek().type == TokenType.IDENTIFIER:
            return self._advance()
        tok = self._peek()
        self._expected.add("identifier")
        self._fail(tok)

    def _expect_int(self) -> Token:
        if self._peek().type == TokenType.INT_CONST:
            return self._advance()
        tok = self._peek()
        self._expected.add("integer")
        self._fail(tok)

    def _fail(self, tok: Token, extra: str = "") -> NoReturn:
        got = _describe(tok)
        expected = _fmt_expected(self._expected)
        msg = f"unexpected {got}, expected {expected}"
        if extra:
            msg += f" ({extra})"
        raise ParseError(msg, tok.line, tok.column)

    # ------------------------------ 入口 ------------------------------

    def parse(self) -> list:
        """解析整个 Token 流，返回语句列表。"""
        stmts = []
        while not self._at_end():
            start = self.pos
            stmt = self._parse_statement()
            self.spans.append((start, self.pos - 1))
            stmts.append(stmt)
        return stmts

    def parse_one(self):
        """解析一条语句（用于交互式逐句解析）。"""
        return self._parse_statement()

    # ------------------------------ 语句 ------------------------------

    def _parse_statement(self):
        self._expected = set()
        if self._check_kw("EXPLAIN"):
            line, column = self._peek().line, self._peek().column
            self._advance()
            return ast.Explain(self._parse_statement(), line=line, column=column)

        if self._check_kw("CREATE"):
            stmt = self._parse_create_table()
        elif self._check_kw("INSERT"):
            stmt = self._parse_insert()
        elif self._check_kw("SELECT"):
            stmt = self._parse_select()
        elif self._check_kw("DELETE"):
            stmt = self._parse_delete()
        elif self._check_kw("UPDATE"):
            stmt = self._parse_update()
        elif self._check_kw("DROP"):
            stmt = self._parse_drop_table()
        else:
            tok = self._peek()
            self._expected.update(STATEMENT_KEYWORDS)
            self._fail(tok)

        self._expect_delim(";")
        return stmt

    # ---- CREATE TABLE ----

    def _parse_create_table(self):
        kw = self._expect_kw("CREATE")
        self._expect_kw("TABLE")
        if_not_exists = False
        if self._match_kw("IF"):
            self._expect_kw("NOT")
            self._expect_kw("EXISTS")
            if_not_exists = True
        name = self._expect_ident()
        self._expect_delim("(")

        columns: list = []
        table_pk: list = []
        while True:
            if self._check_kw("PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"):
                pk = self._parse_table_constraint()
                table_pk.extend(pk)
            else:
                col = self._parse_column_def()
                columns.append(col)
                if col.primary_key:
                    table_pk.append(col.name)
            if self._match_delim(","):
                continue
            break
        self._expect_delim(")")

        pk_lower = {c.lower() for c in table_pk}
        for c in columns:
            if c.name.lower() in pk_lower:
                c.primary_key = True
        return ast.CreateTable(
            name.lexeme, columns, if_not_exists, line=kw.line, column=kw.column
        )

    def _parse_column_def(self):
        name = self._expect_ident()
        line, column = name.line, name.column
        type_name, type_length = self._parse_data_type()
        col = ast.ColumnDef(
            name=name.lexeme,
            type_name=type_name,
            type_length=type_length,
            line=line,
            column=column,
        )
        # 列级约束
        while True:
            if self._match_kw("PRIMARY"):
                self._expect_kw("KEY")
                col.primary_key = True
            elif self._match_kw("NOT"):
                self._expect_kw("NULL")
                col.not_null = True
            elif self._match_kw("NULL"):
                col.not_null = False
            elif self._match_kw("UNIQUE"):
                col.unique = True
            elif self._match_kw("DEFAULT"):
                col.default = self._parse_literal()
            else:
                break
        return col

    def _parse_data_type(self):
        tok = self._peek()
        if self._match_kw("INT"):
            return "INT", 0
        if self._match_kw("VARCHAR"):
            length = 0
            if self._match_delim("("):
                length = int(self._expect_int().value)
                self._expect_delim(")")
            return "VARCHAR", length
        if self._match_kw("BOOL", "BOOLEAN"):
            return "BOOL", 0
        self._expected.update(("INT", "VARCHAR", "BOOL"))
        self._fail(tok, "data type")

    def _parse_table_constraint(self):
        """解析表级约束，返回主键列名列表（不支持的约束仅跳过，不影响主流程）。"""
        if self._match_kw("PRIMARY"):
            self._expect_kw("KEY")
            self._expect_delim("(")
            cols = [self._expect_ident().lexeme]
            while self._match_delim(","):
                cols.append(self._expect_ident().lexeme)
            self._expect_delim(")")
            return cols
        if self._match_kw("UNIQUE"):
            self._skip_paren_group()
            return []
        if self._match_kw("FOREIGN"):
            self._expect_kw("KEY")
            self._skip_paren_group()
            while not self._check_delim(",") and not self._check_delim(")"):
                if self._check_delim("("):
                    self._skip_paren_group()
                else:
                    self._advance()
            return []
        # CHECK / CONSTRAINT 等：整体跳过
        while not self._check_delim(",") and not self._check_delim(")"):
            if self._check_delim("("):
                self._skip_paren_group()
            else:
                self._advance()
        return []

    def _skip_paren_group(self):
        """跳过一个括号包围的 token 分组（深度计数）。"""
        self._expect_delim("(")
        depth = 1
        while depth > 0:
            tok = self._peek()
            if tok.type == TokenType.EOF:
                self._expected.add("')'")
                self._fail(tok)
            if self._check_delim("("):
                depth += 1
            elif self._check_delim(")"):
                depth -= 1
            self._advance()

    # ---- INSERT ----

    def _parse_insert(self):
        kw = self._expect_kw("INSERT")
        self._expect_kw("INTO")
        table = self._expect_ident()
        columns = None
        if self._check_delim("("):
            self._advance()
            columns = [self._expect_ident().lexeme]
            while self._match_delim(","):
                columns.append(self._expect_ident().lexeme)
            self._expect_delim(")")
        self._expect_kw("VALUES")
        rows = []
        while True:
            self._expect_delim("(")
            values = [self._parse_expression()]
            while self._match_delim(","):
                values.append(self._parse_expression())
            self._expect_delim(")")
            rows.append(values)
            if self._match_delim(","):
                continue
            break
        return ast.Insert(table.lexeme, columns, rows, line=kw.line, column=kw.column)

    # ---- SELECT ----

    def _parse_select(self):
        kw = self._expect_kw("SELECT")
        distinct = bool(self._match_kw("DISTINCT"))
        items = []
        while True:
            if self._check_op("*"):
                tok = self._advance()
                items.append(ast.SelectItem(ast.Star(line=tok.line, column=tok.column)))
            else:
                expr = self._parse_expression()
                alias = None
                if self._match_kw("AS"):
                    alias = self._expect_ident().lexeme
                elif self._peek().type == TokenType.IDENTIFIER:
                    alias = self._advance().lexeme
                items.append(ast.SelectItem(expr, alias))
            if self._match_delim(","):
                continue
            break

        self._expect_kw("FROM")
        table = self._expect_ident()
        alias = None
        if self._match_kw("AS"):
            alias = self._expect_ident().lexeme
        elif self._peek().type == TokenType.IDENTIFIER:
            alias = self._advance().lexeme

        where = None
        if self._match_kw("WHERE"):
            where = self._parse_expression()

        order_by = []
        if self._match_kw("ORDER"):
            self._expect_kw("BY")
            while True:
                key_expr = self._parse_expression()
                desc = False
                if self._match_kw("DESC"):
                    desc = True
                else:
                    self._match_kw("ASC")
                order_by.append(ast.OrderKey(key_expr, desc))
                if self._match_delim(","):
                    continue
                break

        limit = None
        if self._match_kw("LIMIT"):
            tok = self._expect_int()
            limit = int(tok.value)
        if self._match_kw("OFFSET"):
            self._expect_int()  # 解析但暂不实现语义，保证不误报语法错误

        return ast.Select(
            items=items,
            from_table=table.lexeme,
            from_alias=alias,
            where=where,
            distinct=distinct,
            order_by=order_by,
            limit=limit,
            line=kw.line,
            column=kw.column,
        )

    # ---- DELETE / UPDATE / DROP ----

    def _parse_delete(self):
        kw = self._expect_kw("DELETE")
        self._expect_kw("FROM")
        table = self._expect_ident()
        where = None
        if self._match_kw("WHERE"):
            where = self._parse_expression()
        return ast.Delete(table.lexeme, where, line=kw.line, column=kw.column)

    def _parse_update(self):
        kw = self._expect_kw("UPDATE")
        table = self._expect_ident()
        self._expect_kw("SET")
        assignments = []
        while True:
            col = self._expect_ident()
            self._expect_operator("=")
            expr = self._parse_expression()
            assignments.append((col.lexeme, expr))
            if self._match_delim(","):
                continue
            break
        where = None
        if self._match_kw("WHERE"):
            where = self._parse_expression()
        return ast.Update(table.lexeme, assignments, where, line=kw.line, column=kw.column)

    def _parse_drop_table(self):
        kw = self._expect_kw("DROP")
        self._expect_kw("TABLE")
        if_exists = bool(self._match_kw("IF") and self._expect_kw("EXISTS"))
        name = self._expect_ident()
        return ast.DropTable(name.lexeme, if_exists, line=kw.line, column=kw.column)

    # ------------------------------ 表达式 ------------------------------

    def _expect_operator(self, op: str) -> Token:
        tok = self._peek()
        if tok.type == TokenType.OPERATOR and tok.lexeme == op:
            return self._advance()
        self._expected.add(f"'{op}'")
        self._fail(tok)

    def _parse_expression(self):
        return self._parse_or()

    def _parse_or(self):
        node = self._parse_and()
        while self._match_kw("OR"):
            op_tok = self.tokens[self.pos - 1]
            right = self._parse_and()
            node = ast.Binary("OR", node, right, line=op_tok.line, column=op_tok.column)
        return node

    def _parse_and(self):
        node = self._parse_not()
        while self._match_kw("AND"):
            op_tok = self.tokens[self.pos - 1]
            right = self._parse_not()
            node = ast.Binary("AND", node, right, line=op_tok.line, column=op_tok.column)
        return node

    def _parse_not(self):
        if self._check_kw("NOT"):
            tok = self._advance()
            operand = self._parse_not()
            return ast.Unary("NOT", operand, line=tok.line, column=tok.column)
        return self._parse_comparison()

    def _parse_comparison(self):
        node = self._parse_additive()
        while True:
            tok = self._peek()
            if tok.type == TokenType.OPERATOR and tok.lexeme in COMPARISON_OPS:
                self._advance()
                right = self._parse_not()
                node = ast.Binary(
                    normalize_operator(tok.lexeme), node, right,
                    line=tok.line, column=tok.column,
                )
                continue
            if self._check_kw("IS"):
                self._advance()
                negated = bool(self._match_kw("NOT"))
                self._expect_kw("NULL")
                node = ast.IsNull(node, negated, line=tok.line, column=tok.column)
                continue
            break
        return node

    def _parse_additive(self):
        node = self._parse_multiplicative()
        while True:
            tok = self._peek()
            if tok.type == TokenType.OPERATOR and tok.lexeme in ("+", "-"):
                self._advance()
                right = self._parse_multiplicative()
                node = ast.Binary(tok.lexeme, node, right, line=tok.line, column=tok.column)
            else:
                break
        return node

    def _parse_multiplicative(self):
        node = self._parse_unary()
        while True:
            tok = self._peek()
            if tok.type == TokenType.OPERATOR and tok.lexeme in ("*", "/"):
                self._advance()
                right = self._parse_unary()
                node = ast.Binary(tok.lexeme, node, right, line=tok.line, column=tok.column)
            else:
                break
        return node

    def _parse_unary(self):
        tok = self._peek()
        if tok.type == TokenType.OPERATOR and tok.lexeme in ("-", "+"):
            self._advance()
            operand = self._parse_unary()
            return ast.Unary(tok.lexeme, operand, line=tok.line, column=tok.column)
        return self._parse_primary()

    def _parse_primary(self):
        tok = self._peek()
        if tok.type == TokenType.INT_CONST:
            self._advance()
            return ast.Literal(tok.value, INT_TYPE, line=tok.line, column=tok.column)
        if tok.type == TokenType.STRING_CONST:
            self._advance()
            return ast.Literal(tok.value, varchar_type(), line=tok.line, column=tok.column)
        if self._match_kw("TRUE"):
            return ast.Literal(True, BOOL_TYPE, line=tok.line, column=tok.column)
        if self._match_kw("FALSE"):
            return ast.Literal(False, BOOL_TYPE, line=tok.line, column=tok.column)
        if self._match_kw("NULL"):
            return ast.Literal(None, NULL_TYPE, line=tok.line, column=tok.column)
        if tok.type == TokenType.IDENTIFIER:
            self._advance()
            if self._check_delim("."):
                self._advance()
                right = self._expect_ident()
                return ast.Identifier(
                    right.lexeme, tok.lexeme, line=tok.line, column=tok.column
                )
            return ast.Identifier(tok.lexeme, None, line=tok.line, column=tok.column)
        if self._check_delim("("):
            self._advance()
            node = self._parse_expression()
            self._expect_delim(")")
            return node
        self._expected.update(("expression", "identifier", "literal", "'('"))
        self._fail(tok)

    def _parse_literal(self):
        """DEFAULT 后面的字面量：整数 / 字符串 / TRUE / FALSE / NULL / 负整数。"""
        tok = self._peek()
        if tok.type == TokenType.OPERATOR and tok.lexeme in ("-", "+"):
            self._advance()
            num = self._expect_int()
            value = -num.value if tok.lexeme == "-" else num.value
            return ast.Literal(value, INT_TYPE, line=tok.line, column=tok.column)
        if tok.type == TokenType.INT_CONST:
            self._advance()
            return ast.Literal(tok.value, INT_TYPE, line=tok.line, column=tok.column)
        if tok.type == TokenType.STRING_CONST:
            self._advance()
            return ast.Literal(tok.value, varchar_type(), line=tok.line, column=tok.column)
        if self._match_kw("TRUE"):
            return ast.Literal(True, BOOL_TYPE, line=tok.line, column=tok.column)
        if self._match_kw("FALSE"):
            return ast.Literal(False, BOOL_TYPE, line=tok.line, column=tok.column)
        if self._match_kw("NULL"):
            return ast.Literal(None, NULL_TYPE, line=tok.line, column=tok.column)
        self._expected.update(("literal", "integer", "string"))
        self._fail(tok)


def parse(sql: str):
    """便捷入口：SQL 文本 -> AST 语句列表。"""
    from database_system.sql_compiler.lexer import tokenize

    return Parser(tokenize(sql)).parse()


def parse_tokens(tokens: list) -> list:
    """便捷入口：Token 流 -> AST 语句列表。"""
    return Parser(tokens).parse()
