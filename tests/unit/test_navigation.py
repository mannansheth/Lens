"""Navigation evidence extraction and client-facing structured destinations."""
from types import SimpleNamespace

from lens.repository.indexing import parse_repository
from lens.repository.knowledge import chunks
from lens.llm.answering import client_navigation
from lens.search.ranking_signals import signals
from lens.search.ranking import rank
from lens.core.models import DocumentChunk, SearchHit
from lens.llm.context import _relevant_navigation_links


def test_razor_navigation_links_resolve_to_controller_and_preserve_route_template(tmp_path):
    view = tmp_path / "Views" / "Journal" / "Index.cshtml"
    view.parent.mkdir(parents=True)
    view.write_text('''<nav><a asp-controller="Journal" asp-action="UpdateJournal">Edit journal</a>
<a href="/Journal/UpdateJournal?id=@journalId&year=@selectedYear">Edit selected journal</a>
<a href="@Url.Action("Index", "Journal")">Journals</a></nav>''')
    controller = tmp_path / "JournalController.cs"
    controller.write_text('''class JournalController {
public object Index() { return View(); }
public object UpdateJournal() { return View(); }
}''')

    knowledge = parse_repository(tmp_path)
    symbols = {symbol.id: symbol for symbol in knowledge.entities}
    view_symbol = next(symbol for symbol in knowledge.symbols if symbol.kind == "razor_view")
    resolved = [edge for edge in knowledge.relationships if edge.kind == "NAVIGATES_TO" and edge.source_id == view_symbol.id]
    assert len(resolved) == 2
    assert resolved[0].target_id and symbols[resolved[0].target_id].name == "UpdateJournal"
    helper = next(link for link in view_symbol.metadata["navigation_links"] if link.get("origin") == "razor_url_action")
    assert helper["controller"] == "Journal" and helper["action"] == "Index"
    assert helper["href"] == "" and helper["href_expression"] == '@Url.Action("Index", "Journal")'
    assert len(view_symbol.metadata["navigation_links"]) == 3
    literal = next(link for link in view_symbol.metadata["navigation_links"] if link.get("href", "").startswith("/"))
    assert literal["href"] == "/Journal/UpdateJournal?id=@journalId&year=@selectedYear"

    documents = chunks(knowledge, "fixture")
    view_document = next(document for document in documents if document.entity_id == view_symbol.id)
    assert view_document.metadata["navigation_candidate"]
    assert signals("Where can I edit the journal?", view_document)["navigation_match"]
    assert "Edit journal" in view_document.embedding_text
    assert "/Journal/UpdateJournal?id=@journalId&year=@selectedYear" in view_document.embedding_text


def test_react_router_routes_and_visible_links_support_client_navigation(tmp_path):
    (tmp_path / "App.jsx").write_text('''import { Route, Routes } from "react-router-dom";
import Profile from "./Profile";
export default function App() { return <Routes><Route path="/profile" element={<Profile />} /></Routes>; }
''')
    (tmp_path / "Profile.jsx").write_text('''export default function Profile() {
  return <main><h1>Profile settings</h1><label>Current password</label><button>Confirm password change</button></main>;
}
''')
    (tmp_path / "Navbar.jsx").write_text('''import { Link } from "react-router-dom";
export default function Navbar() { return <nav><Link to="/profile">Profile</Link></nav>; }
''')
    knowledge = parse_repository(tmp_path)
    profile = next(symbol for symbol in knowledge.symbols if symbol.name == "Profile")
    assert profile.metadata["incoming_routes"][0]["path"] == "/profile"
    assert profile.metadata["incoming_routes"][0]["source_file"] == "App.jsx"
    profile_chunk = next(chunk for chunk in chunks(knowledge, "fixture") if chunk.entity_id == profile.id)
    assert signals("How do I change my password?", profile_chunk)["navigation_route_feature_score"] > 0

    navbar = next(symbol for symbol in knowledge.symbols if symbol.name == "Navbar")
    navbar_chunk = next(chunk for chunk in chunks(knowledge, "fixture") if chunk.entity_id == navbar.id)
    assert navbar_chunk.metadata["navigation_links"][0]["label"] == "Profile"
    assert navbar_chunk.metadata["navigation_links"][0]["href"] == "/profile"
    context = SimpleNamespace(evidence={"E-profile": {
        "file": "Profile.jsx", "line_start": 1,
        "navigation_links": navbar_chunk.metadata["navigation_links"],
        "incoming_routes": profile_chunk.metadata["incoming_routes"],
        "navigation_relationships": [],
    }})
    result = client_navigation(context, ["E-profile"], "How do I change my password?")
    assert result["status"] == "verified_destination"
    assert result["destinations"][0]["label"] == "Profile"
    assert result["destinations"][0]["path_template"] == "/profile"
    assert result["destinations"][0]["routing_basis"] == "react_router_link"
    assert result["steps"] == [{"instruction": "Choose Profile", "evidence_id": "E-profile"}]


