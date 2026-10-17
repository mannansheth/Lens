from pathlib import Path
import pytest
from lens.repository.indexing import parse_repository
from lens.repository.knowledge import chunks


def repository(tmp_path: Path, files: dict[str, str]):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return parse_repository(tmp_path)


def edges(knowledge):
    names = {s.id: s.qualified_name for s in knowledge.entities}
    return {(names[r.source_id], r.kind, names.get(r.target_id)) for r in knowledge.relationships}


@pytest.mark.parametrize("extension", ["js", "jsx", "ts", "tsx", "mjs", "cjs"])
def test_functions_and_parameters(tmp_path, extension):
    knowledge = repository(tmp_path, {f"code.{extension}": "export function add(a,b) { return a+b; } export const twice = x => add(x,x);"})
    add = next(s for s in knowledge.symbols if s.name == "add")
    assert add.metadata["parameters"] == ["a", "b"]
    assert (f"code.{extension}::twice", "CALLS", f"code.{extension}::add") in edges(knowledge)
    assert not knowledge.warnings


def test_typescript_types_and_class_members(tmp_path):
    knowledge = repository(tmp_path, {"types.ts": """
    export interface User { name: string }
    export type Key = string | number;
    export enum Status { Open, Closed }
    export class Base { run(x:number):number { return x; } }
    export class Service extends Base implements User { name = 'x'; }
    export const service = { login: (name:string) => name, logout() { return true; } };
    """})
    kinds = {s.kind for s in knowledge.symbols}
    assert {"interface", "type", "enum", "class", "method", "constant", "module"} <= kinds
    assert ("types.ts::Service", "EXTENDS", "types.ts::Base") in edges(knowledge)
    assert ("types.ts::Service", "IMPLEMENTS", "types.ts::User") in edges(knowledge)


@pytest.mark.parametrize("extension", ["jsx", "tsx"])
def test_components_props_hooks_events_and_imports(tmp_path, extension):
    knowledge = repository(tmp_path, {
        f"Table.{extension}": "export default function Table({rows}) { return <div>{rows.length}</div>; }",
        f"Page.{extension}": f'''import Table from './Table.{extension}';
        import {{useState}} from 'react';
        export default function Page({{initial}}) {{
            const [rows,setRows] = useState(initial);
            function handleClick() {{setRows([]);}}
            return <><Table rows={{rows}}/><button onClick={{handleClick}}>Clear rows</button></>;
        }}''',
    })
    page = next(s for s in knowledge.symbols if s.name == "Page")
    assert page.kind == "component"
    assert "initial" in page.metadata["props"]
    assert page.metadata["hooks"] == ["useState"]
    assert (f"Page.{extension}::Page", "RENDERS", f"Table.{extension}::Table") in edges(knowledge)
    assert (f"Page.{extension}::Page", "IMPORTS", f"Table.{extension}::Table") in edges(knowledge)
    assert (f"Page.{extension}::Page", "REFERENCES", f"Page.{extension}::Page.handleClick") in edges(knowledge)
    document = next(c for c in chunks(knowledge, "test") if c.name.endswith("::Page"))
    assert "Clear rows" in document.embedding_text
    assert "Renders components: Table" in document.embedding_text
    assert document.metadata["symbol"] == page.id


def test_reexports_named_alias_namespace_and_index(tmp_path):
    knowledge = repository(tmp_path, {
        "lib/service.ts": "export const fetchInvoices = () => []; export default function main() {}",
        "lib/index.ts": "export {fetchInvoices as load} from './service'; export {default} from './service';",
        "page.ts": "import start, {load as fetch} from './lib'; import * as svc from './lib/service'; export function run() { start(); fetch(); svc.fetchInvoices(); }",
    })
    resolved = edges(knowledge)
    assert ("page.ts::run", "CALLS", "lib/service.ts::main") in resolved
    assert ("page.ts::run", "CALLS", "lib/service.ts::fetchInvoices") in resolved
    assert not any(r.kind == "CALLS" and r.target_id is None for r in knowledge.relationships)


def test_default_identifier_anonymous_and_export_star(tmp_path):
    knowledge = repository(tmp_path, {
        "one.js": "const go = () => 1; export default go; export function named() {}",
        "two.js": "export default () => 2; export * from './one';",
        "entry.js": "import two,{named} from './two'; function run(){ two(); named(); }",
    })
    assert ("entry.js::run", "CALLS", "two.js::two") in edges(knowledge)
    assert ("entry.js::run", "CALLS", "one.js::named") in edges(knowledge)


def test_shadowing_ambiguity_cycles_and_external_packages(tmp_path):
    knowledge = repository(tmp_path, {
        "service.js": "export function login() {}",
        "Page.jsx": "export default function Page(){ return <div/>; }",
        "Page.tsx": "export default function Page(){ return <div/>; }",
        "cycleA.js": "export * from './cycleB';", "cycleB.js": "export * from './cycleA';",
        "entry.jsx": """import {login} from './service'; import Page from './Page';
        import {missing} from './cycleA'; import External from 'package';
        function run(login) { login(); missing(); return <><Page/><External/></>; }""",
    })
    calls = [r for r in knowledge.relationships if r.kind == "CALLS"]
    assert all(r.target_id is None for r in calls)
    renders = [r for r in knowledge.relationships if r.kind == "RENDERS"]
    assert len(renders) == 2 and all(r.target_id is None for r in renders)


