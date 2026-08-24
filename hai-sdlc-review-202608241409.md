# HAI-SDLC review — ibook2epub

## Anchor

| Field | Value |
|---|---|
| Repo | ibook2epub |
| Version | 1.2.1 |
| HEAD | `e424339` |
| Branch | `fix/review-findings` |
| Tree | clean |
| Profile | **deep** (full tree, no finding caps) |
| Scope | full tree — first run, no prior review found |
| Passes run | 8, 9, 10 (Polish group). User asked for a readability focus. |
| Passes NOT run | 1–7 (Correctness, Completeness, Security, Robustness, Time, Space, Performance) |
| Date | 2026-08-24 |
| Models | Polish group delegated to opus; agent terminated on a harness API error and the passes were completed inline in the main session at the same tier. |
| Prior state | none — no `hai-sdlc-review-*.md`, no dispositions file |
| Output dir | repo root (no `plans/` or `docs/`) |

**Analysis only. No source file was modified by this run.**

### Delegation note

The Polish subagent was launched per protocol and terminated early with `API Error: The
response stopped arriving` after 34 tool calls. This was the third harness failure of the
session — the `code-review` skill failed the same way twice earlier. Rather than retry a
mechanism failing repeatedly, passes 8–10 were completed inline. No pass was dropped and no
finding is reported from the architecture summary alone; every line below was read in the
working tree.

## Pass 0 — architecture summary

CLI converting Apple iBooks `*.epub/` package directories into spec-valid zipped `.epub`
files. Stdlib-only by default, one optional extra (`disarm`) for transliterated names.
2,479 source lines across 10 modules, 2,970 test lines across 8 files.

Dependency direction is acyclic and clean: `spec`, `app_logger` and `naming` are leaves;
`convert` sits at the top and imports everything else. One deliberate `TYPE_CHECKING` cycle
break lets `planning` type against `convert.Report`.

Flow: `main` → `parse_args` → `build_policy` → `_run_export` → acquire output lock →
`sweep_partials` → `plan_exports` (once) → `cap_exports` → `export_planned` →
`asyncio.gather` over a `ThreadPoolExecutor` → `zip_package` per book (mkstemp `.part`,
validate, `Path.replace`) → `_zip_and_record` mutates a shared `Report` under
`_REPORT_LOCK`. `--list` and `--verify` short-circuit before the output directory is created.

Load-bearing invariant: **the output directory is the only record of completed work.** No
state file. Identity is recomputed by globbing `*.epub` and re-deriving each name. Anything
producing a name that glob cannot match breaks reruns permanently.

## Roll-up

| Pass | Findings | Critical | High | Medium | Low | Info |
|---|---|---|---|---|---|---|
| 8 — Observability | 3 | 0 | 0 | 1 | 2 | 0 |
| 9 — Maintainability | 5 | 0 | 0 | 3 | 2 | 1 |
| 10 — Readability | 12 | 0 | 0 | 5 | 7 | 0 |
| **Total** | **20** | **0** | **0** | **9** | **11** | **1** |

No critical or high findings. Nothing here blocks shipping; this is a codebase in good
health being read closely for polish.

## Pass 8 — Observability

| # | File:Line | Severity | Category | Finding | Evidence | Proposed Fix |
|---|-----------|----------|----------|---------|----------|--------------|
| 8.1 | `epubconvert/inspect_output.py:53,58` | medium | poor-log-context | The two containment refusals log which path was rejected but not what the tool decided, and they sit at `debug`. A book silently loses its cover at default verbosity. The sibling messages at :107 and :116 both name the consequence ("No cover for %s"). | `logger.debug("%r escapes %s", href, package.name)` — compare `logger.debug("No cover for %s: %s", target_archive.name, exc)` | Restore the consequence to both: `logger.debug("No cover for %s: %r escapes the package", target_archive.name, href)`. `_contained_file` needs the archive name passed in, or should return a reason string for `extract_cover` to log. |
| 8.2 | `epubconvert/convert.py:249` | low | poor-log-context | `"Skipped object: <%s>"` is the only message in the package wrapping a value in angle brackets, and "object" is used nowhere else for a package member. Log output is inconsistent when TRACE is on. | `logger.trace("Skipped object: <%s>", path.name)` vs 44 other call sites using bare `%s` | `logger.trace("Excluded from archive: %s", path.name)` — matches `is_excluded`, the function that made the decision. |
| 8.3 | `epubconvert/convert.py:968` | low | poor-log-context | `"Ending the convert application."` leaks the module name into user-facing output and says nothing the exit code does not. | `logger.debug("Ending the convert application.")` | Drop it, or replace with something actionable: `logger.debug("Run finished: %d exported, %d failed", report.exported, report.failed)`. |

