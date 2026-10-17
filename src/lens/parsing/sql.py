"""MySQL DDL/DML plus a limited stored-routine envelope parser."""

import re
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from lens.core.models import CodeFile, DatabaseObject, Knowledge, Relationship, stable_id
from lens.parsing.sql_text import masked_comments, statements

ROUTINE = re.compile(
    r"CREATE\s+(?:DEFINER\s*=\s*\S+\s+)?(PROCEDURE|FUNCTION)\s+"
    r"([\w`.]+)\s*\((.*?)\)\s*(.*?)\bBEGIN\b(.*?)\bEND\s*(?=\$\$|//|;)",
    re.I | re.S,
)


def table_name(table: exp.Table) -> str:
    return ".".join(part.name for part in table.parts)


def statement_relationships(source_id: str, statement: exp.Expression) -> list[Relationship]:
    """Separate DML write targets from tables read by nested SELECTs."""
    result = []
    writes: set[int] = set()
    for node in statement.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete)):
            target = node.this
            if isinstance(target, exp.Schema):
                target = target.this
            if isinstance(target, exp.Table):
                writes.add(id(target))
    ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    tables = list(statement.find_all(exp.Table))
    for table in tables:
        if table.name.lower() in ctes:
            continue
        kind = "WRITES" if id(table) in writes else "READS"
        result.append(
            Relationship(
                source_id, kind, table_name(table), evidence=f"SQL {statement.key} table reference"
            )
        )
    aliases = {table.alias_or_name.lower(): table_name(table) for table in tables}
    unique_tables = set(aliases.values())
    for column in statement.find_all(exp.Column):
        owner = (
            aliases.get(column.table.lower())
            if column.table
            else (next(iter(unique_tables)) if len(unique_tables) == 1 else None)
        )
        if owner and not column.is_star:
            result.append(
                Relationship(
                    source_id,
                    "REFERENCES",
                    f"{owner}.{column.name}",
                    evidence="SQL column reference",
                )
            )
    return result


