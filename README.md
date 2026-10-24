# edge-platform

EDGE's shared platform package: the pieces every box and repo in the estate needs to agree on,
in ONE versioned place instead of hand copies kept equal by comments.

| tenant | what | status |
|---|---|---|
| `edge_platform.notify` | the ntfy.sh push budget: the policy file, its reader, the declared sender registry, the Alertmanager night-window check | v0.1.0 |
| `edge_platform.fence` | the trading-hours deploy fence (plans `deploy-fence-replacement`) | reserved |

```
pip install "edge-platform @ https://github.com/mjdevaccount/edge-platform/releases/download/v0.1.0/edge_platform-0.1.0-py3-none-any.whl"
```

## notify

ntfy.sh gives an anonymous publisher **250 messages per UTC day per source IP**, and every sender
on the home IP shares them. Three files say everything about that budget:

| file | says | read by |
|---|---|---|
| `ntfy-policy.json` | HOW MUCH: reserve, per-publisher cap and its exemptions, the night window, tiers, the send-ledger layout | `policy.py` (stdlib only), edge-host `lib-notify.sh`, trade-data `ntfy_client.py`, llm box `lib-notify.ps1` |
| `ntfy-policy.cases.json` | the literal cases every reader runs in its own language, and `policy_sha256` pinning the policy bytes | each consumer's CI |
| `ntfy-publishers.json` | WHO spends it: every sender, its route (`library` / `direct` / `am-webhook` / `gatus` / `ci`) and mode (`gated` / `counted` / `exempt` + why / `uncounted` + what is owed) | `publishers.py` |

THIS REPO IS CANONICAL for all three. v0.1.0 ships the policy bytes consumers already carried
(sha256 `186af626…`) unchanged, so no copy had to move; the policy's own `about` text still names
edge-host as canonical and is corrected in the first release that changes the bytes. A vendored
copy in a consumer is transitional: it proves itself equal with `edge-notify check-copy`, and its
exit is pinning this release instead.

### `edge-notify` (the CLI consumers run)

| command | for |
|---|---|
| `edge-notify am-night alertmanager.yml` | trade-infra CI: the Alertmanager `quiet-night` mute equals `policy.night`, minute-exact, and some route actually mutes with it |
| `edge-notify check-copy FILE` | a vendored policy copy is byte-identical (LF) to this release |
| `edge-notify check-pin [POLICY [CASES]]` | the cases file pins the policy's sha256 |
| `edge-notify shell` / `rows` | `_ENP_*` lines for lib-notify.sh; the shared cases as TAB rows for a bash test |
| `edge-notify senders` | the registry, validated against the policy's exemptions |

Exit 0 = clean, 1 = findings, 2 = BLIND (unreadable or invalid input) -- a crashed check never
reads as a passing one. Run it without installing: `uvx --from <wheel URL> edge-notify ...`.

## Release

Bump `version` in `pyproject.toml` and `edge_platform/__init__.py`, merge, tag `vX.Y.Z`. The
release workflow refuses a tag that disagrees with either, then attaches the wheel, the sdist,
the three notify files and `SHA256SUMS`.
