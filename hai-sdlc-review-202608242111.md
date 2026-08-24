# HAI-SDLC review — ibook2epub — all ten passes

## Anchor

| Field | Value |
|---|---|
| Repo | ibook2epub 1.2.1 |
| HEAD | `a26e3d8` (`main`), tree clean apart from untracked `uv.lock` |
| Profile | **deep** — full tree, no finding caps, strong model on all four groups |
| Scope | passes 1-7 full tree (never previously run); passes 8-10 delta against `e424339` |
| Date | 2026-08-24 |
| Prior review | `hai-sdlc-review-202608241409.md` — passes 8-10 at `e424339` |
| Output | repo root |

**Analysis only. No source file was modified by this run.** All fixtures and harnesses live under the session scratchpad.

### Delegation

Four groups dispatched concurrently. The Polish group terminated early with a harness API
error and was resumed with a tightened, ordered scope; it then completed. The other three
completed first time. No pass was dropped.

## Pass 0 — architecture (delta)

`convert.py` was 970 lines holding five responsibilities. Since the prior review it is three
modules: `convert.py` (566, export orchestration), `run.py` (223, CLI driver, **new**),
`archive.py` (195, discovery and archive writing, **new**). The console script and
`__main__` now resolve `epubconvert.run:main`. `archive.py` and `run.py` had never been
reviewed by any pass before this run.

Load-bearing invariants: the output directory is the sole record of completed work (no state
file, identity recomputed from filenames globbed one level deep); `mimetype` first and
stored; deterministic archive bytes; nothing reaches the target path unless complete;
Apple exclusions apply only at the package root; manifest hrefs are untrusted wherever they
select a filesystem path; planning happens once per run inside the output lock.

## Roll-up (post-dedup)

| Pass | Findings | Critical | High | Medium | Low | Info |
|---|---|---|---|---|---|---|
| 1 — Correctness | 13 | 0 | 2 | 5 | 3 | 3 |
| 2 — Completeness | 10 | 0 | 2 | 4 | 3 | 1 |
| 3 — Security | 16 | 0 | 3 | 3 | 5 | 5 |
| 4 — Robustness | 12 | 0 | 0 | 6 | 4 | 2 |
| 5 — Time | 6 | 0 | 0 | 1 | 3 | 2 |
| 6 — Space | 3 | 0 | 0 | 0 | 1 | 2 |
| 7 — Performance | 10 | 0 | 0 | 3 | 5 | 2 |
| 8 — Observability | 4 | 0 | 0 | 0 | 4 | 0 |
| 9 — Maintainability | 7 | 0 | 0 | 2 | 4 | 1 |
| 10 — Readability | 7 | 0 | 0 | 2 | 5 | 0 |
| **Total** | **88** | **0** | **7** | **26** | **37** | **18** |

Raw total across the four groups was 93. Four rows were superseded as duplicates and one is
marked `known` against issue #2 (see Dedup).

**No critical findings.** Nothing is remotely reachable, nothing corrupts an
already-exported book, and the tool functions correctly on a well-formed library.

## The seven high findings — all reproduced by the coordinator

Each was re-run independently rather than relayed. Commands and observed output below.

| # | File | Defect | Verified outcome |
|---|---|---|---|
| **1.1** | `planning.py:102`, `naming.py:205` | Two packages whose names differ only in case get distinct identities, but on a case-insensitive volume (macOS default) they are the same file. | Printed `Exported 2 epub file(s)`; **one file on disk**. Reruns print `Exported 1, skipped 1` **forever** — never converges. |
| **1.2** | `naming.py:262-267` | `-p romanize` sanitizes the whole filename, so a title that empties eats the dot: `?.epub` -> `epub`. **Live sibling of the `strip_unsafe` defect fixed in `e424339`** — that fix covered one of the two naming policies. | `disarm.sanitize_filename('?.epub')` -> `'epub'`. Three consecutive runs each re-exported; output directory holds a file named `epub`. |
| **2.1** | `archive.py:171` | `Path.rglob` swallows `PermissionError`, so an unreadable subdirectory contributes nothing and the truncated archive is reported as a success. | Archive held only `mimetype` + `container.xml`; chapter silently absent; printed `Exported 1`. After `chmod 755`, rerun printed **`skipped 1`** — the broken book is permanently recorded as done. |
| **2.2** | `archive.py:162-188` | An empty or vanished package yields a `mimetype`-only archive, reported exported and recorded as complete. | `Exported 1 epub file(s) (0 files)`; members `['mimetype']`. |
| **S1** | `archive.py:92-97` | A **symlink** named `*.epub` is accepted as a package; `rglob` traverses through it and zips the whole target directory into the output shelf. | Archive members `['mimetype', 'id_rsa', 'known_hosts']`; `id_rsa` -> `b'PRIVATE-KEY-MATERIAL\n'`. |
| **S2** | `archive.py:171-180` | Symlinked **files** inside a package are dereferenced into the archive under innocuous names. `--validate` passes. | Members included `OEBPS/stolen.txt` and `OEBPS/stolen2.txt`, both `b'TOP-SECRET-SSH-KEY\n'` — absolute and `../../../` targets both worked. |
| **S3** | `convert.py:467` | `sweep_partials` globs `*.part` and unlinks everything it matches, not only its own `tmp*.part`. Default output is `~/Books`. | Two unrelated user files (`Some Download.epub.part`, `notes.part`) **deleted** in one run. |