def test_react_dynamic_route_is_kept_as_a_template(tmp_path):
    (tmp_path / "Chat.jsx").write_text('''import { Link } from "react-router-dom";
export default function Chat({ id }) { return <Link to={`/chat/${id}`}>Chat</Link>; }
''')
    knowledge = parse_repository(tmp_path)
    page = next(symbol for symbol in knowledge.symbols if symbol.name == "Chat")
    link = page.metadata["navigation_links"][0]
    assert link["href"] == "/chat/{dynamic}"
    context = SimpleNamespace(evidence={"E-chat": {
        "file": "Chat.jsx", "line_start": 1, "navigation_links": [link],
        "navigation_relationships": [],
    }})
    destination = client_navigation(context, ["E-chat"], "Where can I chat?")["destinations"][0]
    assert destination["path_template"] == "/chat/{dynamic}"
    assert destination["direct_url"] is None
    assert destination["dynamic_values_present"]


def test_visible_heading_precedes_viewdata_title_and_links_keep_source_lines(tmp_path):
    view = tmp_path / "Views" / "Client" / "ClientIndex.cshtml"
    view.parent.mkdir(parents=True)
    view.write_text('''@{ ViewData["Title"] = "VendorIndex"; }
<h1>Clients @(ViewBag.Currency != null ? $"({ViewBag.Currency})" : "(ALL)")</h1>
<a href="/Client/GetInvoiceByBillingMasterId?Journald=@journalId">Invoices</a>''')
    knowledge = parse_repository(tmp_path)
    symbol = next(symbol for symbol in knowledge.symbols if symbol.kind == "razor_view")
    assert symbol.metadata["page_title"] == "Clients"
    link = symbol.metadata["navigation_links"][0]
    assert link["line"] == 3
    assert link["href"].startswith("/Client/GetInvoiceByBillingMasterId")


def test_client_navigation_returns_only_cited_literal_destinations():
    context = SimpleNamespace(evidence={"E-view": {
        "file": "Views/Journal/Index.cshtml", "line_start": 9,
        "navigation_links": [{"controller": "Journal", "action": "UpdateJournal",
                              "href": "/Journal/UpdateJournal?id=@journalId&year=@selectedYear",
                              "label": "Edit selected journal"}],
        "navigation_relationships": []}})
    result = client_navigation(context, ["E-view"], "Where can I edit the journal?")
    assert result["status"] == "verified_destination"
    destination = result["destinations"][0]
    assert destination["path_template"] == "/Journal/UpdateJournal?id=@journalId&year=@selectedYear"
    assert destination["direct_url"] is None
    assert destination["controller"] == "Journal"
    assert destination["action"] == "UpdateJournal"
    assert destination["page_title"] is None
    assert destination["query_parameters"] == ["id", "year"]
    assert destination["dynamic_values_present"]
    assert result["steps"] == [{"instruction": "Choose Edit selected journal", "evidence_id": "E-view"}]


def test_inline_javascript_location_template_is_preserved_without_runtime_values(tmp_path):
    view = tmp_path / "Views" / "Journal" / "Index.cshtml"
    view.parent.mkdir(parents=True)
    view.write_text('''<script>window.location.href = "/Journal/UpdateJournal?id=" + journalId + "&year=" + selectedYear;</script>''')
    knowledge = parse_repository(tmp_path)
    view_symbol = next(symbol for symbol in knowledge.symbols if symbol.kind == "razor_view")
    link = view_symbol.metadata["navigation_links"][0]
    assert link["href"] == "/Journal/UpdateJournal?id={journalId}&year={selectedYear}"
    assert link["dynamic_parameters"] == ["journalId", "selectedYear"]
    context = SimpleNamespace(evidence={"E-view": {"file": view_symbol.file_id,
        "line_start": link["line"], "navigation_links": [link], "navigation_relationships": []}})
    destination = client_navigation(context, ["E-view"], "edit journal")["destinations"][0]
    assert destination["dynamic_values_present"]
    assert destination["direct_url"] is None
    assert destination["query_parameters"] == ["id", "year"]


def test_placeholder_anchor_is_correlated_with_delegated_javascript_route(tmp_path):
    view = tmp_path / "Views" / "Journal" / "JournalDetails.cshtml"
    view.parent.mkdir(parents=True)
    view.write_text('''<a class="btn update-journal" data-id=@Model.JournalId href="#">Update Journal</a>
<script>
$(document).on("click", ".update-journal", function (e) {
  let journalId = $(this).data("id");
  let selectedYear = @ViewBag.SelectedYear;
  let url = `/Journal/UpdateJournal?id=${journalId}&year=${selectedYear}`;
  window.location.href = url;
});
</script>''')
    knowledge = parse_repository(tmp_path)
    symbol = next(symbol for symbol in knowledge.symbols if symbol.kind == "razor_view")
    links = symbol.metadata["navigation_links"]
    assert len(links) == 1
    assert links[0]["href"] == "/Journal/UpdateJournal?id={journalId}&year={selectedYear}"
    assert links[0]["label"] == "Update Journal"
    assert links[0]["selector"] == ".update-journal"
    assert links[0]["line"] == 1
    context = SimpleNamespace(evidence={"E-journal": {"file": symbol.file_id, "line_start": 1,
        "navigation_links": links, "navigation_relationships": []}})
    navigation = client_navigation(context, ["E-journal"], "How can I edit a journal's information?")
    assert len(navigation["destinations"]) == 1
    assert navigation["destinations"][0]["path_template"] == links[0]["href"]
    assert navigation["destinations"][0]["label"] == "Update Journal"
    assert navigation["steps"] == [{"instruction": "Choose Update Journal", "evidence_id": "E-journal"}]


