# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-08-26

The exit codes changed incompatibly. Nothing else did, and the rest of this
release is additions.

### Breaking

- **Every distinct failure now has its own exit code.** Five unrelated
  conditions used to exit `2`: a mistyped flag, a source directory that is not
  there, a missing optional extra, a missing external tool, and `--verify`
  pointed at nothing. A scheduled run could not tell a misconfiguration it
  should alert on from a transient state it should retry. `2` still means a bad
  command line, because every tool means that by it; the rest moved to `3`
  through `7`, with `130` for Ctrl-C. `epubconvert/exits.py` is the single
  source for the table in the README, so the two cannot drift apart.

### Added

- `--name-by author-title` names books from their own `dc:title` and
  `dc:creator` instead of the package directory. Apple names a package after
  the title, so an exported shelf sorted by title and no amount of flags would
  make it sort by author. The publisher's sort name is preferred, read from
  either the EPUB2 `opf:file-as` attribute or the EPUB3 `<meta refines="#id"
  property="file-as">` element — Apple's library is overwhelmingly the latter,
  and reading only the attribute would have left 301 of 2,793 books filed under
  their author's first name. Where a publisher gives the author and the
  illustrator the same `id`, the first sort name wins, which is the author's.
  `dc:creator` is used verbatim when no sort name exists, never rearranged.
  Composes with `--portable-names`, which decides how a name is cleaned rather
  than where it comes from.
- Up to two contributors are named in full; beyond that the list collapses to
  `Peralta, Samuel et al.`, which is what other library tools produce.
  Publishers put a book's entire contributor list in one metadata field joined
  with ` & `, and a twelve-author name is not a usable filename even when it
  fits inside the byte limit.
- Neither half of a name can squeeze the other out. Clamping used to trim the
  end of the composed `Author - Title`, which is the title: an anthology came
  out as `... & Wecks, Erik - The Time Travel.epub`, keeping fourteen
  contributors and losing "Chronicles". A book whose `dc:title` is its whole
  jacket blurb keeps its author prefix and has the title trimmed instead,
  because a shelf that stops sorting by author defeats the point of the policy.
- An author made only of characters that sanitising removes (`..`, `?`) is
  treated as absent rather than leaving a separator with nothing in front of
  it.
- Stable collision names. Under `--on-collision suffix`, a book that has to
  share a name is marked with a digest of its own `dc:identifier` rather than
  its position in the colliding group. Adding a book that sorts earlier no
  longer renames every later member. Books whose identifier is missing, a
  placeholder, or shared with another book keep the positional suffix and the
  run says so.
- Orphan reporting. An archive that no book in the library claims is now named
  by `--list`, carries `"source": null` in the JSON, and is counted in the run
  summary. Nothing is deleted; the gap was that nothing would say either.
- Copy-through for books that need no conversion. Already-valid `.epub` files
  and `.pdf` files are copied verbatim through the same
  temporary-then-replace path everything else uses, so a library holding both
  Apple's package folders and books that arrived already zipped exports whole.
  `--no-copy-through` turns it off.
- The run reports how many books were named from a package document that
  declared no creator, and how many kept their folder name because the document
  declared no title. A shelf that comes out half-named says so, rather than
  leaving it to be noticed afterwards.

### Fixed

- `dc:identifier` was read as the first such element in document order, where
  the spec names the canonical one through the `unique-identifier` IDREF on
  `<package>`. Publishers commonly list a retail ASIN or ISBN first, so the
  wrong value was returned for 798 of 2,805 books in a real library. Nothing
  consumed the field yet, so nothing had broken.
- Structural checks in the source walk ran only where `st_flags` exists, so a
  package that could not be checked for undownloaded files skipped the rest of
  its checks on Linux.
- The two write paths in `_decide` had drifted into being byte-identical and
  are now one.
- Roughly forty correctness, security, performance and readability findings
  from two full review passes, applied by rule at every call site of the rule
  rather than at the one site that surfaced them.

### Changed

- `assign_names` returns an `Assignment` record rather than a bare triple, so a
  book that loses a collision can say which file holds the name and what its
  own identifier is.
- The PyPI publish workflow was hardened and pinned.
- CodeTour walkthroughs were added for the conversion and planning paths.

## [1.2.1] - 2026-08-24

### Fixed

- Issues raised by review of the Tier 3 work.
- Findings from an automated review pass.

## [1.2.0] - 2026-08-23

### Added

- `-p` / `--portable-names` with `strip` and `romanize` modes, and
  identity-based deduplication so two spellings of one book export once.
- Validation, source inspection and `--list`.
- Reproducible archives, and a clean exit on interrupt.

### Changed

- `click` replaced with `argparse`, removing the last runtime dependency.
- Python 3.10 is the minimum; development moved to 3.14.
- Relicensed to MIT.

### Fixed

- Filename-length and output-overlap bugs.
- Nested content that looked like Apple bookkeeping was being dropped.

[2.0.0]: https://github.com/raeq/ibook2epub/compare/v1.2.1...v2.0.0
[1.2.1]: https://github.com/raeq/ibook2epub/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/raeq/ibook2epub/releases/tag/v1.2.0