### What ties five of these together

1.2, S1 and S2 are the same defect class as the `--covers` path traversal fixed in
`e424339`: an attacker-controlled or externally-supplied name is turned into a filesystem
path, or a name is normalised at the wrong scope, without the guard that already exists
elsewhere in the tree. `validate.contained_file` holds that rule and its own docstring
invites reuse; the archive writer predates it and does not call it.

S3 and 2.1/2.2 are regressions in, or gaps left by, the previous round of fixes. S3 is a
defect introduced *by* the fix for the earlier stale-`.part` finding.

Two cheap guards close four of the seven:

- Refuse to move an archive into place unless it holds `META-INF/container.xml` and at
  least one member — closes 2.1 and 2.2.
- Skip symlinks in both the package walk and the member walk — closes S1 and S2.

## Pass 1 — Correctness

| # | File:Line | Sev | Category | Finding | Fix |
|---|---|---|---|---|---|
| 1.1 | `planning.py:102` | high | race-condition | See above. Case-only names collide on the filesystem but not in the identity map. | Claim a second filesystem-level key, `unicodedata.normalize("NFC", candidate).casefold()`, alongside `policy.identity`, in both `_assign_names` and the `existing` map. |
| 1.2 | `naming.py:262` | high | contract-violation | See above. `romanize` loses the extension. | Mirror `strip_unsafe`: split with `_split_extension` first, sanitize the stem alone, fall back to `"_"`, reassemble. |
| 1.3 | `planning.py:219` | medium | logic | With `--force`, `_decide` targets the freshly computed name rather than `found`. Under a looser-than-filename identity the book is written twice and the stale copy keeps satisfying the identity check. The `--refresh` branch already documents this hazard and writes to `found`. | `if found is not None and settings.force: return Decision(package, PENDING, found, reason="forced")`. |
| 1.4 | `archive.py:177`, `validate.py:444` | medium | type-mismatch | Member names are copied verbatim from disk (NFD on anything that lived on HFS+) while OPF hrefs are NFC. The archive then carries a member no reader can resolve. | Normalise both sides to NFC: the arcname on write, the href in `_check_manifest`. |
| 1.5 | `convert.py:137` | medium | contract-violation | `extract_cover` is called outside the `try` and catches only three exception types, so anything else escapes the worker after the archive is in place — the book is on disk, uncounted, and the run dies. Contradicts the module docstring. | Move the call inside the guard; catch broadly with a comment that a cover never justifies aborting a run. |
| 1.6 | `naming.py:77,158,164` | medium | type-mismatch | Undecodable filenames carry surrogate escapes; every byte-budget `str.encode("utf-8")` raises `UnicodeEncodeError` during planning, outside any handler, aborting the run. | Encode with `errors="surrogatepass"`, or normalise once at the top of `strip_unsafe`. |
| 1.7 | `planning.py:172` | medium | logic | `existing` is built with no `is_file()` filter, so a *directory* named `Book.epub` in the output registers as completed work and the book is never exported. | Add `if f.is_file()` to the comprehension. |
| 1.8 | `planning.py:172` | low | logic | Two output files sharing an identity collapse into one dict entry; which survives depends on glob order, so `--refresh` rewrites an arbitrary one and leaves the other stale. | Build with `setdefault` over sorted names and warn, naming both files. |
| 1.9 | `planning.py:90` | low | off-by-one | `_suffixed` uses `Path().stem`/`.suffix` rather than the module's own `_split_extension`, so `".epub"` becomes `".epub (2)"`. | Use `_split_extension`. |
| 1.10 | `validate.py:229` | low | contract-violation | `contained_file` documents "None if there is nothing safe to read" but a NUL byte in the href raises `ValueError` from `resolve()`. Its docstring invites future callers who will not catch it. | Wrap the resolve in `except (OSError, ValueError): return None`. |
| 1.11 | `convert.py:83` | info | race-condition | Confirms the `_REPORT_LOCK` invariant: every worker-thread mutation of `Report`/`_Progress` holds the lock; the rest are single-threaded before the pool starts or after `shutdown(wait=True)`. | — |
| 1.12 | `convert.py:291` | info | race-condition | Confirms the Ctrl-C claim under a **real SIGINT** (existing tests raise `KeyboardInterrupt` inside a worker, a different path): exit 130, exported count equals files on disk, no `.part` left. | — |
| 1.13 | `validate.py:187` | info | null-handling | Confirms invariant 6. 17 traversal payloads x 3 OPF bases, plus file and directory symlinks: no escape from `_resolve` + `contained_file`. | — |

