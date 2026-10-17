"""Python structure and literal dependency evidence extracted with the standard AST."""
import ast
import re
from pathlib import PurePosixPath
from lens.core.models import CodeSymbol, Knowledge, Relationship, stable_id


def parse(file, data):
    source = data.decode('utf-8-sig')
    lines = source.splitlines()
    result = Knowledge()
    module_name = str(PurePosixPath(file.path).with_suffix('')).replace('/', '.')
    try:
        root = ast.parse(source, filename=file.path)
    except SyntaxError as error:
        result.warnings.append(f'{file.path}:{error.lineno}: Python syntax error; declarations unavailable')
        return result

    assignments = []
    for statement in root.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            continue
        names = [item.id for target in targets for item in ast.walk(target) if isinstance(item, ast.Name)]
        assignments.append((statement, names))
        if any(re.search(r"(?:^|_)(?:password|passwd|secret|token|api_?key|private_?key|credential|database_url|connection_string|dsn)(?:$|_)", name, re.I) for name in names):
            # Parent module evidence must use the same redaction as the symbol.
            first, last = statement.lineno - 1, statement.end_lineno
            lines[first] = " = ".join(names) + " = '<value redacted>'"
            for index in range(first + 1, last):
                lines[index] = "# credential assignment continuation redacted"

    def add(node, name, kind, qualified, parent=None):
        start = getattr(node, 'lineno', 1)
        end = getattr(node, 'end_lineno', len(lines) or 1)
        symbol = CodeSymbol(stable_id(file.id, kind, qualified, str(start)), name, kind,
                            qualified, file.id, start, end, '\n'.join(lines[start-1:end]),
                            {'parent_id': parent.id if parent else None,
                             'documentation': (ast.get_docstring(node) or '') if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else ''})
        result.symbols.append(symbol)
        if parent:
            result.relationships.append(Relationship(parent.id, 'CONTAINS', qualified, symbol.id,
                                                     'Python AST declaration', 'high'))
        return symbol

    module = add(root, module_name, 'python_module', module_name)
    module.metadata['summary'] = f'Python module {file.path}\n' + '\n'.join(
        ast.unparse(n) for n in root.body if isinstance(n, (ast.Import, ast.ImportFrom)))

    def owned(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            yield child
            yield from owned(child)

    def visit(node, symbol):
        symbol.metadata.update({'imports': [], 'calls': [], 'routes': [], 'requests': [], 'environment_variables': [], 'sql_literals': []})
        for child in owned(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                imports = [{'module': alias.name if isinstance(child, ast.Import) else ('.' * child.level + (child.module or '')),
                            'source': alias.name if isinstance(child, ast.Import) else ('.' * child.level + (child.module or '')),
                            'local': alias.asname or alias.name.split('.')[0], 'imported': alias.name,
                            'line': child.lineno} for alias in child.names]
                symbol.metadata['imports'].extend(imports)
                for entry in imports:
                    result.relationships.append(Relationship(symbol.id, 'IMPORTS_MODULE', entry['module'], evidence='Python AST import'))
            if isinstance(child, ast.Call):
                name = ast.unparse(child.func)
                receiver, _, short = name.rpartition('.')
                symbol.metadata['calls'].append({'name': short or name, 'receiver': receiver,
                                                  'owner': symbol.qualified_name.rpartition('.')[0], 'line': child.lineno})
                result.relationships.append(Relationship(symbol.id, 'CALLS', short or name, evidence=name,
                    metadata={'source_line_start': child.lineno, 'call_expression': name}))
                if receiver in {'requests', 'httpx'} and short in {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'} and child.args and isinstance(child.args[0], ast.Constant) and isinstance(child.args[0].value, str):
                    path = child.args[0].value
                    request = {'callee': name, 'path': path, 'method': short.upper(),
                               'direct': True, 'dynamic': False, 'expression': repr(path),
                               'line_start': child.lineno, 'line_end': child.end_lineno}
                    symbol.metadata['requests'].append(request)
                    result.relationships.append(Relationship(symbol.id, 'REQUESTS', short.upper() + ' ' + path,
                        evidence='Python literal HTTP request', metadata={'path': path, 'method': short.upper(), 'source_line_start': child.lineno}))
                if name in {'os.getenv', 'os.environ.get', 'getenv'} and child.args and isinstance(child.args[0], ast.Constant) and isinstance(child.args[0].value, str):
                    symbol.metadata['environment_variables'].append(child.args[0].value)
            if isinstance(child, ast.Subscript) and ast.unparse(child.value) == 'os.environ' and isinstance(child.slice, ast.Constant) and isinstance(child.slice.value, str):
                symbol.metadata['environment_variables'].append(child.slice.value)
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if re.match(r'^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b', child.value, re.I):
                    symbol.metadata['sql_literals'].append(child.value)
        for key in sorted(set(symbol.metadata['environment_variables'])):
            result.relationships.append(Relationship(symbol.id, 'USES_CONFIGURATION', key, evidence='Python literal environment lookup'))
        for child in getattr(node, 'body', []):
            if not isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            kind = 'class' if isinstance(child, ast.ClassDef) else ('method' if isinstance(node, ast.ClassDef) else 'function')
            entity = add(child, child.name, kind, symbol.qualified_name + '.' + child.name, symbol)
            if isinstance(child, ast.ClassDef):
                entity.metadata['bases'] = [ast.unparse(base) for base in child.bases]
                for base in entity.metadata['bases']:
                    result.relationships.append(Relationship(entity.id, 'INHERITS', base, evidence='Python AST base class'))
            else:
                entity.metadata['parameters'] = [{'name': arg.arg, 'type': ast.unparse(arg.annotation) if arg.annotation else ''} for arg in [*child.args.posonlyargs, *child.args.args, *child.args.kwonlyargs]]
                entity.metadata['return_type'] = ast.unparse(child.returns) if child.returns else ''
            visit(child, entity)
            for decorator in child.decorator_list:
                if isinstance(decorator, ast.Call) and decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                    method = ast.unparse(decorator.func).rsplit('.', 1)[-1]
                    if method in {'get', 'post', 'put', 'patch', 'delete', 'route', 'api_route'}:
                        methods = [method.upper()] if method not in {'route', 'api_route'} else ['GET']
                        for keyword in decorator.keywords:
                            if keyword.arg == 'methods' and isinstance(keyword.value, (ast.List, ast.Tuple)):
                                methods = [item.value.upper() for item in keyword.value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
                        entity.metadata['routes'].extend({'path': decorator.args[0].value, 'method': m, 'renders': []} for m in methods)
    visit(root, module)
    for statement, names in assignments:
        for name in names:
            entity = add(statement, name, 'constant' if name.isupper() else 'variable',
                         module.qualified_name + '.' + name, module)
            entity.metadata['assignment_kind'] = 'annotated' if isinstance(statement, ast.AnnAssign) else 'assignment'
            entity.metadata['value_redacted'] = '<value redacted>' in entity.source
            if isinstance(statement, ast.AnnAssign):
                entity.metadata['declared_type'] = ast.unparse(statement.annotation)
    return result
