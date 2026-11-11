"""POST one <service>/admin/run/<kind> -- the shared Windmill step every scheduled flow calls.

TEMPLATE shipped by edge-platform (M6 PL3; lifted from trade-data windmill/u/admin/post_admin_run.py
and its trade_data_headers helper). A consumer copies it into its own Windmill tree with
`python -m edge_platform.windmill copy post_admin_run <windmill-root> <script-path>` and a flow
references it by PATH, with no body of its own:

    - id: run
      value:
        type: script
        path: u/admin/post_admin_run
        input_transforms:
          service_url: {type: static, value: "http://my-service:8090"}
          key_variable: {type: static, value: "u/admin/my_service_api_key"}
          kind: {type: static, value: auto_tag}
          params: {type: javascript, expr: "({symbol: flow_input.symbol ?? 'SPY'})"}

THE SERVICE IS A REQUIRED INPUT, AND SO IS ITS KEY WHENEVER A KEY IS SENT. The trade-data copy
defaulted both (its caddy URL, its key variable), so a step that forgot them called trade-data with
trade-data's key -- the wrong service, or the right one anonymously, and green either way. Windmill
passes null for every input a step does not list, so "required" is enforced HERE at run time, not
only in the schema: a missing or blank `service_url` raises before any request is sent, and so does
a blank `key_variable` unless the step says `send_key: false`. A `send_key: false` step names a
service that verifies no key, so it has no key to name (v0.10.1: v0.10.0 demanded one anyway and
would have failed every keyless flow sharing the script, e.g. trade-quant's in trade-data's
workspace); any `key_variable` it passes is ignored, never read.

WHAT IT KEEPS, on purpose:

- THE BODY ON FAILURE. `RuntimeError(f"HTTP {status}: {text[:400]}")`, never `raise_for_status()`:
  a 503 shed carries its reason (`degraded_planned`, `rth_fenced`) in the body, and an error
  handler that classifies a managed skip reads the job result. Dropping it turns a deploy window
  into a page.
- A BLANK IS NOT SENT. A `params` / `body` value of None or "" is dropped before the request.
- `fail_when`: a 200 can still be a failure (a grader whose work-list JAMMED answers 200 with
  `worklist_jammed: true`). It maps a body key to the value that fails the step, matched on TYPE and
  value (`{"ok": false}` -- a missing key or a 0 is not a `false`); the raise carries the body.
- THE KEY IS FAIL-OPEN ON AN EMPTY OR UNREADABLE VARIABLE (sends no X-API-Key, prints one line to
  the job log) so a flow does not start failing before its credential is provisioned. That is safe
  only when the SERVICE can see an uncredentialed caller (its API-key guard refuses or ledgers the
  write); `require_key: true` makes it fail-closed instead. `send_key: false` names a service that
  verifies no key, so another service's key never travels to it.
- `import wmill` at MODULE level, outside any try: Windmill provisions dependencies from a static
  scan of module-level imports. Nested inside a function it was never installed, every call raised
  ModuleNotFoundError into a bare except, and all 60 credentialed trade-data flows sent anonymous
  requests for a whole rollout.
"""

import httpx
import wmill

DEFAULT_ROUTE = "/admin/run"
DEFAULT_TIMEOUT = 60.0


def _or_default(value, default):
    """None or "" -> the default (Windmill's null for an unlisted input, or a blank UI field)."""
    return default if value is None or value == "" else value


def _required(name, value):
    if value is None or not str(value).strip():
        raise ValueError(f"post_admin_run: input {name!r} is required (got {value!r}); no default is assumed")
    return str(value).strip()


def _sent(values):
    """The mapping minus None / "" values. None stays None (no body at all); {} stays {}."""
    if values is None:
        return None
    return {k: v for k, v in values.items() if v is not None and v != ""}


def _failed_on(out, fail_when):
    """The `fail_when` pairs the 200's body matches, on type AND value (True == 1 in Python, so a
    bare == would fail a step on a count of 1)."""
    if not fail_when or not isinstance(out, dict):
        return {}
    return {k: v for k, v in fail_when.items() if k in out and type(out[k]) is type(v) and out[k] == v}


def auth_headers(key_variable, require_key=False):
    """{"X-API-Key": <key>} when the variable resolves non-empty; else None, or a raise if required."""
    try:
        k = wmill.get_variable(key_variable)
    except Exception as e:
        if require_key:
            raise RuntimeError(f"post_admin_run: reading {key_variable} raised {type(e).__name__}") from e
        print(f"post_admin_run: reading {key_variable} raised {type(e).__name__}; sending WITHOUT X-API-Key")
        k = ""
    if not k and require_key:
        raise RuntimeError(f"post_admin_run: {key_variable} is empty and require_key is set")
    return {"X-API-Key": k} if k else None


def main(
    service_url: str,
    key_variable: str = None,  # required unless send_key is false; checked below, not by the signature
    kind: str = None,  # required (checked below); a default only because key_variable now has one
    params: dict = None,
    body: dict = None,
    timeout: float = DEFAULT_TIMEOUT,
    route: str = DEFAULT_ROUTE,
    send_key: bool = True,
    require_key: bool = False,
    fail_when: dict = None,
):
    service_url = _required("service_url", service_url).rstrip("/")
    # `is not False` / `is True`, never truthiness: an unlisted input's null keeps the safe default.
    send_key = send_key is not False
    require_key = require_key is True
    # The key's variable is required exactly when a key is sent; a keyless step never reads one.
    key_variable = _required("key_variable", key_variable) if send_key else None
    kind = _required("kind", kind)
    # A step's OMITTED argument arrives as None, not as the default, so each default is applied here
    # from the constant the signature names (the first trade-data sync posted to '/None/admin/run/...').
    route = _or_default(route, DEFAULT_ROUTE)
    timeout = _or_default(timeout, DEFAULT_TIMEOUT)
    r = httpx.post(
        f"{service_url}{route}/{kind}",
        params=_sent(params),
        json=_sent(body),
        headers=auth_headers(key_variable, require_key) if send_key else None,
        timeout=float(timeout),
    )
    if r.status_code >= 400:
        # the body matters: a 503 shed carries its reason, which an error handler reads from the job result
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    out = r.json()
    hit = _failed_on(out, fail_when)
    if hit:
        raise RuntimeError(f"HTTP {r.status_code} body matched fail_when {hit}: {r.text[:400]}")
    return out