## Pass 2 — Completeness

| # | File:Line | Sev | Category | Finding | Fix |
|---|---|---|---|---|---|
| 2.1 | `archive.py:171` | high | error-handling | See above. `rglob` swallows `PermissionError`; truncated archive recorded as complete. | Walk with `os.walk(onerror=raise_it)` so the book fails and is retried. |
| 2.2 | `archive.py:162` | high | edge-case | See above. `mimetype`-only archive recorded as complete. | Before the replace, require `META-INF/container.xml` and `file_count > 0`; the existing `except BaseException` removes the partial. |
| 2.3 | `validate.py:394` | medium | error-handling | `validate_archive` catches only `BadZipFile`/`OSError`. `zipfile` also raises `NotImplementedError` (unsupported compression) and `RuntimeError` (encrypted member). `--verify` is unguarded, so the one command whose job is finding damage dies and checks nothing further. | Widen the handler and guard the per-archive call in `verify_output`. |
| 2.4 | `archive.py:41,62` | medium | edge-case | Root-only Apple exclusions drop real content in a flat-layout epub, where chapters sit at the package root. A root `bookmarks*.xhtml` or `*.plist` listed in the manifest is excluded; with `--validate` the book fails every run. | Fire the prefix rule only together with a bookkeeping suffix, or consult the manifest before excluding. |
| 2.5 | `validate.py:147,348` | medium | missing-branch | Both rootfile loops take the first `<rootfile>` with a `full-path` and ignore `media-type`. OCF says the package document is the first with `media-type="application/oebps-package+xml"`. | Prefer the correct media-type, falling back to the first only when none declares it. |
| 2.6 | `run.py:186` | medium | error-handling | `app_logger.configure` does unguarded filesystem work and is the first thing `main` does, so a bad `--log-file` exits with a raw traceback while every comparable error is reported cleanly. | Guard it; degrade to console-only and warn. |
| 2.7 | `run.py:199`, `cli.py:120` | low | missing-validation | `--list --verify` silently runs only the listing; `--json` without `--list` is silently ignored and a full export runs. | Mutually exclusive group, plus `parser.error` for `--json` without `--list`. |
| 2.8 | `planning.py:319` | low | missing-exhaustive | `record_decisions` treats any status missing from `_OUTCOMES` as PENDING, so a sixth `Status` would vanish from the report with no type error. | `if decision.status == PENDING: continue` then index `_OUTCOMES[...]` so a new status raises. |
| 2.9 | `source.py:169` | low | unimplemented | `--skip-incomplete` is a silent no-op wherever `st_flags` is absent (Linux, any non-macOS source), while the help promises the check and the user pays the full walk. | Detect once at startup, warn, and skip the walk. |
| 2.10 | `validate.py:54` | low | unimplemented | `title`, `creator`, `creator_sort`, `identifier` parsed per book, read by nothing. **`known:` filed as issue #2.** | Excluded from totals. |
| 2.11 | `epubconvert/*.py:0` | info | todo/fixme | No TODO/FIXME/HACK/XXX markers anywhere; no stub bodies. | — |

## Pass 3 — Security

Threat model: local single-user CLI. No listener, no auth, no database, no secrets in the
tree. The trust boundary is **the book** — `container.xml` and the OPF are attacker-supplied
XML that becomes filesystem paths.

