# Repository memory

Notes that apply across failures in this repository. Signature-specific notes
belong in `known_failures.jsonl`.

- Prefer the first error in a log over later cascade errors.
- Treat integration-test timeouts as infrastructure until proven otherwise.
