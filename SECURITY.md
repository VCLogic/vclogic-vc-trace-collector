# Security and privacy

## Supported data boundary

This project handles public web material and files deliberately supplied by the
operator. It is not designed to bypass authentication, paywalls, robots
controls, platform access restrictions, or technical protection measures.

## Credentials

Never add `.env` files, cookies, browser profiles, API keys, access tokens,
provider credentials, model caches, or private exports to version control.
Provider secrets are read from environment variables and are redacted from
structured audit payloads. `.env.example` contains variable names only.

The reference projects were inspected as read-only inputs. A hard-coded token
found in an old experimental transcription file was deliberately not copied.
Anyone responsible for that source repository should rotate the token if it was
ever valid and remove it from all reachable history and artifacts.

## Network protections

The HTTP collector permits only `http` and `https`, resolves hostnames before
requests, rejects loopback/private/link-local/reserved targets, and validates
every redirect destination. Response sizes, redirects, retries, timeouts, and
per-host request intervals are bounded. These controls reduce SSRF and resource
exhaustion risk; operators should still run untrusted jobs with OS-level
isolation and egress controls.

## Untrusted content

Fetched pages, transcripts, feeds, media metadata, and supplied documents are
untrusted data. They are never treated as instructions. LLM discovery receives
bounded structured evidence and its response must validate against a strict
schema. Public audit logs contain action summaries and validation results, not
private chain-of-thought.

## Reporting

Report vulnerabilities privately to the repository owner. Include a minimal
reproduction, affected version, impact, and suggested mitigation. Do not include
live credentials or private personal data in a report.