| # | File:Line | Sev | Category | Finding | Fix |
|---|---|---|---|---|---|
| S1 | `archive.py:92` | high | path-traversal | See above. Symlinked `*.epub` accepted as a package; whole target directory zipped. | Skip entries where `entry.is_symlink()` in the `dirs` loop; warn. |
| S2 | `archive.py:171` | high | path-traversal | See above. Symlinked members dereferenced into the archive; `--validate` passes. | Skip `path.is_symlink()` in the rglob loop, or route through `validate.contained_file`. |
| S3 | `convert.py:467` | high | data-loss | See above. `*.part` glob deletes unrelated user files from the output directory. | Give the temporary a distinctive prefix (`.ibook2epub-`) and narrow the sweep to match it. Put the prefix constant next to `PARTIAL_SUFFIX` so writer and sweeper cannot drift. |
| S4 | `source.py:104` | medium | drm-false-negative | An `EncryptedData` block with no `EncryptionMethod` yields an empty algorithm set, so `has_drm` returns False and a protected book exports as an unopenable archive recorded as finished. Every other unreadable state in this module fails closed; this one fails open. | Judge per `EncryptedData` element: an empty or non-font algorithm set means protected. |
| S5 | `validate.py:341,360` | medium | resource-exhaustion | `read_package_dir` applies **no** size cap while the archive-side reader caps at `MAX_XML_BYTES`. The uncapped side is the untrusted one. Measured: 105 MB `container.xml` parsed, 338 MB RSS; a 5.2 MB doc with 95x entity amplification reached **542 MB** in 0.3 s, under expat's 100x guard. | Apply the same `MAX_XML_BYTES` stat check before each read; consider rejecting any internal DTD subset, which neither document needs. |
| S6 | `validate.py:423` | medium | resource-exhaustion | `_check_mimetype` reads the whole member to compare 20 bytes. `--verify` runs over files this tool may not have written. Measured: a 510 KB archive declaring a 512 MiB `mimetype` reached **1101 MB** RSS. | Reject on `info.file_size != len(MIMETYPE_CONTENT)` before reading. |
| S7 | `planning.py:363`, `convert.py:134` | low | output-injection | Package names reach stdout and the log verbatim, control characters included; a sideloaded book can erase and rewrite its own status line. | Add a `printable()` helper escaping C0/C1 and route all user-facing name rendering through it. |
| S8 | `archive.py:161` | low | permissions | `chmod(0o644)` overrides the user's umask; `umask 077` still yields world-readable books. | Honour the umask: `0o666 & ~mask`. Keep `ARCHIVE_MODE` for the zip entry, which is metadata and rightly fixed. |
| S9 | `publish.yml:69` | medium | supply-chain | The `publish` job holds `id-token: write`, and the SHA pin covers only `gh-action-pypi-publish`. `actions/download-artifact@v6` runs in the same job from a mutable tag. | Pin it, and the `build` job's actions, to SHAs. |
| S10 | `ci.yml:0` | low | least-privilege | `ci.yml` declares no `permissions:` block and runs untrusted PR code via `pip install -e ".[dev]"`. `publish.yml` already argues the default should be stated. | Add `permissions: contents: read`. |
| S11 | `ci.yml:6,27,46` | low | resource | No `timeout-minutes` on any job; a hung run holds a runner for the 360-minute default. | Add `timeout-minutes: 15`. |
| S12 | `validate.py:206` | info | confirmation | Confirms invariant 6 independently of 1.13: 23 href encodings x 4 OPF bases plus symlinks — zero escapes. `ok.jpg#/../../secret.txt` is correctly defragged in-package. | — |
| S13 | `validate.py:36` | info | confirmation | Confirms the `MAX_XML_BYTES` docstring: `zipfile` clamps decompression to the declared size, so a lying header cannot smuggle bytes past the check (raises `Bad CRC-32`). | — |
| S14 | `validate.py:105` | info | confirmation | No XXE exposure. `ElementTree` refuses external general entities outright; billion-laughs is stopped by libexpat 2.7.1's amplification guard. Residual 100x headroom is S5. | — |
| S15 | `validate.py:475` | info | confirmation | The single `subprocess` use is well-formed: absolute path via `which`, argument list, no shell, `check=False`, 120 s timeout, both error classes handled. | — |
| S16 | `convert.py:334` | info | confirmation | Confirms the output-lock invariant: a second acquisition is refused with `OutputLockedError` naming pid and host. | — |

**Not applicable** (stated rather than invented): CORS, CSRF, XSS, JWT, sessions, SQL/NoSQL
injection, ORM misuse, password storage, TLS, rate limiting, authorization/IDOR — no server,
no auth, no database. No `pickle`/`yaml.load`/`eval`/`exec` anywhere. No cryptography;
`random.shuffle` selects which books to export and carries no security weight.

## Pass 4 — Robustness

