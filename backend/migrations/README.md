Schema v1: users, audit, datasets (JSON write sequence), configs (bounded LSM parameters), trials (immutable input snapshot and JSON result). MySQL stores metadata only. Run python -m scripts.seed_demo for create-if-missing and natural-key seed; existing values are not overwritten.

Schema v2 (additive, created by Base.metadata.create_all on startup): drill_sessions for the optimistic transaction drill.
- id / unique name / description / nullable dataset_id foreign key / created_at / updated_at.
- state JSON is the authoritative engine state: physical segments with per-key logical versions and tombstones, commit_seq, monotonic transaction sequence, every transaction's start snapshot, read-range predicates, staged writes and terminal commit result, plus the event log.
- The whole state is rewritten after each transition, so a backend restart resumes in-flight transactions with predicates intact. No migration of v1 tables is required.