## Pass 9 — Maintainability

| # | File:Line | Severity | Category | Finding | Evidence | Proposed Fix |
|---|-----------|----------|----------|---------|----------|--------------|
| 9.1 | `epubconvert/convert.py:0` | medium | god-module | 970 lines holding five separable responsibilities, in a package where every other module has exactly one job. `convert.py` is the residue everything else was extracted from, and it is now 3× the next largest module. | Section boundaries are already clean: discovery 118–177, archive writing 178–270, concurrency+reporting 272–480, output-dir bookkeeping 557–700 + 753, CLI driver 791–970 | Extract two modules along seams that already exist: `archive.py` (`_entry`, `zip_package`, `is_excluded`, `collect_package_dirs`) and `run.py` (`_log_preamble`, `_run_listing`, `_run_verify`, `_plan_options`, `_run_export`, `main`). Leaves `convert.py` as the exporter proper at roughly 300 lines. |
| 9.2 | `epubconvert/planning.py:269-274` | medium | duplication | The DRM and INCOMPLETE branches differ only in which counter they increment; the `logger.warning` call is byte-identical. The status→counter→message mapping is then written a second time at 276–286. Adding a sixth status means editing three places. | Lines 271 and 274 are the same statement: `logger.warning("Skipped, %s: %s", decision.reason, decision.package.name)` | Replace the if/elif chain with a table: `_COUNTERS = {ALREADY: "skipped", COLLISION: "collisions", DRM: "drm", INCOMPLETE: "incomplete"}` plus a parallel `_SUMMARY` dict of format strings, then one loop and one summary loop. |
| 9.3 | `epubconvert/inspect_output.py:39` | medium | missing-abstraction | The containment rule is split across two modules: `escapes_archive` in `validate.py`, its resolved-path partner `_contained_file` in `inspect_output.py`. A future reader finds half the rule, and a new caller (fonts, thumbnails, metadata sidecars) has no obvious seam to reuse. | `_contained_file` imports `escapes_archive` across a module boundary to complete a single security check | Move `_contained_file` into `validate.py` beside `escapes_archive` and export it. Both halves of one rule then live together, and `inspect_output.py` imports one name instead of two. |
| 9.4 | `epubconvert/planning.py:36` | low | hardcoded-config | `on_collision: str = "skip"` repeats the literal instead of referencing `SKIP`, which is defined 20 lines below in the same file. Renaming the mode would leave the default silently wrong. | `on_collision: str = "skip"` vs `SKIP = "skip"` at line 55 | Move the `SKIP`/`SUFFIX`/`COLLISION_MODES` block above the dataclass and write `on_collision: str = SKIP`. |
| 9.5 | `tests/:0` | low | test-structure | Eight test files for ten source modules, and the mapping is neither one-to-one nor consistently feature-based. `inspect_output.py` has no `test_inspect_output.py`; its tests live in `TestCovers` (test_export.py) and `TestVerifyMode` (test_validate.py). A reader changing `extract_cover` has no obvious place to look. | `ls tests/` → no `test_inspect_output.py`; `grep -rl inspect_output tests/` → 1 file | Either add `test_inspect_output.py` and move `TestCovers` + `TestVerifyMode` into it, or add a one-line map at the top of `tests/conftest.py` saying which file covers which module. |
| 9.6 | `pyproject.toml:55-99` | info | missing-linter | Structural checks pass. `.git/` present, tests present (8 files, 237 passing), linter config present and strict: ruff with 15 rule families, mypy `--strict` with `warn_unreachable`, pylint at 10.00/10. Each deviation carries a written justification comment. Confirms the "all four gates clean" claim. | `python3 -m ruff check .` → All checks passed; `mypy` → Success: 22 files; `pylint` → 10.00/10; `pytest` → 237 passed | None. |

## Pass 10 — Readability