| # | File:Line | Sev | Category | Finding | Fix |
|---|---|---|---|---|---|
| R2 | `convert.py:357` | medium | poor-error-context | `output_lock` opens the lock file unguarded; a read-only output directory survives `mkdir(exist_ok=True)` and dies with a raw `PermissionError`. | Wrap and raise `OutputLockedError`, which `main` already turns into exit 3. |
| R3 | `app_logger.py:86` | medium | unhandled-failure | Duplicate root cause with 2.6, different call site: `mkdir` inside `configure` kills the process before logging exists. | Guard the file handler; degrade to console and warn. |
| R4 | `convert.py:360` | medium | poor-error-context | `except OSError` treats every errno as contention. On a filesystem without advisory locking (SMB, some FUSE — and `--min-free`'s help names SD cards and Kindles) `ENOTSUP` is reported as "another run is already using…", quoting a pid from a long-dead run because the lock file is never truncated on release. | Distinguish `EAGAIN`/`EACCES`/`EWOULDBLOCK` from the rest; continue unlocked with a warning otherwise, and truncate on release. |
| R5 | `inspect_output.py:37` | medium | silent-failure | `free_megabytes` returns 1 PiB on `OSError` and logs nothing, so `--min-free` silently stops enforcing on exactly the removable and network volumes it exists for. | Keep the permissive return; log a warning once. |
| R6 | `validate.py:475` | medium | missing-timeout | The 120 s epubcheck timeout is a default parameter with no CLI exposure, and `TimeoutExpired` is returned as a *problem*, so the archive is deleted and the book counted failed — a book too large to check in 120 s can never complete, on any run. | Add `--epubcheck-timeout`; treat a timeout as "unchecked", not "invalid". |
| R7 | `inspect_output.py:120` | medium | silent-failure | `--verify` on a typo'd `-o` globs nothing and reports a clean bill of health with exit 0. | Fail fast when the directory does not exist; distinguish "nothing checked" from "nothing damaged". |
| R9 | `archive.py:179`, `source.py:161` | medium | missing-retry | No retry or timeout anywhere in the read path, on a library the project's own docstrings describe as cloud-backed. A transient `EIO` costs the whole book. `has_dataless_files` swallows stat failures and returns False, so a package it could not inspect is treated as fully downloaded. | Bounded retry with backoff on the per-file copy; treat a stat failure in `has_dataless_files` as incompleteness, matching `has_drm`'s fail-closed posture. |
| R10 | `inspect_output.py:77` | low | silent-failure | Every reason a cover is skipped logs at debug, off by default, and the summary has no cover field — `--covers` can silently write nothing. | Add `covers_written`/`covers_skipped` to `Report` and surface the tally. |
| R11 | `validate.py:383` | low | resource-exhaustion | `testzip()` fully decompresses every member with no bound, before any structural check rejects the archive. | Bound total declared size and compression ratio first. |
| R12 | `convert.py:277` | low | no-degradation | `asyncio.gather` without `return_exceptions=True`. Safe today only because `_zip_and_record` catches `Exception` — which does not cover `_has_room` or `extract_cover` (1.5). | Pass `return_exceptions=True` and count surprises as failures. Belt to 1.5's braces. |
| R13 | `archive.py:189` | info | confirmation | Confirms invariant 4: `except BaseException` unlinks the temporary and re-raises, so the target is only reached after validation. | — |
| R14 | `archive.py:85` | info | confirmation | `os.walk(onerror=...)` reports unreadable directories rather than skipping silently — and pathlib's `**` does not follow symlinked subdirectories, which is what limits S2 to file symlinks. | — |

## Passes 5-7 — Time, Space, Performance

Every claim below carries a measurement. Machine: 10-core macOS, Python 3.13.5, warm cache,
medians of 3-5 runs, synthetic fixture libraries under the scratchpad.

| # | File:Line | Sev | Finding | Measurement | Fix |
|---|---|---|---|---|---|
| 5.1 | `run.py:149` | medium | `plan_exports` runs the `--skip-incomplete` walk over every unexported book, then `cap_exports` discards all but `-m N` of them. Cost is O(library) to export O(cap). | `-m 5 --skip-incomplete` on 200 books: **801 scandirs + 3,696 stats to export 5 books**. At the shipped default `-m 5`, a 2,805-book library needs 561 runs and ~790,000 package walks — about 9.5 min of pure metadata walking, warm and local. | Split planning: classify without the walk, cap, then `confirm_downloaded()` only the selected decisions. Keep the eager path for `--list`/`--dry-run`. |
| 5.2 | `planning.py:224` | low | `has_drm` likewise paid before the cap: 2 stats per package. | 496 stats for 200 books at `-m 5`; 7 us/book, 0.02 s for 2,805. | Fold into 5.1's post-cap phase. Not worth a separate change. |
| 5.3 | `planning.py:134` | low | Once a colliding group exhausts `MAX_SUFFIX`, every later member re-tries all 99 candidates before losing. | 300 identical names: 35.6 ms passthrough / 50.9 ms romanize; ~385 ms extrapolated to 2,805. | Track `next_position` per identity; amortised O(1). |
| 5.4 | `validate.py:411,439` | low | `namelist()` built twice per archive. | 5,610 list builds where 2,805 would do. | Build once in `validate_archive` and pass down. |
| 5.5 | `planning.py:161` | info | Confirms the no-state-file claim costs nothing at scale. | `_assign_names` over 2,805 names: 3.2 ms passthrough, 11.6 ms strip, 8.9 ms romanize. | — |
| 5.6 | `planning.py:216` | info | Confirms the "already exported needs no source walk" comment. | A no-op rerun over 200 books: **1 scandir, 1 stat, 1 open (the lock)**, 0.00 s. | — |
| 6.3 | `inspect_output.py:98` | low | `write_bytes(read_bytes())` holds the whole cover in memory, once per worker. | 5 MB cover x 64 workers = 320 MB transient. | `shutil.copyfile` — streams, and may clone on APFS. |
| 6.4 | `convert.py:277` | info | Fan-out is not a memory concern. | `gather` over 2,805 coroutines: **2.5 MB**. Full 100-book export: 6.4 MB heap, 47.3 MB RSS. | — |
| 6.5 | `source.py:93` | info | Confirms both stated bounds check size before reading. | — | — |
| 7.1 | `archive.py:27,119` | medium | **`COMPRESS_LEVEL = 9` has no effect.** `ZipFile(compresslevel=)` is consulted only when `open()` builds its own `ZipInfo`; `_entry` supplies a prebuilt one, so every member deflates at zlib's default 6. Making it effective would be the wrong fix. | Coordinator-verified: prebuilt `ZipInfo` gives **228 bytes at ctor levels 1, 6 and 9 alike**, while `writestr` gives 301/228/228. End-to-end: level 9 costs **3.2x the CPU for 0.6% smaller** (147.9 ms vs 45.7 ms per 3 MB book). | Set `COMPRESS_LEVEL = 6`, assign it in `_entry` via `entry._compresslevel`, and record the measurement so nobody raises it again. |
| 7.2 | `convert.py:275` | medium | `max_workers=None` takes Python's CPU-tuned default (14 here). The dominant cost is blocking on iCloud. | I/O-bound model: 14 workers 19.2 s, 48 workers **7.12 s (2.7x)**, 64 workers **4.76 s (4.0x)**. CPU-bound cost of raising it: 1.20 -> 1.27 s (6%). | Default to `min(64, 4 * cpu_count)`. |
| 7.3 | `cli.py:96` | low | `-w` help says "a value above the CPU count often helps", but the default already exceeds it, so following the advice literally can *slow the run down*. | Default is cpu+4 = 14; "above the CPU count" would be 11, measuring ~24 s vs 19.2 s. | State the default and the direction; ship with 7.2. |
| 7.4 | `inspect_output.py:123` | medium | `verify_output` is sequential while the export path is pooled. | 100 archives: **0.439 s sequential -> 12.3 s for 2,805**; 4 threads 0.213 s (2.06x). Past 8 threads it gets slower (GIL-bound). | Pool it at `min(8, cpu_count)`, keeping ordered output. |
| 7.5 | `archive.py:171` | low | `sorted(rglob("*"))` + `is_file()` stats every entry to learn what `scandir`'s dirent already reported. `has_dataless_files` documents this exact trade-off and uses `os.walk`; `zip_package` does the opposite. | **19 stats per 16-file book**; enumeration 391.7 vs 335.3 us/book (1.2x). ~0.1% of run time locally — but each stat is a potential round trip on a network source. | `os.walk`, sorting the same full paths so member order and byte-identity are preserved. |
| 7.6 | `archive.py:119` | low | Every member is deflated, including JPEG/PNG/font data that is already entropy-coded. | Image-heavy book: deflate-everything **71.8 ms/book, 4.790 MB**; store-media **18.9 ms/book, 4.789 MB** — **3.8x faster for +0.02% size**. | Store known-compressed extensions. Deterministic (pure function of the name); note that old archives will hash differently. |
| 7.7 | `inspect_output.py:69` | low | `contained_file` calls `package.resolve()` once per book for a value constant across the call. | `--covers` adds 3 opens and **7 lstats per book** over a plain run. | Resolve the package root once and pass it in. Leave the OPF re-read; it is page-cache warm. |
| 7.8 | `convert.py:121` | low | `shutil.disk_usage` per book, on by default, and `--min-free`'s help names the volumes where `statvfs` is slowest. | 201 `statvfs` calls for 200 books. Negligible locally; method given for measuring on a slow target. | Sample every N books; the floor is a margin, not an accounting. |
| 7.9 | `validate.py:369` | info | **Corrects an assumption.** `--validate` does not double read work: `testzip()` re-inflates from the page cache the archive was just written into, and inflate is far cheaper than deflate. | Text books **60.8 -> 65.4 ms (+7.7%)**; image books 74.5 -> 79.7 ms (+7.0%). Exactly 1 extra output open, 0 extra source reads. | — |
| 7.10 | `README.md:68` | info | Confirms the README's "50 books, 80 s, 8% CPU". | Compression is 45.7-60.6 ms/book, so 50 books is ~3 s of CPU in an 80 s run — under 4% of one core. | — |

## Passes 8-10 — Observability, Maintainability, Readability (delta)

### Prior findings re-checked

The prior review carries **20 actionable rows plus one `info`** (the fix commit's subject says
19; the count was wrong, the work was not). Verdicts: **20 landed, 1 landed but incomplete,
none not landed, none regressed.** The incomplete one is 10.3 — see P10.2.

| # | File:Line | Sev | Finding | Fix |
|---|---|---|---|---|
| P9.1 | `planning.py:264` | medium | The `_OUTCOMES` table added for prior 9.2 replaced type-checked writes with `setattr` on a bare `str`. Renaming a `Report` field now breaks at runtime with mypy, ruff and pylint all silent — the one place the project's strictness is paid for and not collected. | Narrow `counter` to a `Literal`, and assert the table's keys are a subset of `Report`'s fields in a test. |
| P9.2 | `ci.yml:25` | medium | CI gates `pylint --fail-under=8.0`, two points below the 10.00/10 the tree holds and documents. **Any other finding here could regress and merge green.** | `pylint epubconvert tests` — it already exits non-zero on any message. |
| P10.1 | `conftest.py:22` | medium | The module map added for prior 9.5 says `app_logger` and `defaults` are tested only indirectly. `test_cli.py:96` calls `app_logger.level_for_verbosity` directly and `:23` uses `defaults.DEFAULT_*`. | Extend the `test_cli.py` row; reduce the closing sentence to `spec` alone. |
| P10.2 | `inspect_output.py:2` | medium | Prior 10.3's fix rewrote the docstring body and left the one-line summary, which the body now contradicts six lines later. The reported contradiction still exists, relocated. | Retitle to "Reading the output directory, and writing beside it." |
| P8.1 | `validate.py:226` | low | Prior 8.1's fix added the consequence at the caller without removing the reason at the callee: one rejected cover now logs twice at the same level. | Demote the two `contained_file` lines to `trace`. |
| P8.2 | `run.py:170` | low | Ctrl-C sets the flag with no log record and the closing line omits it, so a `--log-file` transcript of an interrupted run is indistinguishable from a complete one. The only statement that it was interrupted goes to `print`. | Log the interrupt, and log the summary as well as printing it. |
| P8.3 | `planning.py:328` | low | The tally loop reads the cumulative `Report` counter rather than the decisions in hand, so a second call on an accumulating report logs the running total as that batch's tally. | Tally from the argument with a `Counter`. |
| P8.4 | `archive.py:161` | low | `os.close` and `chmod` run above the `try` whose `except BaseException` unlinks the temporary, so a failure in either leaks a `.part` with no log line — on the volume `--min-free` protects. | Move both inside the `try`. |
| P9.3 | `cli.py:125` | low | `--list`'s help spells the five statuses as prose while its two sibling closed sets are imported from the module that owns them. `planning.Status`'s docstring names this help as part of the contract. | Export a `STATUSES` tuple and build the help from it. |
| P9.4 | `naming.py:41` | low | Prior 10.4 typed `planning`'s closed sets as Literals; the identical set here stayed `str`, so `build_policy("romanise")` type-checks and silently falls through to `PortableNaming()`. | Add `PortableMode = Literal[...]`. |
| P9.5 | `run.py:49` | low | The preamble states the naming policy twice from two independent sources — the policy object, then a re-derivation from the raw argument. | Key both off `policy`. |
| P9.6 | `test_convert.py:28` | low | Two helpers still drive `export_packages`, which production never calls, so the seam the refactor created has no direct unit coverage. | Point them at `plan_exports` + `export_planned`. |
| P10.3 | `convert.py:186` | low | The note added for prior 10.5 cross-references `:func:`main``, which the same commit moved to `run.py`. | Qualify as `epubconvert.run.main`. |
| P10.4 | `run.py:181` | low | `main`'s `:return:` describes one of five exit codes; the README documents all five. | Enumerate them. |
| P10.5 | `archive.py:26` | low | Three constants in the new module carry no comment while both neighbouring groups do — the gap prior 10.7 closed in `planning.py`, reopened in the module the same commit created. | Add `#:` lines. |
| P10.6 | `archive.py:28` | low | `ARCHIVE_MODE` governs two different things: zip-entry permission bits and the temporary file's on-disk mode. Changing one silently changes the other. | Split, or rename with a comment saying it governs both. |
| P10.7 | `planning.py:324` | low | The same warn/info selection is written two ways six lines apart in one function. | Use the named local in both. |
| P9.7 | `pyproject.toml:0` | info | Structural checks pass: `.git/`, 8 test files/237 tests, ruff+mypy+pylint config, and a CI job asserting `disarm` never leaks into the base install. | — |

## Dedup

**Superseded within this run** (kept in the earliest pass, excluded from totals):

| Dropped | Kept | Same defect |
|---|---|---|
| R1 (Pass 4) | **1.5** (Pass 1) | `extract_cover` escaping the worker guard |
| R8 (Pass 4) | **2.4** (Pass 2) | root-level manifest items excluded by the Apple prefix rule |
| 6.1 (Pass 6) | **S5** (Pass 3) | `read_package_dir` uncapped while the archive reader caps |
| 6.2 (Pass 6) | **S6** (Pass 3) | `_check_mimetype` reading a whole member to compare 20 bytes |

**Known** (excluded from totals): 2.10, the four parsed-but-unread OPF metadata fields, is
already filed as **issue #2** together with the `dc:identifier` collision work.

**Against the prior review:** passes 8-10 were re-run as a delta and dedup was handled by the
re-check table above rather than by dropping rows.

## Coverage

**Read in full by at least one group:** all fourteen files in `epubconvert/`; `pyproject.toml`;
both GitHub workflows; `tests/conftest.py`; the Performance, Safety and exit-code sections of
`README.md`; the prior review; the `f3582b0` commit message.

**Executed:** the 237-test suite (green, before and after — no repository file was created,
edited or deleted); roughly 30 purpose-built fixtures and harnesses under the scratchpad,
including a 17x3 and a 23x4 traversal fuzz, two real-SIGINT runs against a 200-book library,
syscall-counting instrumentation across nine run modes, worker scaling from 1 to 64 threads
under both CPU-bound and modelled-iCloud conditions, and compression-level measurement
end-to-end and through bare zlib.

**Coordinator-verified independently:** all seven high findings, reproduced from scratch;
plus 7.1 (compression level inert) and a full re-check of the 101 `.tours` anchors, which the
Polish group had flagged as uncovered — **0 broken**.

**Not examined:** Windows behaviour (`HAVE_FLOCK = False`, backslash separators); `epubcheck`
integration (binary not on PATH — R6 and S15 are static reasoning); genuine iCloud-backed
volumes (7.5 and 7.8 state the method for measuring there); real FairPlay-protected packages;
Python 3.10/3.11 runtime paths (not installed here). Test files were read selectively rather
than audited for their own defects.

## Cross-cutting

**A guard exists in the tree and is not reused.** 1.2, S1 and S2 are the same class as the
`--covers` traversal fixed in `e424339` — an externally supplied name becoming a filesystem
path, or a normalisation applied at the wrong scope. `validate.contained_file` holds that
rule and its docstring explicitly invites future callers; the archive writer predates it and
does not call it. The fix for that traversal was scoped to the one call site that had the
bug rather than to the class.

**Fixes are creating their own findings.** S3 (destructive `*.part` glob) was introduced by
the fix for the earlier stale-`.part` finding. P8.1, P10.2, P10.3 and P10.5 were each
introduced or left incomplete by the fix commit. That is five of the 88 traceable to the
previous round, which argues for re-running the affected pass after any substantial fix
commit rather than trusting the fix.

**Silent success is the dominant failure shape.** 1.1, 2.1, 2.2, S1, S2 and S4 all end with
a book reported as exported that is wrong or missing, and — because the output directory is
the sole record of completed work — permanently recorded as done. The single highest-value
structural change is a content assertion before `partial.replace()`: an archive that does not
hold `META-INF/container.xml` and at least one member is not a book.

**Measurement corrected two assumptions.** `--validate` was expected to double read work; it
costs 7%. `COMPRESS_LEVEL = 9` was assumed effective; it has never been applied, and applying
it would cost 3.2x the CPU for 0.6% of size.

## Recommendation

Fix the seven highs before any release, in this order:

1. **S3** — destructive, affects `~/Books` by default, one-line root cause.
2. **S1, S2** — data exfiltration into a shareable artifact; one symlink check each.
3. **2.1, 2.2** — one content assertion closes both.
4. **1.1** — silent book loss on the default macOS filesystem.
5. **1.2** — completes the `e424339` fix across the second naming policy.

Then **P9.2**, because CI at `--fail-under=8.0` cannot hold the line on any of the rest.

A further cycle is not needed for passes 5-7 — that ground is measured and the findings are
tuning, not defects. Re-run passes 1-4 after the high fixes land, given the pattern of fixes
introducing findings.