def parse(file: CodeFile, data: bytes) -> Knowledge:
    source = data.decode("utf-8-sig")
    result = Knowledge()

    def add(
        name: str, kind: str, text: str, offset: int, metadata: dict | None = None
    ) -> DatabaseObject:
        line = source[:offset].count("\n") + 1
        obj = DatabaseObject(
            stable_id(file.id, kind, name, str(offset)),
            name.split(".")[-1],
            kind,
            name,
            file.id,
            line,
            line + text.count("\n"),
            text,
            metadata or {},
        )
        result.database_objects.append(obj)
        return obj

    remaining = list(source)
    for match in ROUTINE.finditer(masked_comments(source)):
        kind, name, parameters, returns, body = match.groups()
        name = name.replace("`", "")
        params = []
        for param in re.split(r",(?![^()]*\))", parameters):
            pieces = param.strip().split()
            if pieces:
                direction = (
                    pieces.pop(0).upper() if pieces[0].upper() in {"IN", "OUT", "INOUT"} else "IN"
                )
                if len(pieces) < 2:
                    result.warnings.append(f"{file.path}: routine {name}: malformed parameter")
                    continue
                params.append(
                    {
                        "direction": direction,
                        "name": pieces[0].strip("`"),
                        "type": " ".join(pieces[1:]),
                    }
                )
        obj = add(
            name,
            kind.lower(),
            source[match.start() : match.end()],
            match.start(),
            {"parameters": params, "returns": returns.strip()},
        )
        if re.search(r"\b(IF|WHILE|LOOP|CASE|PREPARE|EXECUTE)\b", body, re.I):
            result.warnings.append(
                f"{file.path}: routine {name}: procedural/dynamic SQL not fully analyzed"
            )
        for _, fragment in statements(body):
            fragment = fragment.strip()
            if not fragment:
                continue
            if not re.match(r"^(SELECT|INSERT|UPDATE|DELETE|WITH)\b", fragment, re.I):
                result.warnings.append(f"{file.path}: routine {name}: unsupported statement")
                continue
            try:
                statement = sqlglot.parse_one(fragment, read="mysql")
                result.relationships.extend(statement_relationships(obj.id, statement))
            except ParseError:
                result.warnings.append(f"{file.path}: routine {name}: malformed SQL statement")
        remaining[match.start() : match.end()] = "".join(
            "\n" if c == "\n" else " " for c in match.group()
        )

    outside = "".join(remaining)
    outside = re.sub(r"(?im)^\s*DELIMITER\s+\S+[^\S\n]*$", lambda m: " " * len(m.group()), outside)
    outside = outside.replace("$$", "  ")
    for start, raw in statements(outside):
        clean = masked_comments(raw)
        fragment = clean.strip()
        if not fragment.strip("; \t\r\n"):
            continue
        offset = start + len(clean) - len(clean.lstrip())
        # Dump/session administration does not describe application objects.
        if re.match(r"^(?:SET|START\s+TRANSACTION|BEGIN|COMMIT|ROLLBACK|USE|LOCK\s+TABLES|UNLOCK\s+TABLES)\b", fragment, re.I):
            continue
        alteration = re.match(r"^ALTER\s+TABLE\s+([\w`.]+)\s+(.+)", fragment, re.I | re.S)
        if alteration:
            name, operations = alteration.groups()
            name = name.replace("`", "")
            primary = re.findall(r"PRIMARY\s+KEY\s*\(([^)]+)\)", operations, re.I)
            foreign = re.findall(r"FOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+([\w`.]+)\s*\(([^)]+)\)", operations, re.I)
            indexes = re.findall(r"(?:ADD\s+)?(?:UNIQUE\s+)?(?:KEY|INDEX)\s+([\w`]+)\s*\(([^)]+)\)", operations, re.I)
            obj = add(f"{name}::alter_{offset}", "schema_change", fragment, offset,
                      {"table": name, "primary_keys": primary,
                       "foreign_keys": [{"columns": columns, "table": target.replace("`", ""), "target_columns": targets} for columns, target, targets in foreign],
                       "indexes": [{"name": key.replace("`", ""), "columns": columns} for key, columns in indexes],
                       "analysis": "structural_ddl_fallback"})
            result.relationships.append(Relationship(obj.id, "REFERENCES", name, evidence="ALTER TABLE target"))
            for _, target, _ in foreign:
                result.relationships.append(Relationship(obj.id, "REFERENCES", target.replace("`", ""), evidence="ALTER TABLE foreign key"))
            warning = f"{file.path}: ALTER TABLE metadata extracted; detailed alteration expressions only partially analyzed"
            if warning not in result.warnings:
                result.warnings.append(warning)
            continue
        try:
            statement = sqlglot.parse_one(fragment, read="mysql")
        except ParseError:
            result.warnings.append(
                f"{file.path}:{source[:offset].count(chr(10)) + 1}: malformed SQL"
            )
            continue
        if isinstance(statement, exp.Create):
            kind = str(statement.args.get("kind", "")).lower()
            schema = statement.this
            table = schema.this if isinstance(schema, exp.Schema) else schema
            if kind in {"table", "view"} and isinstance(table, exp.Table):
                name = table_name(table)
                obj = add(name, kind, fragment, offset)
                if isinstance(schema, exp.Schema):
                    for definition in schema.expressions:
                        if isinstance(definition, exp.ColumnDef):
                            column = add(
                                f"{name}.{definition.name}",
                                "column",
                                definition.sql(dialect="mysql"),
                                offset + definition.this.meta.get("start", 0),
                                {
                                    "data_type": definition.args["kind"].sql(),
                                    "constraints": [
                                        c.sql(dialect="mysql")
                                        for c in definition.args.get("constraints", [])
                                    ],
                                },
                            )
                            result.relationships.append(
                                Relationship(
                                    obj.id,
                                    "CONTAINS",
                                    column.qualified_name,
                                    column.id,
                                    "table column",
                                    "high",
                                )
                            )
                    obj.metadata["constraints"] = [
                        d.sql(dialect="mysql")
                        for d in schema.expressions
                        if not isinstance(d, exp.ColumnDef)
                    ]
                    for foreign in schema.find_all(exp.Reference):
                        referenced = foreign.this
                        if isinstance(referenced, exp.Schema):
                            referenced = referenced.this
                        if isinstance(referenced, exp.Table):
                            result.relationships.append(
                                Relationship(
                                    obj.id,
                                    "REFERENCES",
                                    table_name(referenced),
                                    evidence="declared foreign key",
                                )
                            )
                if kind == "view" and statement.expression:
                    result.relationships.extend(
                        statement_relationships(obj.id, statement.expression)
                    )
            elif kind in {"schema", "database"}:
                add(table.name, kind, fragment, offset)
            elif kind == "index":
                add(statement.this.name, "index", fragment, offset)
            else:
                result.warnings.append(f"{file.path}: unsupported CREATE {kind}")
        elif isinstance(statement, (exp.Select, exp.Insert, exp.Update, exp.Delete)):
            indexed_source = fragment
            redacted_rows = isinstance(statement, exp.Insert) and isinstance(statement.expression, exp.Values)
            if redacted_rows:
                # SQL dumps contain application records, not query templates.
                # Keep identifiers and write evidence while discarding all row
                # expressions without rendering or recording their values.
                sanitized = statement.copy()
                sanitized_values = sanitized.expression
                sanitized_values.set("expressions", [
                    exp.Tuple(expressions=[exp.Placeholder() for _ in row.expressions])
                    for row in sanitized_values.expressions
                ])
                # Duplicate-key clauses can also contain copied record literals.
                sanitized = sanitized.transform(
                    lambda node: exp.Placeholder() if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)) else node
                )
                indexed_source = sanitized.sql(dialect="mysql")
            obj = add(f"statement_{offset}", statement.key, indexed_source, offset)
            obj.line_end = obj.line_start + fragment.count("\n")
            if redacted_rows:
                obj.metadata["row_values_redacted"] = True
            result.relationships.extend(statement_relationships(obj.id, statement))
        else:
            result.warnings.append(f"{file.path}: unsupported SQL statement")
    result.warnings = list(dict.fromkeys(result.warnings))
    return result