| # | File:Line | Severity | Category | Finding | Evidence | Proposed Fix |
|---|-----------|----------|----------|---------|----------|--------------|
| 10.1 | `epubconvert/cli.py:207` | medium | outdated-comment | The `--no-shuffle` help says it takes "the first N **packages** in sorted order". Since `cap_exports` replaced `select_packages`, it takes the first N **pending books**. This is precisely the semantics whose earlier mismatch caused the flag to stall reruns, so the stale text points a user back at the old broken mental model. The README was corrected; the `--help` string was not. | `"Take the first N packages in sorted order instead of a random selection when --max-export-files applies."` vs `cap_exports` filtering on `decision.status == PENDING` | `"Take the first N books still needing export, in sorted order, instead of a random selection when --max-export-files applies."` |
| 10.2 | `epubconvert/naming.py:275` | medium | dead-code | `portable_available()` is defined, documented and exported, and referenced nowhere — not in `epubconvert/`, not in `tests/`, not in the README. It advertises a capability check that nothing performs; `build_policy` instead lets `PortableNaming.__init__` raise. | `grep -rn portable_available .` returns only the definition line | Delete it. If a pre-flight check is wanted, call it from `cli.parse_args` so `-p romanize` fails at argument time rather than after the preamble is logged. |
| 10.3 | `epubconvert/inspect_output.py:1-8` | medium | misleading-docs | The module docstring frames all three operations as reading the output directory ("Reading the output directory back... lifting a cover image out beside a book"). `extract_cover` reads the **source package**, and its own docstring says so explicitly. A module docstring and a function docstring inside it contradict each other. | Module: "These are the operations that read it again". `extract_cover:68`: "The image is read from the source package rather than from the archive that was just written" | Reword the module docstring to cover both directions: "...and writing a cover image beside a book, read from the source package rather than from the archive just written." |
| 10.4 | `epubconvert/planning.py:41` | medium | missing-types | `Decision.status: str` where five module constants are the only legal values — and those values are a documented public contract, enumerated in the `--list` help text and emitted verbatim by `--list --json`. mypy `--strict` cannot catch a typo in a status comparison. | `status: str` at :41; `--list` help enumerates "(pending, exported, collision, drm, incomplete)"; `render_listing` emits `"status": decision.status` | `Status = Literal["pending", "exported", "collision", "drm", "incomplete"]`, then `status: Status`. The five constants keep their names and gain `: Status` annotations. Same treatment for `PlanOptions.on_collision`. |
| 10.5 | `epubconvert/convert.py:362,509` | medium | dead-code | After the plan-once restructure, `export_packages` and `count_pending` are reachable only from tests — production goes `_run_export` → `export_planned` and `count_pending_decisions`. Both carry long docstrings describing invariants that now live in `plan_exports`, so a reader meets the authoritative-looking explanation on the path nothing calls. | AST reference scan: both symbols used by `tests/` only, zero production callers. `grep -n "export_planned\|count_pending_decisions" epubconvert/` shows the live path. | Keep `export_packages` as the documented library entry point but say so in one line ("Convenience wrapper: plan then export. `main` plans once itself and calls `export_planned`."). Delete `count_pending`, and point its two test classes at `count_pending_decisions`. |
| 10.6 | `epubconvert/planning.py:50` | low | naming-inconsistency | `ALREADY = "exported"` is the only one of the five status constants whose name and value disagree; the other four match exactly. The value is what users see in JSON output, so code and output use two different words for one state. | `PENDING = "pending"`, `COLLISION = "collision"`, `DRM = "drm"`, `INCOMPLETE = "incomplete"`, `ALREADY = "exported"` | Rename the constant to `EXPORTED`. Three call sites in `epubconvert/`, three in `tests/`. The wire value does not change. |
| 10.7 | `epubconvert/planning.py:48-59` | low | format-inconsistency | Three unrelated constant groups run together with one comment covering only the first. `COLLISION` (a decision status) sits six lines from `COLLISION_MODES` (a tuple of collision *strategies*) — two meanings of the word, adjacent, undivided. | `#: Decision statuses.` covers 49–53; 55–57 (modes) and 59 (`MAX_SUFFIX`) have none | Add `#: How --on-collision may be set.` above `SKIP` and `#: Highest " (n)" suffix the planner will try.` above `MAX_SUFFIX`, with a blank line between each group. |
| 10.8 | `epubconvert/convert.py:606` | low | missing-types | `_read_lock_holder(handle: object)` forces two `# type: ignore[attr-defined]` suppressions on consecutive lines. The caller always passes the `TextIOWrapper` from `path.open()`, so the real type is known. The suppressions sit at exactly the point where the code does something unusual and most wants checking. | `def _read_lock_holder(handle: object) -> str:` then `handle.seek(0)  # type: ignore[attr-defined]` and `handle.read()  # type: ignore[attr-defined]` | `from typing import TextIO`, then `handle: TextIO`. Both ignores delete. |
| 10.9 | `epubconvert/naming.py:83` | low | naming-inconsistency | `split_extension` is public and `_replace_illegal` private, though they were added together, are adjacent, and neither is used outside `naming.py` — including by tests. The visibility split implies an external contract that does not exist. | `def split_extension(...)` at :83, `def _replace_illegal(...)` at :105; AST scan shows no importer outside `naming.py` | Rename to `_split_extension`. If it is meant as public API, give it a test — the extension-preservation behaviour is currently covered only indirectly through `strip_unsafe`. |
| 10.10 | `epubconvert/convert.py:368` | low | naming-inconsistency | The keyword parameter `naming=` holds a policy object. Everywhere else in the package the concept is `policy` (`plan_exports(..., policy, ...)`, `build_policy`, `NamingPolicy`, `_log_preamble(args, policy)`), and `naming` is also the name of the module the type comes from. Callers read `naming=policy`. | `naming: NamingPolicy | None = None` at :368, immediately assigned `policy = naming if naming is not None else PassthroughNaming()` at :392 | Rename the parameter to `policy`. Two call sites in `tests/`, none in production. |
| 10.11 | `epubconvert/validate.py:171,182` | low | naming-inconsistency | `is_remote` and `escapes_archive` are adjacent and their one-line summaries are near-identical — "Report whether an href names a resource outside the archive" and "Report whether a resolved archive path points outside the archive" — for two unrelated conditions (a legitimate remote URL vs a path-traversal attempt). A maintainer skimming summaries will conflate them. | Both summaries quoted above, 11 lines apart | Retitle for the distinction: `is_remote` → "Report whether an href is a URL rather than an archive member." `escapes_archive` → "Report whether a path resolves above the archive root." |
| 10.12 | `epubconvert/validate.py:143` | low | outdated-comment | `find_opf_path`'s `:raises ValidationError:` says "If the container is missing or names no rootfile". It now also raises when the rootfile escapes the archive, via `_checked_opf_path`. A caller reading the contract will not expect the third case. | `:raises ValidationError: If the container is missing or names no rootfile.` vs `return _checked_opf_path(full_path)` at :149 | `:raises ValidationError: If the container is missing, names no rootfile, or names one outside the archive.` |