def test_client_navigation_prefers_question_matched_branch_route():
    context = SimpleNamespace(evidence={"E-view": {"file": "Views/Client/ClientIndex.cshtml",
        "line_start": 10, "navigation_links": [
            {"href": "/Client/GetProformas?id={journalId}", "condition": "btn-proforma"},
            {"href": "/Client/GetInvoiceByBillingMasterId?Journald={journalId}&yearname={year}",
             "condition": "btn-invoice"},
            {"href": "/Client/GetReceiptByBillingMasterId?JournalId={journalId}", "condition": "btn-receipt"}],
        "navigation_relationships": []}})
    result = client_navigation(context, ["E-view"], "Where can I find invoices?")
    assert len(result["destinations"]) == 1
    assert result["destinations"][0]["action"] == "GetInvoiceByBillingMasterId"
    assert result["destinations"][0]["label"] == "Invoice"
    assert result["steps"] == [{"instruction": "Choose Invoice", "evidence_id": "E-view"}]


def test_context_navigation_filter_keeps_only_question_matched_route():
    links = [
        {"label": "Show All", "action": "Index"},
        {"label": "Invoice", "action": "GetInvoiceByBillingMasterId", "line": 188},
        {"label": "Receipts", "action": "GetReceiptByBillingMasterId"},
    ]
    selected = _relevant_navigation_links(links, "Where can I find invoices?")
    assert [link["action"] for link in selected] == ["GetInvoiceByBillingMasterId"]


def test_context_navigation_filter_drops_all_unmatched_links():
    links = [{"label": "Take Assessment Again", "href": "/assessment"}]
    assert _relevant_navigation_links(links, "How do I change my password?") == []


def test_client_navigation_does_not_return_an_unrelated_link():
    context = SimpleNamespace(evidence={"E-page": {
        "file": "ProfileSettings.jsx", "line_start": 10,
        "navigation_links": [{"label": "Take Assessment Again", "href": "/assessment"}],
        "incoming_routes": [{"path": "/profile", "source_file": "App.js", "line": 20}],
        "navigation_relationships": [],
    }})
    result = client_navigation(context, ["E-page"], "How do I change my password?")
    assert [item["path_template"] for item in result["destinations"]] == ["/profile"]
    assert result["steps"] == []


def test_controller_action_link_is_not_misreported_as_a_literal_url():
    context = SimpleNamespace(evidence={"E-menu": {"file": "Views/Shared/_Layout.cshtml",
        "line_start": 42, "navigation_links": [{"controller": "JournalOrder",
            "action": "JournalInvoiceSummary", "href": "", "href_expression":
            '@Url.Action("JournalInvoiceSummary", "JournalOrder")', "label": "Journal Invoice Summary"}],
        "navigation_relationships": []}})
    result = client_navigation(context, ["E-menu"], "Where can staff review journal invoices?")
    assert result["status"] == "action_only"
    target = result["destinations"][0]
    assert target["controller"] == "JournalOrder"
    assert target["action"] == "JournalInvoiceSummary"
    assert target["path_template"] is None
    assert target["route_expression"].startswith("@Url.Action")


def test_multi_term_feature_match_outscores_generic_shared_journal_word():
    generic = SearchHit(DocumentChunk("generic", "generic", "JournalController.AddJournal", "method",
        "Views/Journal/AddJournal.cshtml", "razor", "add a journal", {"navigation_candidate": True,
        "navigation_links": [{"label": "Add journal", "action": "AddJournal"}]}), hybrid_score=0.7)
    invoice = SearchHit(DocumentChunk("invoice", "invoice", "ClientController.GetInvoiceByBillingMasterId", "method",
        "Views/Client/ClientIndex.cshtml", "razor", "journal invoice list", {"navigation_candidate": True,
        "navigation_links": [{"label": "Invoice for journal", "controller": "Client",
            "action": "GetInvoiceByBillingMasterId", "href": "/Client/GetInvoiceByBillingMasterId?Journald={journalId}"}]}), hybrid_score=0.7)
    ranked = rank([generic, invoice], [], query="Where can I find all invoices for a particular journal?")
    assert ranked[0].chunk.id == "invoice"
    assert ranked[0].ranking_signals["navigation_match_score"] > ranked[1].ranking_signals["navigation_match_score"]
