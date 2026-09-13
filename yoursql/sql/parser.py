"""SQL 子集递归下降解析器。"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable, TypeVar

from ..common.errors import BinderError, ParserError
from ..common.types import DataType
from .ast import (
    BetweenPredicate,
    BinaryOp,
    ColumnDefinition,
    ColumnRef,
    CreateRole,
    CreateIndex,
    CreateTable,
    CreateView,
    CreateUser,
    Delete,
    DropIndex,
    DropTable,
    DropView,
    Explain,
    Expr,
    FunctionCall,
    Grant,
    InPredicate,
    Insert,
    IsNull,
    JoinClause,
    Literal,
    Node,
    OrderItem,
    Parameter,
    Revoke,
    Select,
    SelectItem,
    Show,
    ShowGrants,
    Star,
    Statement,
    Subquery,
    TableRef,
    UnaryOp,
    Update,
)
from .lexer import Lexer, Token, TokenKind


NodeType = TypeVar("NodeType", bound=Node)


class Parser:
    """将 Token 流转换为 AST，并在错误中保留出错位置。"""

    def __init__(self, source_or_tokens: str | Iterable[Token]) -> None:
        self.tokens = Lexer(source_or_tokens).tokenize() if isinstance(source_or_tokens, str) else list(source_or_tokens)
        if not self.tokens or self.tokens[-1].kind is not TokenKind.EOF:
            self.tokens.append(Token(TokenKind.EOF, "", self.tokens[-1].line if self.tokens else 1, self.tokens[-1].column if self.tokens else 1))
        self.position = 0
        self._in_insert_values = False

    def parse_one(self) -> Statement:
        statement = self._statement()
        self._match(TokenKind.SEMICOLON)
        self._expect(TokenKind.EOF, "语句结束")
        return statement

    def parse_script(self) -> list[Statement]:
        statements: list[Statement] = []
        while not self._at(TokenKind.EOF):
            statements.append(self._statement())
            if self._match(TokenKind.SEMICOLON):
                while self._at(TokenKind.SEMICOLON):
                    self._advance()
            elif not self._at(TokenKind.EOF):
                self._error("多语句脚本中的语句必须以分号分隔", "';'")
        return statements

    def _statement(self) -> Statement:
        kind = self._current().kind
        if kind is TokenKind.CREATE:
            return self._create()
        if kind is TokenKind.GRANT:
            return self._grant()
        if kind is TokenKind.REVOKE:
            return self._revoke()
        if kind is TokenKind.DROP:
            return self._drop()
        if kind is TokenKind.INSERT:
            return self._insert()
        if kind is TokenKind.SELECT:
            return self._select()
        if kind is TokenKind.UPDATE:
            return self._update()
        if kind is TokenKind.DELETE:
            return self._delete()
        if kind is TokenKind.EXPLAIN:
            token = self._advance()
            return self._located(Explain(self._statement()), token)
        if kind is TokenKind.SHOW:
            return self._show()
        if kind in {TokenKind.DESC, TokenKind.DESCRIBE}:
            self._advance()
            name, token = self._name_with_token("表名")
            return self._located(Show("COLUMNS", name), token)
        self._error("不支持的语句起始符号", "CREATE/INSERT/SELECT/UPDATE/DELETE")

    def _show(self) -> Statement:
        show_token = self._expect(TokenKind.SHOW, "SHOW")
        if self._match(TokenKind.GRANTS):
            if self._match(TokenKind.FOR):
                target_kind, target_name, target_token = self._principal_with_token()
                statement = self._located(ShowGrants(target_kind, target_name), show_token)
                statement.with_named_source_location("target", target_token.line, target_token.column)
                return statement
            return self._located(ShowGrants(), show_token)
        if self._match(TokenKind.TABLES):
            return self._located(Show("TABLES"), show_token)
        if self._match(TokenKind.VIEWS):
            return self._located(Show("VIEWS"), show_token)
        if self._match(TokenKind.TABLE):
            return self._located(Show("TABLE"), show_token)
        if self._match(TokenKind.COLUMNS) or self._match(TokenKind.FIELDS):
            object_name, object_token = self._show_object_name("SHOW COLUMNS")
            return self._located(Show("COLUMNS", object_name), object_token)
        if self._match(TokenKind.INDEX) or self._match(TokenKind.INDEXES):
            object_name, object_token = self._show_object_name("SHOW INDEX")
            return self._located(Show("INDEX", object_name), object_token)
        if self._match(TokenKind.CREATE):
            if self._match(TokenKind.TABLE):
                name, token = self._name_with_token("表名")
                return self._located(Show("CREATE_TABLE", name), token)
            if self._match(TokenKind.VIEW):
                name, token = self._name_with_token("视图名")
                return self._located(Show("CREATE_VIEW", name), token)
            self._error("SHOW CREATE 后需要 TABLE 或 VIEW", "TABLE/VIEW")
        self._error("SHOW 后需要对象类型", "TABLES/VIEWS/COLUMNS/INDEX/CREATE TABLE/CREATE VIEW/GRANTS")

    def _show_object_name(self, context: str) -> tuple[str, Token]:
        if not (self._match(TokenKind.FROM) or self._match(TokenKind.IN)):
            self._error(f"{context} 后需要 FROM 或 IN", "FROM/IN")
        return self._name_with_token("表名")

    def _create(self) -> Statement:
        self._expect(TokenKind.CREATE, "CREATE")
        if self._match(TokenKind.ROLE):
            name, token = self._name_with_token("角色名")
            return self._located(CreateRole(name), token)
        if self._match(TokenKind.USER):
            return self._create_user()
        unique = self._match(TokenKind.UNIQUE)
        if self._match(TokenKind.TABLE):
            return self._create_table()
        if self._match(TokenKind.VIEW):
            return self._create_view()
        if self._match(TokenKind.INDEX):
            return self._create_index(unique)
        self._error("CREATE 后需要 TABLE、VIEW 或 INDEX", "TABLE/VIEW/INDEX")

    def _create_view(self) -> CreateView:
        """解析 CREATE VIEW name AS SELECT ...，只允许保存查询定义。"""

        if_not_exists = self._if_not_exists()
        name, name_token = self._name_with_token("视图名")
        self._expect(TokenKind.AS, "AS")
        start = self.position
        query = self._select()
        # HOW：Token 不保留注释，但保留词素；用空格重建可再次解析的规范定义。
        definition_sql = " ".join(token.lexeme for token in self.tokens[start:self.position])
        return self._located(CreateView(name, query, definition_sql, if_not_exists), name_token)

    def _create_user(self) -> CreateUser:
        name, name_token = self._name_with_token("用户名")
        self._expect(TokenKind.IDENTIFIED, "IDENTIFIED")
        self._expect(TokenKind.BY, "BY")
        password = self._current()
        if password.kind is not TokenKind.STRING:
            self._error("用户密码必须是字符串", "STRING")
        self._advance()
        roles: list[str] = []
        if self._match(TokenKind.DEFAULT):
            self._expect(TokenKind.ROLE, "ROLE")
            roles.append(self._name("角色名"))
            while self._match(TokenKind.COMMA):
                roles.append(self._name("角色名"))
        return self._located(CreateUser(name, str(password.literal), tuple(roles)), name_token)

    def _grant(self) -> Grant:
        token = self._expect(TokenKind.GRANT, "GRANT")
        privileges = self._privilege_list()
        object_name = self._privilege_object()
        self._expect(TokenKind.TO, "TO")
        target_kind, target_name, target_token = self._principal_with_token()
        statement = self._located(Grant(privileges, object_name, target_kind, target_name), token)
        statement.with_named_source_location("target", target_token.line, target_token.column)
        return statement

    def _revoke(self) -> Revoke:
        token = self._expect(TokenKind.REVOKE, "REVOKE")
        privileges = self._privilege_list()
        object_name = self._privilege_object()
        self._expect(TokenKind.FROM, "FROM")
        target_kind, target_name, target_token = self._principal_with_token()
        statement = self._located(Revoke(privileges, object_name, target_kind, target_name), token)
        statement.with_named_source_location("target", target_token.line, target_token.column)
        return statement

    def _privilege_list(self) -> tuple[str, ...]:
        privileges = [self._privilege()]
        while self._match(TokenKind.COMMA):
            privileges.append(self._privilege())
        return tuple(privileges)

    def _privilege(self) -> str:
        token = self._current()
        if token.kind is TokenKind.STAR:
            self._advance()
            return "ALL"
        if token.kind in {
            TokenKind.ALL,
            TokenKind.SELECT,
            TokenKind.INSERT,
            TokenKind.UPDATE,
            TokenKind.DELETE,
            TokenKind.CREATE,
            TokenKind.DROP,
            TokenKind.IDENTIFIER,
            TokenKind.QUOTED_IDENTIFIER,
        }:
            self._advance()
            return str(token.literal if token.kind is TokenKind.QUOTED_IDENTIFIER else token.lexeme).upper()
        self._error("GRANT/REVOKE 后需要权限名", "SELECT/INSERT/UPDATE/DELETE/ALL")

    def _privilege_object(self) -> str | None:
        self._expect(TokenKind.ON, "ON")
        if self._match(TokenKind.STAR):
            return None
        self._match(TokenKind.TABLE)
        return self._name("表名")

    def _principal(self) -> tuple[str, str]:
        target_kind, target_name, _token = self._principal_with_token()
        return target_kind, target_name

    def _principal_with_token(self) -> tuple[str, str, Token]:
        if self._match(TokenKind.USER):
            name, token = self._name_with_token("用户名")
            return "USER", name, token
        if self._match(TokenKind.ROLE):
            name, token = self._name_with_token("角色名")
            return "ROLE", name, token
        name, token = self._name_with_token("用户名")
        return "USER", name, token

    def _create_table(self) -> CreateTable:
        if_not_exists = self._if_not_exists()
        name, name_token = self._name_with_token("表名")
        self._expect(TokenKind.LPAREN, "'('")
        columns: list[ColumnDefinition] = []
        table_primary: list[str] = []
        table_unique: list[str] = []
        primary_tokens: list[Token] = []
        unique_tokens: list[Token] = []
        while True:
            if self._match(TokenKind.PRIMARY):
                self._expect(TokenKind.KEY, "KEY")
                named_columns = self._name_list_with_tokens()
                table_primary.extend(item[0] for item in named_columns)
                primary_tokens.extend(item[1] for item in named_columns)
            elif self._match(TokenKind.UNIQUE):
                named_columns = self._name_list_with_tokens()
                table_unique.extend(item[0] for item in named_columns)
                unique_tokens.extend(item[1] for item in named_columns)
            else:
                columns.append(self._column_definition())
            if not self._match(TokenKind.COMMA):
                break
        self._expect(TokenKind.RPAREN, "')'")
        if table_primary:
            primary_set = {name.lower() for name in table_primary}
            columns = [self._replace_column(column, primary_key=True, nullable=False) if column.name.lower() in primary_set else column for column in columns]
        if table_unique:
            unique_set = {name.lower() for name in table_unique}
            columns = [self._replace_column(column, unique=True) if column.name.lower() in unique_set else column for column in columns]
        statement = self._located(CreateTable(name, tuple(columns), if_not_exists), name_token)
        for index, token in enumerate(primary_tokens):
            statement.with_named_source_location(f"primary:{index}", token.line, token.column)
        for index, token in enumerate(unique_tokens):
            statement.with_named_source_location(f"unique:{index}", token.line, token.column)
        return statement

    def _column_definition(self) -> ColumnDefinition:
        name, name_token = self._name_with_token("列名")
        type_token = self._current()
        if type_token.kind not in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
            self._error("列定义需要类型", "INT/VARCHAR/FLOAT/BOOLEAN")
        self._advance()
        try:
            data_type = DataType.parse(type_token.literal if type_token.kind is TokenKind.QUOTED_IDENTIFIER else type_token.lexeme)
        except BinderError as exc:
            raise BinderError(exc.message, line=type_token.line, column=type_token.column, **exc.details) from exc
        if self._match(TokenKind.LPAREN):
            if not self._at(TokenKind.INTEGER):
                self._error("类型长度需要整数", "INTEGER")
            self._advance()
            self._expect(TokenKind.RPAREN, "')'")
        nullable = True
        primary_key = False
        unique = False
        default: Expr | None = None
        while True:
            if self._match(TokenKind.PRIMARY):
                self._expect(TokenKind.KEY, "KEY")
                primary_key, nullable = True, False
            elif self._match(TokenKind.UNIQUE):
                unique = True
            elif self._match(TokenKind.NOT):
                self._expect(TokenKind.NULL, "NULL")
                nullable = False
            elif self._match(TokenKind.DEFAULT):
                default = self._expression()
            else:
                break
        return self._located(ColumnDefinition(name, data_type, nullable, primary_key, unique, default), name_token)

    def _create_index(self, unique: bool) -> CreateIndex:
        if_not_exists = self._if_not_exists()
        name, name_token = self._name_with_token("索引名")
        self._expect(TokenKind.ON, "ON")
        table, table_token = self._name_with_token("表名")
        named_columns = self._name_list_with_tokens()
        statement = self._located(CreateIndex(name, table, tuple(item[0] for item in named_columns), unique, if_not_exists), name_token)
        statement.with_named_source_location("table", table_token.line, table_token.column)
        for index, (_column, token) in enumerate(named_columns):
            statement.with_named_source_location(f"column:{index}", token.line, token.column)
        return statement

    def _drop(self) -> Statement:
        self._expect(TokenKind.DROP, "DROP")
        if self._match(TokenKind.TABLE):
            if_exists = self._if_exists()
            name, token = self._name_with_token("表名")
            return self._located(DropTable(name, if_exists), token)
        if self._match(TokenKind.VIEW):
            if_exists = self._if_exists()
            name, token = self._name_with_token("视图名")
            return self._located(DropView(name, if_exists), token)
        if self._match(TokenKind.INDEX):
            if_exists = self._if_exists()
            name, token = self._name_with_token("索引名")
            return self._located(DropIndex(name, if_exists), token)
        self._error("DROP 后需要 TABLE、VIEW 或 INDEX", "TABLE/VIEW/INDEX")

    def _insert(self) -> Insert:
        self._expect(TokenKind.INSERT, "INSERT")
        self._match(TokenKind.INTO)
        table, table_token = self._name_with_token("表名")
        named_columns = self._name_list_with_tokens() if self._at(TokenKind.LPAREN) else []
        columns = tuple(item[0] for item in named_columns)
        self._expect(TokenKind.VALUES, "VALUES")
        values: list[tuple[Expr, ...]] = []
        row_tokens: list[Token] = []
        self._in_insert_values = True
        try:
            while True:
                row_token = self._current()
                self._expect(TokenKind.LPAREN, "'('")
                row_tokens.append(row_token)
                row: list[Expr] = []
                if not self._at(TokenKind.RPAREN):
                    while True:
                        row.append(self._expression())
                        if not self._match(TokenKind.COMMA):
                            break
                self._expect(TokenKind.RPAREN, "')'")
                values.append(tuple(row))
                if not self._match(TokenKind.COMMA):
                    break
        finally:
            self._in_insert_values = False
        statement = self._located(Insert(table, tuple(values), columns), table_token)
        statement.with_named_source_location("table", table_token.line, table_token.column)
        for index, (_column, token) in enumerate(named_columns):
            statement.with_named_source_location(f"column:{index}", token.line, token.column)
        for index, token in enumerate(row_tokens):
            statement.with_named_source_location(f"row:{index}", token.line, token.column)
        return statement

    def _select(self) -> Select:
        first = self._select_core()
        if self._match(TokenKind.UNION):
            union_all = self._match(TokenKind.ALL)
            self._expect(TokenKind.SELECT, "SELECT")
            return replace(first, union=self._select_core(already_consumed_select=True), union_all=union_all)
        return first

    def _select_core(self, *, already_consumed_select: bool = False) -> Select:
        select_token = self.tokens[self.position - 1] if already_consumed_select else self._current()
        if not already_consumed_select:
            self._expect(TokenKind.SELECT, "SELECT")
        distinct = self._match(TokenKind.DISTINCT)
        items: list[SelectItem] = []
        while True:
            expression = self._expression()
            alias: str | None = None
            if self._match(TokenKind.AS):
                alias = self._name("别名")
            elif self._current().kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
                alias = self._name("别名")
            item = SelectItem(expression, alias)
            if expression.source_location is not None:
                item.with_source_location(*expression.source_location)
            items.append(item)
            if not self._match(TokenKind.COMMA):
                break
        from_table: TableRef | None = None
        joins: list[JoinClause] = []
        if self._match(TokenKind.FROM):
            from_table = self._table_ref()
            while True:
                if self._match(TokenKind.COMMA):
                    joins.append(JoinClause("CROSS", self._table_ref(), None))
                    continue
                join_type = self._join_type()
                if join_type is None:
                    break
                table = self._table_ref()
                on = None
                if self._match(TokenKind.ON):
                    on = self._expression()
                elif join_type != "CROSS":
                    self._error("JOIN 需要 ON 条件", "ON")
                joins.append(JoinClause(join_type, table, on))
        where = self._expression() if self._match(TokenKind.WHERE) else None
        group_by: list[Expr] = []
        if self._match(TokenKind.GROUP):
            self._expect(TokenKind.BY, "BY")
            group_by = self._expression_list()
        having = self._expression() if self._match(TokenKind.HAVING) else None
        order_by: list[OrderItem] = []
        if self._match(TokenKind.ORDER):
            self._expect(TokenKind.BY, "BY")
            while True:
                expression = self._expression()
                descending = self._match(TokenKind.DESC)
                if not descending:
                    self._match(TokenKind.ASC)
                nulls_first: bool | None = None
                if self._match(TokenKind.NULLS):
                    nulls_first = self._match(TokenKind.FIRST)
                    if nulls_first is False:
                        self._expect(TokenKind.LAST, "FIRST/LAST")
                        nulls_first = False
                order_by.append(OrderItem(expression, descending, nulls_first))
                if not self._match(TokenKind.COMMA):
                    break
        limit = None
        if self._match(TokenKind.LIMIT):
            limit = self._integer_literal("LIMIT")
        offset = 0
        if self._match(TokenKind.OFFSET):
            offset = self._integer_literal("OFFSET")
        return self._located(Select(tuple(items), from_table, tuple(joins), where, tuple(group_by), having, tuple(order_by), limit, offset, distinct), select_token)

    def _join_type(self) -> str | None:
        if self._match(TokenKind.JOIN):
            return "INNER"
        for kind, name in ((TokenKind.INNER, "INNER"), (TokenKind.LEFT, "LEFT"), (TokenKind.RIGHT, "RIGHT"), (TokenKind.FULL, "FULL")):
            if self._match(kind):
                self._match(TokenKind.OUTER)
                self._expect(TokenKind.JOIN, "JOIN")
                return name
        return None

    def _table_ref(self) -> TableRef:
        name_token = self._current()
        name = self._name("表名")
        alias = None
        if self._match(TokenKind.AS):
            alias = self._name("表别名")
        elif self._current().kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
            alias = self._name("表别名")
        return self._located(TableRef(name, alias), name_token)

    def _update(self) -> Update:
        self._expect(TokenKind.UPDATE, "UPDATE")
        table, table_token = self._name_with_token("表名")
        self._expect(TokenKind.SET, "SET")
        assignments: list[tuple[str, Expr]] = []
        assignment_tokens: list[Token] = []
        while True:
            name, name_token = self._name_with_token("列名")
            self._expect(TokenKind.EQ, "'='")
            assignments.append((name, self._expression()))
            assignment_tokens.append(name_token)
            if not self._match(TokenKind.COMMA):
                break
        where = self._expression() if self._match(TokenKind.WHERE) else None
        statement = self._located(Update(table, tuple(assignments), where), table_token)
        statement.with_named_source_location("table", table_token.line, table_token.column)
        for index, token in enumerate(assignment_tokens):
            statement.with_named_source_location(f"assignment:{index}", token.line, token.column)
        return statement

    def _delete(self) -> Delete:
        self._expect(TokenKind.DELETE, "DELETE")
        self._expect(TokenKind.FROM, "FROM")
        table, table_token = self._name_with_token("表名")
        where = self._expression() if self._match(TokenKind.WHERE) else None
        statement = self._located(Delete(table, where), table_token)
        statement.with_named_source_location("table", table_token.line, table_token.column)
        return statement

    def _expression(self) -> Expr:
        return self._or()

    def _or(self) -> Expr:
        expression = self._and()
        while self._at(TokenKind.OR):
            operator = self._advance()
            expression = self._located(BinaryOp(expression, "OR", self._and()), operator)
        return expression

    def _and(self) -> Expr:
        expression = self._not()
        while self._at(TokenKind.AND):
            operator = self._advance()
            expression = self._located(BinaryOp(expression, "AND", self._not()), operator)
        return expression

    def _not(self) -> Expr:
        if self._match(TokenKind.NOT):
            return self._located(UnaryOp("NOT", self._not()), self.tokens[self.position - 1])
        return self._comparison()

    def _comparison(self) -> Expr:
        expression = self._additive()
        if self._at(TokenKind.IS):
            operator = self._advance()
            negated = self._match(TokenKind.NOT)
            self._expect(TokenKind.NULL, "NULL")
            return self._located(IsNull(expression, negated), operator)
        negated = False
        if self._at(TokenKind.NOT) and self._peek(1).kind in {TokenKind.IN, TokenKind.BETWEEN, TokenKind.LIKE}:
            self._advance()
            negated = True
        if self._at(TokenKind.IN):
            operator = self._advance()
            self._expect(TokenKind.LPAREN, "'('")
            if self._at(TokenKind.SELECT):
                subquery_token = self._current()
                values = [self._located(Subquery(self._select()), subquery_token)]
            else:
                values = self._expression_list(allow_empty=False)
            self._expect(TokenKind.RPAREN, "')'")
            return self._located(InPredicate(expression, tuple(values), negated), operator)
        if self._at(TokenKind.BETWEEN):
            operator = self._advance()
            lower = self._additive()
            self._expect(TokenKind.AND, "AND")
            upper = self._additive()
            return self._located(BetweenPredicate(expression, lower, upper, negated), operator)
        if self._at(TokenKind.LIKE):
            operator = self._advance()
            return self._located(BinaryOp(expression, "NOT LIKE" if negated else "LIKE", self._additive()), operator)
        for kind, operator in ((TokenKind.EQ, "="), (TokenKind.EQEQ, "="), (TokenKind.NE, "!="), (TokenKind.NE2, "!="), (TokenKind.LT, "<"), (TokenKind.LE, "<="), (TokenKind.GT, ">"), (TokenKind.GE, ">=")):
            if self._at(kind):
                operator_token = self._advance()
                return self._located(BinaryOp(expression, operator, self._additive()), operator_token)
        return expression

    def _additive(self) -> Expr:
        expression = self._multiplicative()
        while self._current().kind in {TokenKind.PLUS, TokenKind.MINUS, TokenKind.CONCAT}:
            operator_token = self._advance()
            expression = self._located(BinaryOp(expression, operator_token.lexeme, self._multiplicative()), operator_token)
        return expression

    def _multiplicative(self) -> Expr:
        expression = self._unary()
        while self._current().kind in {TokenKind.STAR, TokenKind.SLASH, TokenKind.PERCENT}:
            operator_token = self._advance()
            expression = self._located(BinaryOp(expression, operator_token.lexeme, self._unary()), operator_token)
        return expression

    def _unary(self) -> Expr:
        if self._at(TokenKind.PLUS):
            operator = self._advance()
            return self._located(UnaryOp("+", self._unary()), operator)
        if self._at(TokenKind.MINUS):
            operator = self._advance()
            return self._located(UnaryOp("-", self._unary()), operator)
        return self._primary()

    def _primary(self) -> Expr:
        token = self._current()
        if token.kind is TokenKind.INTEGER or token.kind is TokenKind.FLOAT or token.kind is TokenKind.STRING or token.kind is TokenKind.BOOLEAN or token.kind is TokenKind.NULL:
            self._advance()
            return self._located(Literal(token.literal), token)
        if token.kind is TokenKind.QUESTION:
            self._advance()
            return self._located(Parameter(), token)
        if token.kind is TokenKind.STAR:
            self._advance()
            return self._located(Star(), token)
        if token.kind is TokenKind.QUOTED_IDENTIFIER and self._in_insert_values:
            self._error(
                f"INSERT ... VALUES 中的 {token.lexeme} 是标识符；字符串请使用单引号，例如 'test'",
                "STRING",
            )
        if token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
            name = token.literal if token.kind is TokenKind.QUOTED_IDENTIFIER else token.lexeme
            self._advance()
            if self._match(TokenKind.LPAREN):
                distinct = self._match(TokenKind.DISTINCT)
                args: list[Expr] = []
                if self._match(TokenKind.STAR):
                    args.append(Star())
                elif not self._at(TokenKind.RPAREN):
                    args = self._expression_list()
                self._expect(TokenKind.RPAREN, "')'")
                return self._located(FunctionCall(name, tuple(args), distinct), token)
            if self._match(TokenKind.DOT):
                if self._match(TokenKind.STAR):
                    return self._located(Star(name), token)
                column_token = self._current()
                column = self._name("列名")
                return self._located(ColumnRef(column, name), column_token)
            return self._located(ColumnRef(name), token)
        if self._match(TokenKind.LPAREN):
            expression = self._expression()
            self._expect(TokenKind.RPAREN, "')'")
            return expression
        self._error("需要表达式", "标识符/字面量/'('")

    def _expression_list(self, *, allow_empty: bool = False) -> list[Expr]:
        values: list[Expr] = []
        if allow_empty and self._at(TokenKind.RPAREN):
            return values
        while True:
            values.append(self._expression())
            if not self._match(TokenKind.COMMA):
                return values

    def _name_list(self) -> list[str]:
        return [name for name, _token in self._name_list_with_tokens()]

    def _name_list_with_tokens(self) -> list[tuple[str, Token]]:
        self._expect(TokenKind.LPAREN, "'('")
        names: list[tuple[str, Token]] = []
        while True:
            names.append(self._name_with_token("列名"))
            if not self._match(TokenKind.COMMA):
                break
        self._expect(TokenKind.RPAREN, "')'")
        return names

    def _integer_literal(self, context: str) -> int:
        token = self._current()
        if token.kind is not TokenKind.INTEGER or int(token.literal) < 0:
            self._error(f"{context} 需要非负整数", "INTEGER")
        self._advance()
        return int(token.literal)

    def _if_not_exists(self) -> bool:
        if self._match(TokenKind.IF):
            self._expect(TokenKind.NOT, "NOT")
            self._expect(TokenKind.EXISTS, "EXISTS")
            return True
        return False

    def _if_exists(self) -> bool:
        if self._match(TokenKind.IF):
            self._expect(TokenKind.EXISTS, "EXISTS")
            return True
        return False

    def _name(self, context: str) -> str:
        return self._name_with_token(context)[0]

    def _name_with_token(self, context: str) -> tuple[str, Token]:
        token = self._current()
        if token.kind not in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
            self._error(f"{context}不合法", "IDENTIFIER")
        self._advance()
        return str(token.literal if token.kind is TokenKind.QUOTED_IDENTIFIER else token.lexeme), token

    @staticmethod
    def _replace_column(column: ColumnDefinition, **changes: object) -> ColumnDefinition:
        """应用表级约束并保留列定义原有的源码位置。"""

        updated = replace(column, **changes)
        updated.copy_source_metadata_from(column)
        return updated

    def _current(self) -> Token:
        return self.tokens[min(self.position, len(self.tokens) - 1)]

    def _peek(self, offset: int) -> Token:
        return self.tokens[min(self.position + offset, len(self.tokens) - 1)]

    def _at(self, kind: TokenKind) -> bool:
        return self._current().kind is kind

    def _advance(self) -> Token:
        token = self._current()
        if not self._at(TokenKind.EOF):
            self.position += 1
        return token

    def _match(self, kind: TokenKind) -> bool:
        if self._at(kind):
            self._advance()
            return True
        return False

    def _expect(self, kind: TokenKind, expected: str) -> Token:
        if not self._at(kind):
            self._error("语法错误", expected)
        return self._advance()

    def _error(self, message: str, expected: str) -> None:
        token = self._current()
        raise ParserError(message, line=token.line, column=token.column, expected=expected, found=token.lexeme or token.kind.value)

    @staticmethod
    def _located(node: NodeType, token: Token) -> NodeType:
        """给 AST 节点附加 Token 起点；动态元数据不会进入 ``to_dict``。"""

        node.with_source_location(token.line, token.column)
        return node


def parse_one(source: str) -> Statement:
    return Parser(source).parse_one()


def parse_script(source: str) -> list[Statement]:
    return Parser(source).parse_script()


__all__ = ["Parser", "parse_one", "parse_script"]
