# Full-stack static analysis fixture

This small source fixture demonstrates a journal submission: JavaScript calls a C# controller, the controller calls a Python sentiment stub and a repository, and the repository inserts into a SQL table. A Razor view submits to the same controller action.

It is intentionally a static analysis fixture, not a deployable application. The C# database connection is illustrative; no database or external service is contacted by parsing or tests. The Python scorer is deterministic and downloads no weights. Environment values are harmless placeholders and are omitted from indexed configuration evidence.

Run `lens parse examples/fullstack-app` to inspect extracted symbols and relationships.