## Coverage

**Read in full:** all ten modules in `epubconvert/` (`convert.py`, `validate.py`,
`planning.py`, `naming.py`, `cli.py`, `source.py`, `inspect_output.py`, `app_logger.py`,
`defaults.py`, `spec.py`); `pyproject.toml`; the `--help`, selection and exit-code sections
of `README.md`; test file inventory and the classes touching the findings above.

**Mechanical checks run:** AST comparison of every docstring `:param:`/`:raises:` block
against its signature across all ten modules (4 hits, all correct indirect raises or
contextmanager typing — no findings); AST reachability scan for dead and tests-only symbols;
full log-call inventory by level (45 sites); ruff, mypy `--strict`, pylint and pytest.

**Not examined:** test bodies were read only where a finding touched them, so test-internal
readability is uncovered. `.github/` workflows were not read. Passes 1–7 were not run —
correctness, security and performance are outside this review's scope and were last covered
by the separate review earlier today (six defects, all fixed in this HEAD).

## Cross-cutting concerns

**Stale text clusters at the seams a recent change moved.** Findings 10.1, 10.5 and 10.12
are all drift introduced by this branch's own restructure, and 10.3 is older drift of the
same kind. The codebase's habit of writing down *why* is a real asset, but long rationale
paragraphs raise the cost of moving code: the explanation stays behind. Worth a rule that
any function whose call graph changes gets its docstring re-read in the same commit.

**Type-level looseness sits exactly where the domain has closed sets.** 10.4, 10.8 and
`PlanOptions.on_collision` are the same shape — a `str` or `object` where three to five legal
values are known and already named as constants. The project runs mypy `--strict`, so this is
the one place strictness is being paid for and not collected.

**Naming for one concept has drifted across module boundaries** (10.6, 10.9, 10.10, 10.11).
Individually trivial; together they mean a reader must hold two words for one thing in four
places.

## Recommendation

No further cycle needed for readability. The 9 medium findings are each a contained edit;
10.1 is the one with user-visible consequence and should go first.

Passes 1–7 were not run here. Pass 3 (Security) is the gap worth closing if this branch is
headed for release: the earlier review found and fixed a path-traversal in `--covers`, and a
focused security pass over input validation at the OPF and container boundaries would confirm
no sibling of that defect remains. It is not implied by anything in passes 8–10.
