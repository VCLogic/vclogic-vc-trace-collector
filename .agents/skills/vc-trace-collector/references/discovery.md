# Discovery and source review

In the user's terminal, start `wizard --stage discover --name "Investor Name"`.
Use `--investor existing-slug` to resume without overwriting identity.
The wizard accepts a known URL or searches possible profiles, confirms identity,
asks for platforms and search limits, then reviews sources. It downloads only
identity evidence pages at this stage, never video/audio or model weights.

In agent conversation, ask for a name and optional firm/profile. If only a name
is known, use available search tools to propose profile URLs with affiliation
evidence and retain query/results in the workspace discovery audit. If no search
tool is available, offer the wizard or ask for a profile URL; do not assume one.
For agent-native search, append JSONL entries to a separate
`outputs/<name-slug>/discovery/agent_profile_evidence.jsonl` with `query`,
`provider`, `retrieved_at`, `url`, `title`, `excerpt`, and
`validation_status: "unreviewed"`. Do not overwrite CLI-managed evidence files.
Ask which person is intended before running known-profile discovery:

```bash
uv run vc-trace-collector discover --name "Investor Name" \
  --known-profile-url "https://example.org/investor" --disable-public-search
```

Do not rerun `discover` over an existing workspace to change identity. Read its
`identity/resolved_identity.json`, `identity/identity_evidence.jsonl` and
`discovery/source_plan.json` first. Stop for an explicit identity correction if
they concern another person.

Offer platform choices: youtube, podcast, web_article, web_profile, rss_feed,
substack, medium, x, linkedin_export. X and LinkedIn may require authorized
backends or supplied exports. `doctor --json` reports capabilities, not proof that
a site is reachable. Missing Agent Reach/Exa is not zero relevant results.

```bash
uv run vc-trace-collector search-source --investor investor-name \
  --source youtube --backend agent-reach
uv run vc-trace-collector list-sources --investor investor-name
```

Use one search command per chosen platform. Generated queries are the default;
use repeatable `--query` only when needed. Explain limit/cost increases before
applying them. Do not ask for Whisper, pyannote, GPU or reference models here.

Show title, URL, source type, identity confidence, material role and exclusions.
Ask approve/reject/defer and group sources only when the user understands the
group. Record actual IDs and the user's chosen roles:

```json
[{"candidate_id":"candidate:actual-id","status":"approved",
  "material_role":"spoken_by_target","reason":"Human confirmed interview",
  "decided_by":"human:reviewer"}]
```

Save in a new decision file without overwriting the user's existing decisions.
Only after explicit human identity confirmation:

```bash
uv run vc-trace-collector review --investor investor-name \
  --decision-file selected-decisions.json --reviewer human:reviewer --confirm-identity
```

An approval is not an exclusion override or verified speaker attribution. Defer
means no decision entry. Discovery ends with the saved plan, not downloads.
