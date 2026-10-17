"""Configuration structure without persisting configuration values or credentials."""
import configparser
import json
import re
import tomllib
from pathlib import Path
from dotenv import dotenv_values
from io import StringIO
from lens.core.models import CodeSymbol, Knowledge, Relationship, stable_id

ENV_REFERENCE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)[^}]*\}|\$([A-Za-z_][A-Za-z0-9_]*)|%([A-Za-z_][A-Za-z0-9_]*)%')


def jsonc_loads(source):
    """Load JSON with the comments and trailing commas accepted by .NET config."""
    chars = list(source.lstrip("\ufeff"))
    quoted = escaped = False
    i = 0
    while i < len(chars):
        char = chars[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            i += 1
            continue
        if char == '"':
            quoted = True
            i += 1
            continue
        if char == "/" and i + 1 < len(chars) and chars[i + 1] in {"/", "*"}:
            end = i + 2
            if chars[i + 1] == "/":
                while end < len(chars) and chars[end] not in "\r\n":
                    end += 1
            else:
                while end + 1 < len(chars) and chars[end:end + 2] != ["*", "/"]:
                    end += 1
                end = min(len(chars), end + 2)
            for index in range(i, end):
                if chars[index] not in "\r\n":
                    chars[index] = " "
            i = end
            continue
        i += 1
    quoted = escaped = False
    for i, char in enumerate(chars):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == ",":
            following = i + 1
            while following < len(chars) and chars[following].isspace():
                following += 1
            if following < len(chars) and chars[following] in {"]", "}"}:
                chars[i] = " "
    return json.loads("".join(chars))


def is_environment(path):
    name = Path(path).name.lower()
    return name == '.env' or name.startswith('.env.') or name.endswith('.env')


def parse(file, data):
    source = data.decode('utf-8-sig')
    result = Knowledge()
    values = []
    limited = False
    key_lines = {}
    key_occurrences = {}
    for number, line in enumerate(source.splitlines(), 1):
        match = re.match(r'''\s*(?:export\s+)?["']?([\w.-]+)["']?\s*[:=]''', line)
        if match:
            key_lines.setdefault(match[1], []).append(number)

    def flatten(value, prefix='', depth=0):
        nonlocal limited
        if len(values) >= 2000 or depth > 32:
            limited = True
            return
        if isinstance(value, dict):
            for key, item in value.items():
                flatten(item, f'{prefix}.{key}' if prefix else str(key), depth + 1)
                if limited:
                    break
        elif isinstance(value, list):
            for index, item in enumerate(value):
                flatten(item, f'{prefix}[{index}]', depth + 1)
                if limited:
                    break
        else:
            values.append((prefix, value))
    try:
        if is_environment(file.path):
            values = list(dotenv_values(stream=StringIO(source), interpolate=False).items())
        elif file.path.lower().endswith('.json'):
            value = jsonc_loads(source)
            if not isinstance(value, dict):
                result.warnings.append(f'{file.path}: JSON root is not a configuration object; ignored')
                return result
            flatten(value)
        elif file.path.lower().endswith('.toml'):
            flatten(tomllib.loads(source))
        elif file.path.lower().endswith(('.ini', '.cfg')):
            config = configparser.ConfigParser(interpolation=None)
            config.read_string(source)
            flatten({section: dict(config[section]) for section in config.sections()})
        else:
            # YAML's complete grammar is intentionally not emulated. Preserve key
            # structure only; values are never indexed, including multiline secrets.
            stack = []
            for number, line in enumerate(source.splitlines(), 1):
                match = re.match(r'^(\s*)(-\s+)?([\w.-]+)\s*:', line)
                if not match:
                    continue
                indent, key = len(match[1]) + len(match[2] or ""), match[3]
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                if len(values) >= 2000 or len(stack) >= 32:
                    limited = True
                    break
                values.append(('.'.join([entry[1] for entry in stack] + [key]), None, number))
                stack.append((indent, key))
    except (ValueError, configparser.Error):
        result.warnings.append(f'{file.path}: invalid configuration syntax; values omitted')
        return result
    if limited or len(values) > 2000:
        result.warnings.append(f'{file.path}: configuration structure truncated at 2000 keys or 32 nesting levels')
    for entry in values[:2000]:
        key, value = entry[:2]
        leaf = key.split('.')[-1].split('[')[0]
        key_occurrences[key] = key_occurrences.get(key, 0) + 1
        occurrence = key_occurrences[key]
        locations = key_lines.get(leaf, [1])
        location = entry[2] if len(entry) == 3 else locations[min(occurrence - 1, len(locations) - 1)]
        qualified_name = key if occurrence == 1 else f'{key}[{occurrence}]'
        refs = sorted({next(group for group in match.groups() if group) for match in ENV_REFERENCE.finditer(str(value or ''))})
        entity = CodeSymbol(stable_id(file.id, 'configuration', key, str(location), str(occurrence)), key, 'configuration', qualified_name,
                            file.id, location, location, f'{key}=<value omitted>',
                            {'environment_variables': refs, 'value_redacted': True, 'configuration_key': key, 'occurrence': occurrence,
                             'configuration_format': 'environment' if is_environment(file.path) else Path(file.path).suffix[1:]})
        result.symbols.append(entity)
        for reference in refs:
            result.relationships.append(Relationship(entity.id, 'USES_CONFIGURATION', reference,
                evidence='configuration variable interpolation'))
    return result
