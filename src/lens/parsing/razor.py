"""Razor directives and HTML tag helpers, retaining source locations."""
import re
from html.parser import HTMLParser
from lens.core.models import CodeSymbol, Knowledge, Relationship, stable_id


def parse(file, data):
    source = data.decode('utf-8-sig')
    result = Knowledge()
    model = re.search(r'^\s*@model\s+([^\r\n]+)', source, re.M)
    # The visible heading best represents the screen a client sees. Keep the
    # Razor metadata title as a fallback for pages without an h1.
    title = re.search(r'<h1\b[^>]*>(.*?)</h1\s*>', source, re.I | re.S)
    if not title:
        title = re.search(r'ViewData\[\s*["\']Title["\']\s*\]\s*=\s*["\']([^"\']+)', source, re.I)
    page_title = re.sub(r'<[^>]+>|\s+', ' ', title[1]).strip() if title else ''
    # Razor expressions in headings are runtime values; retain the visible
    # static prefix without implying that a dynamic title was resolved.
    page_title = re.split(r'\s*@\s*(?:\(|\{|[A-Za-z_])', page_title, maxsplit=1)[0].strip()[:180]
    view = CodeSymbol(stable_id(file.id, 'razor_view'), file.path, 'razor_view', file.path,
                      file.id, 1, max(1, len(source.splitlines())), source,
                      {'model': model[1].strip() if model else '', 'page_title': page_title,
                       'forms': [], 'navigation_links': [],
                       'model_references': sorted(set(re.findall(r'\bModel\.([\w.]+)', source)))})
    result.symbols.append(view)
    if model:
        result.relationships.append(Relationship(view.id, 'USES_MODEL', model[1].strip(), evidence='Razor @model directive'))
    for match in re.finditer(r'^\s*@using\s+([^\r\n;]+)', source, re.M):
        result.relationships.append(Relationship(view.id, 'IMPORTS_MODULE', match[1].strip(), evidence='Razor @using directive'))

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.links = []
            self.anchors = []
            self.anchor_stack = []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag not in {'form', 'a', 'input', 'select', 'textarea', 'button'}:
                return
            line = self.getpos()[0]
            if tag == 'a':
                anchor = {'line': line, 'class': attributes.get('class', ''),
                          'href': attributes.get('href', ''), 'label': ''}
                self.anchors.append(anchor)
                self.anchor_stack.append(anchor)
            name = tag + (':' + attributes['asp-for'] if attributes.get('asp-for') else '')
            entity = CodeSymbol(stable_id(file.id, tag, str(line), str(self.getpos()[1])), name,
                'html_element', f'{file.path}::{name}:{line}', file.id, line, line,
                self.get_starttag_text(), {'tag': tag, 'attributes': attributes})
            result.symbols.append(entity)
            result.relationships.append(Relationship(view.id, 'CONTAINS', entity.qualified_name, entity.id, 'Razor HTML structure', 'high'))
            if tag == 'form' or attributes.get('asp-action'):
                form = {'controller': attributes.get('asp-controller', ''), 'action': attributes.get('asp-action', ''),
                        'method': attributes.get('method', 'get').upper(),
                        'route': attributes.get('action') or attributes.get('href') or '', 'line': line}
                view.metadata['forms'].append(form)
                entity.metadata['form'] = form
                if form['action']:
                    result.relationships.append(Relationship(entity.id, 'SUBMITS_TO', '.'.join(filter(None, [form['controller'], form['action']])), evidence='Razor tag helper', metadata=form))
            href = attributes.get('href', '').strip()
            placeholder_href = href == '#' or href.lower().startswith('javascript:void')
            if tag == 'a' and (attributes.get('asp-action') or (href and not placeholder_href)):
                href_value = attributes.get('href', '')
                action_helper = re.search(
                    r'@(?:Url|Html)\.Action\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']+)["\']\s*\)?',
                    self.get_starttag_text())
                helper_action, helper_controller = action_helper.groups() if action_helper else ('', '')
                link = {'action': attributes.get('asp-action', '') or helper_action,
                        'controller': attributes.get('asp-controller', '') or helper_controller,
                        'area': attributes.get('asp-area', ''),
                        'route_values': {key[len('asp-route-'):]: value for key, value in attributes.items()
                                         if key.startswith('asp-route-') and key != 'asp-route'},
                        'href': '' if action_helper else href_value,
                        'href_expression': action_helper[0] if action_helper else '',
                        'origin': 'razor_url_action' if action_helper else 'razor_anchor',
                        'label': '', 'line': line}
                entity.metadata['navigation_link'] = link
                view.metadata['navigation_links'].append(link)
                if link['action'] or link['href']:
                    target = '.'.join(filter(None, [link['area'], link['controller'], link['action']])) or link['href']
                    result.relationships.append(Relationship(entity.id, 'LINKS_TO', target,
                        evidence='Razor anchor tag helper' if link['action'] else 'literal Razor href',
                        metadata=link))
                self.links.append((line, link))
            if attributes.get('asp-for'):
                entity.metadata['model_reference'] = attributes['asp-for']

        def handle_data(self, data):
            if self.links and data.strip():
                line, link = self.links[-1]
                link['label'] = (link['label'] + ' ' + data.strip()).strip()[:240]
            if self.anchor_stack and data.strip():
                self.anchor_stack[-1]['label'] = (self.anchor_stack[-1]['label'] + ' ' + data.strip()).strip()[:240]

        def handle_endtag(self, tag):
            if tag == 'a' and self.links:
                self.links.pop()
            if tag == 'a' and self.anchor_stack:
                self.anchor_stack.pop()
    parser = Tags()
    parser.feed(source)
    parser.close()
    # Razor views commonly assemble MVC destinations in inline JavaScript.
    # Preserve only literal, app-relative route expressions and their variable
    # names; this is static template evidence, not a request or a resolved URL.
    assignment = re.compile(r"(?:window\.)?location(?:\.href)?\s*=\s*([\"'`])(/[^\"'`\r\n]{1,1000})\1")
    suffix = re.compile(r"\s*\+\s*(?:\"([^\"\r\n]*)\"|'([^'\r\n]*)'|([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:\([^()\r\n]*\))?))")
    for match in assignment.finditer(source):
        template = match[2]
        cursor = match.end()
        dynamic_names = []
        for _ in range(20):
            part = suffix.match(source, cursor)
            if not part:
                break
            literal, single_quoted, expression = part.groups()
            if expression is not None:
                template += "{" + expression + "}"
                dynamic_names.append(expression)
            else:
                template += literal if literal is not None else single_quoted
            cursor = part.end()
        line = source.count("\n", 0, match.start()) + 1
        route = {'controller': '', 'action': '', 'area': '', 'route_values': {},
                 'href': template, 'label': '', 'line': line,
                 'origin': 'javascript_location_assignment',
                 'dynamic_parameters': dynamic_names}
        view.metadata['navigation_links'].append(route)
        result.relationships.append(Relationship(view.id, 'DECLARES_NAVIGATION_ROUTE', template,
            evidence='app-relative route assembled in a location assignment', confidence='static_pattern',
            metadata={'line': line, 'route_template': template,
                      'query_parameters': sorted(set(re.findall(r'[?&]([A-Za-z_][\w-]*)=', template))),
                      'dynamic_parameters': dynamic_names, 'static_only': True}))
    # Also capture routes first assigned to a variable and later handed to
    # location.href (common for conditional invoice/payroll buttons).
    location_variables = set(re.findall(
        r'(?:window\.)?location(?:\.href)?\s*=\s*([A-Za-z_$][\w$]*)\b', source))
    variable_route = re.compile(r'\b(?:let|const|var)?\s*([A-Za-z_$][\w$]*)\s*=\s*([\"\'`])(/[^\"\'`\r\n]{1,1000})\2')
    route_selectors = {}
    handler = re.compile(
        r"\.on\(\s*['\"](?:click|submit)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,"
        r"([\s\S]*?)\}\s*\)\s*;", re.I)
    for event in handler.finditer(source):
        selector, body = event.groups()
        assigned = re.search(r"\b([A-Za-z_$][\w$]*)\s*=\s*['\"`](/[^'\"`\r\n]{1,1000})['\"`]", body)
        if assigned and re.search(r"(?:window\.)?location(?:\.href)?\s*=\s*" + re.escape(assigned[1]) + r"\b", body):
            route_selectors[assigned[1]] = selector

    def matching_anchor(selector):
        classes = set(re.findall(r"\.([A-Za-z_][\w-]*)", selector))
        return next((anchor for anchor in parser.anchors
                     if classes & set(anchor.get('class', '').split())), None)

    known_routes = {item.get('href') for item in view.metadata['navigation_links']}
    for match in variable_route.finditer(source):
        variable, quote, raw_template = match.groups()
        if variable not in location_variables:
            continue
        template = re.sub(r'\$\{([^}]+)\}', r'{\1}', raw_template)
        if template in known_routes:
            continue
        known_routes.add(template)
        line = source.count("\n", 0, match.start()) + 1
        prefix = source[max(0, match.start() - 240):match.start()]
        guard = re.findall(r"hasClass\(\s*['\"]([^'\"]+)['\"]\s*\)|(?:if|else if)\s*\([^\n]{0,120}?===?\s*['\"]([^'\"]+)['\"]", prefix)
        condition = next((a or b for a, b in reversed(guard) if a or b), "")
        dynamic_names = re.findall(r'\$\{([^}]+)\}', raw_template)
        # Common post-branch query updates (for example yearname added after
        # choosing proforma/invoice/receipt) apply to each candidate route.
        location_assignment = re.search(r'(?:window\.)?location(?:\.href)?\s*=\s*' + re.escape(variable) + r'\b', source[match.end():])
        if location_assignment:
            tail = source[match.end():match.end() + location_assignment.start()]
            for append in re.finditer(r'\b' + re.escape(variable) + r'\s*\+=\s*([\"\'`])(&yearname=[^\"\'`\r\n]*)\1', tail):
                addition = re.sub(r'\$\{([^}]+)\}', r'{\1}', append[2])
                template += addition
                dynamic_names.extend(re.findall(r'\$\{([^}]+)\}', append[2]))
        selector = route_selectors.get(variable, '')
        anchor = matching_anchor(selector) if selector else None
        route = {'controller': '', 'action': '', 'area': '', 'route_values': {},
                 'href': template, 'label': anchor.get('label', '') if anchor else '',
                 'line': anchor.get('line', line) if anchor else line,
                 'route_line': line, 'selector': selector,
                 'origin': 'javascript_route_variable', 'condition': condition,
                 'dynamic_parameters': dynamic_names}
        view.metadata['navigation_links'].append(route)
        result.relationships.append(Relationship(view.id, 'DECLARES_NAVIGATION_ROUTE', template,
            evidence='app-relative route assigned to a variable passed to location.href', confidence='static_pattern',
            metadata={'line': line, 'route_template': template,
                      'query_parameters': sorted(set(re.findall(r'[?&]([A-Za-z_][\w-]*)=', template))),
                      'dynamic_parameters': dynamic_names, 'condition': condition, 'static_only': True}))
    # Embedded C# remains available as view source; references are facts rather
    # than fabricated standalone C# declarations from incomplete expression blocks.
    view.metadata['code_expressions'] = sorted(set(re.findall(r'@(?:Model\.)?[A-Za-z_]\w*(?:\.\w+)*', source)))
    return result
