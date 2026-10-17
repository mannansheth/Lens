# Application database questions

`db-check` checks whether a discovered application database can be reached. `db-schema` reads metadata; it does not return application rows. `sql` combines the natural-language request with inspected schema and stored-routine metadata where supported, generates SQL, validates it, displays it for review, and asks before execution.

Configuration discovery supports connection URLs and common key/value settings, plus .NET `ConnectionStrings` in `appsettings*.json`. For MySQL, Lens supports environment variables and configuration fields such as host, user, password, and database when the project provides MySQL provider evidence. Use `--env-file`, `--config-file`, or `--variable` to disambiguate configuration explicitly.

The SQL validator accepts a single read-only SELECT, SELECT CTE, or set operation (UNION, INTERSECT, EXCEPT), and rejects multiple statements and write/DDL operations. It checks referenced tables and known foreign-key relationships, constrains functions, and rejects sensitive wildcard selection. Execution uses the database driver by default; for MySQL, `--executor mysql` can invoke a local MySQL client. Results are row-limited and displayed as a table.

These controls do not replace least-privilege database accounts, host-application authorization, a network boundary, query timeouts, or operational monitoring. SQL answers are not role-aware by themselves. An application exposing Lens to end users must enforce authorization before calling this workflow and must decide whether to reveal returned data.
