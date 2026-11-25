# Verification Standards

Always run `python -m compileall` for changed Python modules and `git diff --check`. Use targeted unit/route tests where dependencies are installed. For graph changes, use `python main.py --dry-run`; for FastAPI routes, use a running server or `TestClient` after installing `requirements.txt`.

Provider-affecting changes need a sandbox/test credential or dry-run behavior. Do not validate by unintentionally creating real ads, orders, posts, products, emails, or paid video jobs. Report unmet verification prerequisites plainly.

Related: `runtime/bootstrap.md`, `integrations/`, `web/operations-api.md`.
