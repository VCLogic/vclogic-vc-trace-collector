# Michael Hyatt live pilot — 2026-09-11

## Scope

This pilot used the requested profile as identity evidence:

- `https://www.thepitch.show/investors/michael-hyatt`

The source was retained as evidence and rejected from the corpus by the default
`thepitch.show` leakage rule. The resolved record identifies the BlueCat and
Dyadem co-founder, while preserving Michael S. Hyatt (the publishing executive)
as a competing identity rather than silently merging them.

## Reference-voice discovery

The successful public reference candidate was:

- `https://tanktalks.substack.com/p/tank-talk-michael-hyatt-hyatt-family`

The page names Michael Hyatt, BlueCat, and the Hyatt Family Office. Discovery
classified it as a podcast/reference-voice candidate with 0.90 identity
confidence. After explicit source and identity review, collection retained:

- the public episode page;
- an 83,424,564-byte MP3 enclosure;
- SHA-256 `2857a1d498332cd8f8ad03c013b3dd8be303ba7a84b27b8a303487063079fa58`;
- media duration 2,561.67 seconds (42:41), as independently read by `ffprobe`;
- source linkage from the MP3 artifact to the reviewed episode candidate.

This is a **candidate** sample, not a verified speaker profile. A human must
select a clean Michael-only interval before `review-voice` embeds it. The
pipeline will not label target speech as verified when the score or runner-up
margin is below the configured thresholds.

## Negative provenance finding

An older Spotify/Anchor episode URL redirected to an unrelated episode titled
“Swenglanese - talking business in the UAE.” The live collector preserved the
redirected provenance, assigned identity confidence 0.30 and material role
`unknown`, and did not create a reference-voice candidate. This is the expected
fail-closed result and demonstrates why identity validation must inspect final
content, not trust a URL slug.

## Verification performed

- Offline suite: 96 tests passed, one live test deselected at the time this
  report was updated.
- Opt-in network contract: the Michael Hyatt profile/voice discovery test
  passed against the public sites.
- Offline audiovisual acceptance: a supplied video is converted to audio,
  transcribed, diarized, matched against a human-approved reference embedding,
  filtered to the matched speaker, exported to `talks.jsonl`, and independently
  verified. A weak-match fixture is routed to review.

No provider credentials, downloaded media, or generated workspace files are
tracked by Git.
