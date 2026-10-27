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

## Conversational source review

Start the review in chat, not with instructions to run the CLI. Read the saved
identity and source plan; reuse an existing explicit identity confirmation.
Use small numbered batches (about five items, adjustable on request), prioritizing
first-person writing and the investor's own channel, then interviews/podcasts,
then uncertain matches. Do not claim channel ownership from a name match alone.

For every displayed item provide a clickable `[descriptive title](source URL)`
using the candidate's actual URL, source type, stored identity-confidence score,
short identity/relevance evidence, and proposed action and material role. State
the current decision status and any exclusions, per item or once for the batch
when shared. Label automated scores as heuristics, distinct from an agent's
evidence assessment; explain an apparent mismatch without silently changing the
score or treating a low score as irrelevant. Mark provisional types, blocked
pages, uncertain attribution and third-party material explicitly.
Use the schema roles: `authored_by_target` for the investor's own writing,
`spoken_by_target` for their speech, `third_party` for others' descriptions,
and `unknown` when attribution is unresolved. A published interview may contain
both interviewer and investor text; do not label the whole page first-person
merely because the investor is its subject.

A compact table or numbered list is sufficient. Links to local JSON/TSV files
may supplement the review but never replace clickable source links in chat.
Keep excluded items separate from an approvable batch and explain the rule.

Before presenting a batch, save its immutable mapping under
`outputs/<investor-slug>/discovery/review_batches/<batch-id>.json`. Use a new batch
ID, never overwrite an earlier one. Record investor slug, plan ID, batch ID and
each displayed number's candidate ID, URL, title, source type, confidence,
current status, evidence, exclusions, proposed action and proposed material role.
This is an agent review snapshot, not a new CLI input format. On resume, recover
the displayed mapping, not the current source-plan sort order.

End with a concrete choice, for example: "Approve batch 1, or approve 1 and 3;
reject 2; defer 4 and 5. Approval records decisions only; no downloads start."
Also allow "show more evidence for 3" without recording a decision. A request to
change the skill, a generic acknowledgement, or an ambiguous batch reference is
not approval. Ask for clarification if the referenced batch cannot be recovered.

Resolve a reply against the displayed snapshot. "Approve batch 1" applies only
to that batch's displayed candidates and proposed roles, never the whole source
plan or every item on that platform. Re-read those candidate IDs before applying
decisions; if their URL, type, role proposal or exclusions changed materially,
re-present the affected items. Already-applied identical decisions need no replay;
conflicting current decisions need clarification. Deferred and unmentioned items
remain unchanged. Do not use `--approve-all-eligible` to implement batch approval.

Record actual IDs and the user's chosen roles using the existing decision schema:

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
means no decision entry. Verify the saved statuses and roles after the review
command, report this batch's approved/rejected/deferred counts (not the CLI's
cumulative approval count), and offer the next batch. Discovery/review ends with
the saved plan, not downloads or processing; these require a separate user choice.
