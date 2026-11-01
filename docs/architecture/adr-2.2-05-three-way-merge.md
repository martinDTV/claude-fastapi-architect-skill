# ADR 2.2-05 — Three-way merge of hand-modified generated files: opt-in, authenticated base, conflicts as records

Status: accepted (2026-10-31, decisions of the v2.2.0 review: opt-in, never default-on) and implemented in increment 3
(2026-11-01). The discovery proposed `files.allow_merge: [globs]`; the implementation narrows that to exact,
per-file authorisations with an ownership boundary, because a glob is too broad a permission for an operation that
rewrites a developer's file.

## Context

An evolution regenerates the integration points of a generated project (`main.py`, `api/router.py`,
`core/config.py`, `.audit-fastapi.json`, …). Since v2.0 a file edited by hand on such a point blocks the plan
(`manual_change_on_integration_point`): regenerating it would destroy the edit, and the tool never trades a
developer's code for an applicable evolution. Increment 0 wrote the pure merge (`merge.py`): three texts in, a
clean text or a list of conflicts out, no project touched. This record decides how that engine enters the planning,
verification and application of evolutions.

## Decision

1. **Two halves of an opt-in, both required.** The contract names each file under `files.merge[]` (evolution 1.5):
   the exact relative path (no globs, no directories, never the manifest, the journal or historical revisions),
   the transformation (`three_way_python` for `.py`, `three_way_json` for `.json`), the ownership boundary and
   `on_conflict: reject` (the only behaviour that exists). The command line adds `--allow-merge`. With one half
   missing the plan blocks as before (`manual_change_on_integration_point`, or
   `merge_not_authorized_on_command_line` naming the missing flag). Nothing merges by default, nowhere.
2. **BASE is authenticated.** BASE is the file as the generator produced it at the project's baseline, regenerated
   from the contract the manifest records, and it counts only when its digest equals the digest the manifest recorded
   for that file. A different digest (another tool version, a manipulated manifest, a contract that is not the one the
   file came from) is `merge_base_not_reconstructible`, with both digests; a file the manifest does not know is
   `merge_base_unknown`; a manifest whose contract is not the one its spec digest names is refused earlier
   (`baseline_contract_available`). Regenerating the current code is not assumed to produce the historical base: the
   recorded digest decides. LOCAL is the file on disk, read with its line endings; NEW is the generator's output for
   the evolved contract. The three digests travel in the plan, the journal step and the report.
3. **Ownership is declared.** `ownership.generator_may_change` (`file`, or regions), `ownership.developer_owns`
   (regions the generator may not touch) and `ownership.generator_owns` (regions where a manual change is refused),
   each region a top-level function or class of BASE, a `Class.method`, or a BASE line range. Outside those, the
   file is merged hunk by hunk. A manual hunk inside a generator-owned region is
   `manual_change_in_generator_region`; a generated hunk inside a developer-owned region is
   `generated_change_in_developer_region`; a generated hunk outside the authorised regions is
   `generated_change_outside_authorized_region`; a region that does not resolve to exactly one node is
   `merge_region_unresolved`.
4. **What is accepted.** Changes whose base ranges do not overlap, each side's additions present in the result
   (`merge_python`'s structural proof), the developer's comments and hunks present, no symbol defined twice, no
   import repeated or bound twice to different origins, no router registered twice under the same prefix, headers
   (decorators, signature, bases) of a top-level definition changed by at most one side, both sides parsing, the
   result parsing, line endings unchanged. Two sides changing the *body* of the same function at distinct lines merge
   (that is what a three-way merge is for); two sides changing its header do not.
5. **What is refused, each as a structured conflict** with the base lines and both sides' text:
   overlapping changes, insertions at the same point, a lost node, a header changed by both sides, a duplicate
   addition, symbol, import or registration, a conflicting import, a developer's comment or hunk missing from the
   result, a line-ending change (every line differs: nothing is merged), a side that does not parse, a JSON key
   changed differently by both sides. No marker is ever written into a project file; no global text substitution
   resolves anything; a parsing result is not taken for an equivalent one.
6. **The plan is the dry run.** `plan` computes the merged text and never writes it; the report lists, per file,
   the three digests, the result digest, the hunks taken from each side, the developer's hunks preserved, the diff
   from LOCAL to the result, the conflicts, the authorisation it required and the validations the copy will run.
7. **Apply never recomputes.** The merged text in the plan is what the recoverable writer publishes, under the
   project lock, after the writer's digest preconditions (LOCAL changed after the plan is a concurrent-change
   refusal) and after the planned result digest is re-checked (a plan altered after verification is refused). The
   journal records a `merge` step with the digests; a failure during publication leaves the journal for `recover`,
   which restores the developer's version. The manifest keeps recording the *generated* content of the file, so the
   manual hunks stay the developer's and the next evolution merges them again against the new base; the history
   entry names the files merged (`history[].merged`, manifest 1.6).
8. **Verification judges the merge, not the merge itself.** In the copy: the planned text is what landed (digest), it
   parses, no duplicate symbol, import or registration, the developer's hunks and comments are present, then the
   existing machinery: static audit against the project's own, public route contracts, protected files, and the
   generated tests under the trust model. A textually clean merge whose copy fails any of these is rejected.
9. **A verification copy carries no lock.** The project's lock file does not travel with the copy (a lock held by
   another process made the verification crash instead of ending in `locked`; found and fixed by this increment).

## Alternatives rejected

- Merging by default, or `files.allow_merge` with globs: too broad a permission for rewriting a developer's file; an
  exact path, a transformation and an ownership boundary are the smallest authorisation that says what will happen.
- Resolution policies (`ours`, `theirs`, "keep manual"): a policy decides without evidence; the only honest outcome of
  a conflict is a record and a blocked plan.
- Regenerating BASE from the current code, or trusting the current tool's output as the historical base: the recorded
  digest is the only authentication available without storing every generated file; when it fails the merge is
  refused rather than guessed.
- A concrete-syntax-tree dependency for semantic merges: outside the decision of v2.2 (ADR-2.2-04).
- Storing the generated base of every file inside the project to survive tool upgrades: deferred; the cost is a
  second copy of the project's generated code, and the refusal is explicit when the base cannot be reconstructed.

## Consequences

- Contract `fastapi-architect/evolution/1.5` (`files.merge`), report `evolution_report/1.6` (`plan.merges`,
  `plan.files.merged`, `verification.merges`, `contract_authorization.allow_merge`), manifest
  `fastapi-architect/generation/1.6` (`history[].merged`), journal step kind `merge`; `evolve_fastapi.py plan|apply
  --allow-merge`. Projects whose contracts have no `files.merge` behave exactly as before.
- After a tool upgrade that changes a template, a previously merged file cannot be merged again until its base can
  be authenticated (regenerate the project with the new version, re-apply the manual change): stated in the conflict.
- `.audit-fastapi.json` is wired for the JSON merge but today's evolutions never change it; the end-to-end scenario
  shows the manual keys preserved, the three-way behaviour is unit-tested.
- Scenarios A to E and the adversarial controls are in `tests/test_merge_integration_v22.py`; the guide is section 10
  of `docs/guides/evolving-generated-projects.md`; the contract is section 12 of `references/evolution-contracts.md`.
