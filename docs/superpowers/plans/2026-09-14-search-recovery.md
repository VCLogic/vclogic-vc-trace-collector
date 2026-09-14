# Search Recovery Implementation Plan

1. Add failing tests for Agent Reach preflight and unreachable-backend accounting.
2. Add failing tests for custom source queries, audited search-limit increases, and YouTube channel exclusions.
3. Implement provider diagnostics that distinguish unavailable backends from completed provider attempts and stop repeated doomed queries.
4. Extend `search-source` with repeatable `--query` and `--max-search-operations` options.
5. Preserve structured YouTube channel metadata through observations and source candidates so the existing leakage firewall rejects The Pitch Show material explicitly.
6. Update operator documentation, changelog, and the repository skill; run focused and full verification.