def test_api_wrapper_backend_and_sql_evidence(tmp_path):
    knowledge = repository(tmp_path, {
        "api.js": "export async function api(path, options={}) {return fetch(`/api${path}`, options);}",
        "Page.jsx": "import {api} from './api'; export function Page(){ function submit(){return api('/contact', {method:'POST'});} return <button onClick={submit}>Send message</button>;}",
        "server.js": "import express from 'express'; const app=express(); app.post('/api/contact', async(req,res)=>{await query('INSERT INTO contacts (name) VALUES (?)', [req.body.name]);});",
        "schema.sql": "CREATE TABLE contacts (name VARCHAR(100));",
    })
    resolved = edges(knowledge)
    assert ("Page.jsx::Page.submit", "REQUESTS", "server.js::POST /api/contact") in resolved
    assert ("server.js::POST /api/contact", "WRITES", "contacts") in resolved
    assert any(r.kind == "REQUESTS" and r.target_name == "POST /api/contact" for r in knowledge.relationships)


def test_fetch_axios_dynamic_evidence_and_no_false_comments(tmp_path):
    knowledge = repository(tmp_path, {"requests.js": """import client from 'axios';
    // fetch('/not-real'); function invented() {}
    const example = "fetch('/not-real')";
    export function load(id) {fetch('/api/items'); client.post('/api/items'); fetch(`/api/items/${id}`); fetch(url);}
    """})
    load = next(s for s in knowledge.symbols if s.name == "load")
    assert len(load.metadata["requests"]) == 3
    assert load.metadata["requests"][2]["dynamic"] is True
    assert not any(r.target_name.endswith("/not-real") for r in knowledge.relationships)
    assert not any(s.name == "invented" for s in knowledge.symbols)


def test_html_css_and_malformed_files(tmp_path):
    knowledge = repository(tmp_path, {
        "index.html": '<html><body><h1>Invoices</h1><form id="login"><input name="email"/><button>Sign in</button></form><a href="/help">Help</a><script src="/main.js"></script></body></html>',
        "styles.css": ".login button { color: red; } @media (max-width:600px){ .login { display:block; } }",
        "bad.tsx": "export function Broken( { return <div>",
        "good.js": "export const ok = () => true;",
    })
    assert {"html_document", "html_element", "css_rule", "function"} <= {s.kind for s in knowledge.symbols}
    assert any(s.metadata.get("attributes", {}).get("id") == "login" for s in knowledge.symbols)
    assert knowledge.warnings
    assert any(s.name == "ok" for s in knowledge.symbols)


def test_scanner_mixed_languages_and_generated_exclusions(tmp_path):
    knowledge = repository(tmp_path, {
        "a.cs": "class A {}", "a.sql": "CREATE TABLE invoices (id INT);",
        "a.js": "export function run() {}", "a.tsx": "export const Page = () => <div/>;",
        "node_modules/dependency/index.js": "function ignored() {}",
        "dist/output.js": "function ignored() {}", "build/bundle.js": "function ignored() {}",
        "coverage/report.js": "function ignored() {}", ".cache/file.js": "function ignored() {}",
        "app.min.js": "function ignored() {}", "app.js.map": "{}",
    })
    assert len(knowledge.files) == 4
    assert knowledge.statistics["directories_ignored"] == 5
    assert knowledge.statistics["files_ignored"] == 2
    assert knowledge.statistics["parser_coverage"] == 1


def test_class_components_and_large_units_keep_full_source(tmp_path):
    source = "import {Component} from 'react'; export class Login extends Component { render(){return <button>Sign in</button>;} }"
    knowledge = repository(tmp_path, {"Login.jsx": source,
        "large.jsx": "export function Large(){return <div>" + ("long content " * 500) + "</div>;}"})
    assert next(s for s in knowledge.symbols if s.name == "Login").kind == "component_class"
    large = next(s for s in knowledge.symbols if s.name == "Large")
    assert len(large.source) > 5000
    document = next(c for c in chunks(knowledge, "test") if c.name.endswith("::Large"))
    assert len(document.embedding_text) <= 1000
    assert large.source.endswith("</div>;}")


def test_dynamic_urls_are_not_concrete_requests_and_parameter_methods_are_unknown(tmp_path):
    knowledge = repository(tmp_path, {"api.js": "export function call(url, method){fetch('/items',{method}); fetch('/items', options);}",
                                    "page.js": "export function load(id){fetch(`/api/items/${id}`);}"})
    request = next(s for s in knowledge.symbols if s.name == "load").metadata["requests"][0]
    assert request["path"] == "/api/items/{dynamic}"
    assert request["dynamic"]
    assert not any(r.target_id for r in knowledge.relationships if r.kind == "REQUESTS")
    call = next(s for s in knowledge.symbols if s.name == "call")
    assert all(r["method"] is None for r in call.metadata["requests"])


def test_jsx_route_context_and_literal_express_factory(tmp_path):
    knowledge = repository(tmp_path, {
        "Page.jsx": '''export function Page(){return <div/>;} export function App(){return <Route path="/work/:slug" element={<Page/>}/>;}''',
        "fake.js": '''const fake = '=express()'; fake.get('/not-an-endpoint',()=>{});''',
    })
    page = next(s for s in knowledge.symbols if s.name == "Page")
    assert page.metadata["incoming_routes"] == [{"path": "/work/:slug", "owner": "Page.jsx::App",
                                                   "source_file": "Page.jsx", "line": 1}]
    assert not any(s.kind == "endpoint" for s in knowledge.symbols)


def test_wrapped_components_and_class_props(tmp_path):
    knowledge = repository(tmp_path, {"components.tsx": '''import {memo,Component} from 'react';
        export const Card = memo((props: {title:string}) => <h1>{props.title}</h1>);
        export class Panel extends Component {render(){return <div>{this.props.children}</div>;}}'''
    })
    card = next(s for s in knowledge.symbols if s.name == "Card")
    panel = next(s for s in knowledge.symbols if s.name == "Panel")
    assert card.kind == "component" and "title" in card.metadata["props"]
    assert panel.kind == "component_class" and "children" in panel.metadata["props"]
