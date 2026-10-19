# Lens Python client

This is a small, dependency-free HTTP client for a running Lens engine. It does not install or start the Lens engine, download models, read repositories, authenticate users, or execute SQL.

For local development, install it from this checkout:

```sh
python -m pip install ./sdks/python
```

Example:

```python
from lens_client import LensClient

lens = LensClient("http://127.0.0.1:8765", api_key="read-from-server-secret-store")
answer = lens.ask("How do I update my contact details?", mode="client")
if answer["output"].get("abstention"):
    show_not_found(answer["output"]["abstention"])
else:
    render(answer["output"], answer["citations"])

sql = lens.sql("Count active customer accounts", user_id=authenticated_user_id,
               record_scope="user_records_only")
review_or_execute_with_host_policy(sql["sql"], sql["dialect"])
```

The `sql` method returns generated and validated SQL only. The host application decides whether it is authorized to execute that statement. API details and the full contract are in [`docs/release/deployment.md`](../../docs/release/deployment.md) and [`docs/release/openapi.yaml`](../../docs/release/openapi.yaml).
