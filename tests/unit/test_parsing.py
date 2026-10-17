from pathlib import Path
from lens.repository.indexing import parse_repository
from lens.repository.knowledge import chunks, resolve
from lens.core.models import CodeFile
from lens.parsing import csharp, sql

DEMO = Path(__file__).parents[2] / "examples/demo-app"


def test_demo_cross_layer_chain():
    knowledge = parse_repository(DEMO.resolve())
    names = {e.id: e.qualified_name for e in knowledge.entities}
    edges = {(names[r.source_id], r.kind, names.get(r.target_id)) for r in knowledge.relationships}
    assert (
        "Demo.Controllers.InvoiceController.GetInvoices",
        "CALLS",
        "Demo.Services.InvoiceService.GetInvoices",
    ) in edges
    assert (
        "Demo.Services.InvoiceService.GetInvoices",
        "CALLS",
        "Demo.Repositories.InvoiceRepository.GetInvoices",
    ) in edges
    assert (
        "Demo.Repositories.InvoiceRepository.GetInvoices",
        "CALLS_PROCEDURE",
        "GetInvoices",
    ) in edges
    assert ("GetInvoices", "READS", "Invoices") in edges
    assert ("Invoices", "REFERENCES", "Customers") in edges
    assert not knowledge.warnings


def test_semantic_chunks_and_parameters():
    knowledge = parse_repository(DEMO.resolve())
    documents = chunks(knowledge, "demo")
    method = next(c for c in documents if c.name.endswith("CalculatePendingAmount"))
    assert "TotalAmount - invoice.PaidAmount" in method.text
    summary = next(c for c in documents if c.name == "Demo.Services.InvoiceService")
    assert "Math.Max" not in summary.text
    assert method.metadata["permission"] is None
    controller = next(
        c for c in documents if c.name == "Demo.Controllers.InvoiceController.GetInvoices"
    )
    assert any("READS -> Invoices" in path for path in controller.metadata["relationship_paths"])
    proc = next(e for e in knowledge.database_objects if e.kind == "procedure")
    assert proc.metadata["parameters"] == [{"direction": "IN", "name": "customerId", "type": "INT"}]
    column = next(e for e in knowledge.database_objects if e.qualified_name == "Invoices.Id")
    assert any("PRIMARY KEY" in item for item in column.metadata["constraints"])


def test_ambiguous_overloads_and_dynamic_calls_remain_unresolved():
    source = b"""namespace Demo { class A {
    public void Run() { Foo(); unknown.Foo(); }
    public void Foo() {} public void Foo(int x) {}
    } }"""
    knowledge = csharp.parse(CodeFile("a", "a.cs", "csharp", "h", len(source)), source)
    resolve(knowledge)
    calls = [r for r in knowledge.relationships if r.kind == "CALLS"]
    assert len(calls) == 2
    assert all(r.target_id is None for r in calls)


def test_csharp_structural_features():
    source = b"""namespace Demo { interface IBase {} struct Point { public int X; }
    enum Status { Open, Paid } class Child : IBase {
    public int Amount { get; set; } public Child(int value) {} } }"""
    knowledge = csharp.parse(CodeFile("a", "a.cs", "csharp", "h", len(source)), source)
    resolve(knowledge)
    assert {"namespace", "interface", "struct", "enum", "field", "property", "constructor"} <= {
        s.kind for s in knowledge.symbols
    }
    assert any(r.kind == "INHERITS" and r.target_id for r in knowledge.relationships)


def test_malformed_file_does_not_stop_other_files(tmp_path):
    (tmp_path / "bad.cs").write_text("class Broken { public void Oops(")
    (tmp_path / "good.cs").write_text("class Fine { public void Run() {} }")
    (tmp_path / "bad.sql").write_text("CREATE TABLE broken (;")
    (tmp_path / "ignored.txt").write_text("irrelevant")
    knowledge = parse_repository(tmp_path)
    assert any(s.name == "Run" for s in knowledge.symbols)
    assert len(knowledge.files) == 4  # project text documentation is now indexed
    assert len(knowledge.warnings) >= 2


def test_dml_read_and_write_evidence():
    source = b"""CREATE PROCEDURE SaveInvoice(IN value INT)
    BEGIN
    INSERT INTO Invoices (Id) SELECT Id FROM Drafts;
    UPDATE Invoices SET PaidAmount = value WHERE Id = 1;
    DELETE FROM Drafts WHERE Id = 1;
    END;"""
    knowledge = sql.parse(CodeFile("s", "s.sql", "mysql", "h", len(source)), source)
    edges = {(r.kind, r.target_name) for r in knowledge.relationships}
    assert {("WRITES", "Invoices"), ("READS", "Drafts"), ("WRITES", "Drafts")} <= edges


def test_inline_sql_and_cte_are_not_fabricated_tables():
    source = b"""class Repo { void Find() { db.Query("SELECT Id FROM Invoices"); } }"""
    knowledge = csharp.parse(CodeFile("a", "a.cs", "csharp", "h", len(source)), source)
    resolve(knowledge)
    assert any(r.kind == "READS" and r.target_name == "Invoices" for r in knowledge.relationships)
    source = b"WITH recent AS (SELECT Id FROM Invoices) SELECT Id FROM recent;"
    knowledge = sql.parse(CodeFile("s", "s.sql", "mysql", "h", len(source)), source)
    assert not any(r.target_name == "recent" for r in knowledge.relationships)


def test_sql_comments_and_string_semicolons():
    source = b"""-- CREATE PROCEDURE Fake() BEGIN SELECT * FROM Secret; END;
    CREATE TABLE Notes (Id INT PRIMARY KEY, Text VARCHAR(200));
    INSERT INTO Notes (Id, Text) VALUES (1, 'hello; world');
    /* SELECT * FROM Invisible; */
    SELECT Id FROM Notes WHERE Text = 'it''s safe;';"""
    knowledge = sql.parse(CodeFile("s", "s.sql", "mysql", "h", len(source)), source)
    assert not knowledge.warnings
    assert {e.kind for e in knowledge.database_objects} >= {"table", "column", "insert", "select"}
    assert not any(r.target_name in {"Secret", "Invisible"} for r in knowledge.relationships)
    assert not any(e.name == "Fake" for e in knowledge.database_objects)
