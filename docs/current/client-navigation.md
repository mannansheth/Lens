# Client navigation and feature questions

Client mode is intended to explain application features in user-facing language and to expose actionable destinations when the indexed source provides them. It is invoked with `lens ask "<question>" --mode client`.

During indexing, Razor views can contribute visible link labels, literal URLs, MVC controller/action tag-helper targets, route values, forms, and selected inline JavaScript URL constructions. These facts are linked to view and controller evidence. Retrieval expands the candidate set for navigation-like questions and ranks route and feature evidence explicitly. The answer layer can return explanatory text together with structured navigation fields, including destination templates, controller/action, evidence locations, and steps supported by the retrieved context.

The output is source-grounded. A dynamic route such as `/Journal/UpdateJournal?id={journalId}` is a template: Lens does not know the actual journal identifier or application hostname unless those values are supplied by the host application. If evidence does not establish a page or route, client mode should state that limitation rather than invent a menu path. Menu hierarchy and runtime authorization remain outside static analysis.

This workflow is currently specialized around MVC/Razor navigation evidence. It should not be read as a guarantee that every frontend framework's router, menu, or runtime-generated URL is modeled.
