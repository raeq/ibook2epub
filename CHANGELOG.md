# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0] - 2026-08-28

### Added

- Highlights and notes come out of Apple Books. Five flags cover three
  decisions: `-an` gather none, `-ae` put them inside each book at
  `META-INF/annotations.json`, `-ad FILE` write one file for the library,
  `-ao FILE` write that file and convert nothing, `-ar` go back over books an
  earlier run converted. `-ad` and `-ao` write to standard output with no
  FILE. The shape is documented by `epubconvert/annotations.schema.json`,
  shipped inside the package.

  Modelled on the direction of the W3C EPUB Annotations work rather than its
  current draft, which still has T.B.D. sections. The locator is a URL text
  fragment, which is what that work is converging on; the EPUB CFI Apple
  records is kept in its own field because the group has ruled CFI out but it
  is the only locator that survives text which is ambiguous or must not be
  reproduced.
- Each annotation records its book's own `dc:identifier`, resolved through the
  package document's `unique-identifier`. Every other field naming the book is
  a name that can change, so this is the only key that still matches an
  annotation to its book after a rename or a re-download. Values that identify
  nothing are left out: `none` is the identifier for 92 books in a real
  2,805-book library.

  It is recorded canonically, since a key is only a key if the same book always
  yields the same string. That library writes one ISBN six ways -- bare,
  `urn:isbn:`, `URN:ISBN:`, hyphenated, `ISBN 978...` and `urn:ean:` -- and one
  UUID two ways. A verifiable ISBN becomes `urn:isbn:` and thirteen digits, an
  ISBN-10 becomes the ISBN-13 naming the same book, and a UUID becomes
  lowercase `urn:uuid:`; 2,328 of 2,703 identifiers there are rewritten.
  Recognition is by check digit rather than by length, because 68 identifiers
  in that library are ten or thirteen digits and fail it. Anything
  unrecognised passes through untouched, and the original is kept as
  `declaredIdentifier` whenever canonicalising changed it.

  Shelf naming is deliberately not affected: `planning.py` still compares
  identifiers as declared, because changing that would move filenames on
  shelves that already exist.
- A Homebrew tap: `brew install raeq/tap/ibook2epub`. The formula lives in
  [raeq/homebrew-tap](https://github.com/raeq/homebrew-tap) rather than
  homebrew-core, which weighs how widely used a project is. It brings its own
  Python and installs the same distribution PyPI serves.

### Fixed

- The detached export is written to a temporary file and moved into place.
  `write_text` truncates before it writes, so a failure part-way through — a
  full disk, a Ctrl-C — left the export as a prefix of itself: not valid JSON,
  and therefore refused by every later run, which could no longer write to
  that path at all. Reproduced with 400 annotations and 408,646 valid bytes
  replaced by 40,000 unparsable ones.
- `--dry-run` is enforced inside the function that writes, not at one of the
  two routes into it. `--dry-run -ae -ar` rewrote every archive on the shelf.
- A refresh no longer deletes an annotation set it did not write. A book that
  arrived carrying `META-INF/annotations.json` lost it silently when this run
  had no annotations for that book, and the shelf is the only record there is.
- An export file that is valid JSON of the wrong shape is refused rather than
  silently replaced. Only malformed JSON was refused; a file holding a JSON
  list fell through to "nothing to merge into" and was overwritten.
- One unusable database row costs one annotation rather than the whole export.
  Apple's columns are untyped and its schema is undocumented, and `collect`
  built the list in a single comprehension, so a creation date holding a
  string, a style holding a colour name or selected text holding a BLOB took
  every good row with it.
- Long text with no word boundary is quoted whole. Both ends of a
  `textStart,textEnd` pair take their first word unconditionally, so Japanese,
  Thai or a long URL produced a range whose two ends were the same string,
  which selects nothing — a 95-character highlight became a 1,719-character
  locator that matched nothing at all.
- A damaged archive on the shelf is skipped rather than aborting the refresh.
  `zipfile.BadZipFile` is not an `OSError`, so one damaged book stopped every
  book after it from being reached.
- An embedded annotation set carries no generation stamp, so an archive
  holding one is still byte-reproducible. Exporting the same library twice
  produced different bytes, which stops a backup deduplicating and makes two
  outputs incomparable by hash.
- A run that converts every book but cannot read the annotations exits 0. It
  exited 4, which means "the source directory does not exist" — a path a
  scheduled run would then be told to go and fix, having just used it.
- Annotations are applied under the output directory lock, and their temporary
  files can no longer be swept away by a concurrent run.
- `-ao` no longer requires the library to be present. Its own docstring says it
  reads Apple's container and nothing else, but the source-directory check ran
  first, so a reader whose books are on another disk could not get their
  highlights out at all.
- `-ar` against an output directory that does not exist reports the mistake
  instead of "refreshed 0 books" and success.
- A book's shelf name comes from the export that wrote it. Naming the library
  again disagreed with the export under `--match` with a collision suffix, so
  the refresh looked for an archive that was never written.
- `-ad ''` and `-ao ''` are refused. An empty filename is falsy, so every later
  check read as "not asked for" and the run quietly converted instead.
- `-ao - | head` exits cleanly instead of printing a `BrokenPipeError`
  traceback, which is how that flag's own help text says to use it.
- Two package directories with the same name no longer share each other's
  highlights. An annotation records its book's name rather than its path, so
  the run now detects the ambiguity where it is visible and skips those books
  rather than guessing.
- `_newest` survives a dangling symlink among the database candidates. Books
  leaves old files in that directory across upgrades, which is why the glob
  exists.

### Security

- The read-only database URI is built by `Path.as_uri()` rather than by string
  concatenation. A filename containing `?` appended its own parameters ahead of
  `mode=ro`, so a read path could open Apple's database writable; one
  containing `%` failed to open at all. Demonstrated by planting a file that
  matched the glob and watching a read create a file inside the container.
- A manifest href that climbs out of its package is not published as an
  annotation's `href`. The field is described as a path within the book and a
  consumer will join it onto a book root; the same containment rule already
  guarded `container.xml` and not the manifest.
- A package path recorded by the library database is checked before it is
  published. A `ZPATH` ending in `..` produced `"source": ".."`.
- Nothing below the top level of an existing export is trusted. A hand-edited
  or hostile file raised `AttributeError`, `KeyError`, `TypeError` or
  `RecursionError` out of the merge, and the file being read is one the reader
  named.

### Changed

- `schema_problems` derives its checks from the shipped schema instead of
  restating part of it. The nested `book` object was never validated, so an
  annotation carrying a book with no title at all passed the tool's own
  validator.
- `generated` is optional in the schema. A detached export carries it; a set
  embedded in an archive does not, because that stamp would move on every run.

### Performance

- A book is written once under `-ae`. Its annotations go in as the archive is
  built rather than by rebuilding the finished archive, which serialised every
  annotated book twice: measured over 200 books, 1.09s against 0.62s.
- Annotations are indexed once per run instead of scanned once per book.
  Measured over 3,620 books: 1.60s to 0.003s at 20,000 annotations, 623x.
- A refresh that changes nothing no longer decompresses the whole archive to
  find that out. It read every member into memory before the comparison that
  only looks at one small one: peak allocation on a no-op drops from 7.86 MB to
  0.09 MB on a 6.4 MB book.
- The annotation run reads Apple's databases once. Naming the library a second
  time also re-parsed every package document under a metadata naming policy,
  the 2.00x read this project had already fixed once elsewhere.

## [2.0.4] - 2026-08-27

### Fixed

- Encryption is judged per block rather than per document. A book declaring one
  font-obfuscation block and one block naming no algorithm passed as
  unprotected, because the check counted blocks and collected algorithms as two
  flat totals and only fired when *no* block anywhere named one. Such a book
  exported as an archive that will not open and was recorded as finished work,
  which rerun safety then skips for ever — the exact failure the per-document
  check had been written to prevent.
- A copy interrupted part-way through is counted. `--no-copy-through`'s
  counterpart returned a total assigned only once the whole loop finished, so a
  Ctrl-C reported nothing copied while the files were already on disk.

### Security

- Cover extraction reads through `open_contained` like every other reader.
  `shutil.copyfile` opens its source with a plain `open()`, which follows a
  symlink, so resolving the path and then copying it reopened the
  check-then-open window `O_NOFOLLOW` exists to close. A test now fails the
  build if any module copies a file that way.
- The entity-declaration guard asks the parser instead of reading the bytes.
  Two hand-written scans of the `DOCTYPE` declaration were defeated in turn —
  first by a `SYSTEM` identifier containing `>`, then by a comment holding a
  decoy `<!DOCTYPE` — because each had to re-derive where the declaration
  begins and ends. expat already knows, so it is asked.

## [2.0.3] - 2026-08-27

### Fixed

- A package document is read once per run rather than twice. Planning and
  orphan detection each named every package, and naming reads a package
  document per book under `--name-by author-title`, so a real 2,805-book
  library was parsed 5,610 times for one listing. The two share an assignment
  when nothing narrowed the selection; under `--match` or `-m` they still name
  their own sets, because the shelf is judged against the whole library and the
  run against the subset.

### Security

- XML entity declarations in a package document or container are refused. An
  entity lets a small file expand into a large one, and the size cap cannot see
  it — the cap measures the file, the expansion happens after. expat has capped
  the amplification factor since 2.4, so a current Python already refuses the
  classic attack; the rule is stated here so it belongs to the tool rather than
  to whichever expat the interpreter was built against. Of 2,804 package
  documents in a real library, one carries a `DOCTYPE` and none declares an
  entity, so nothing real is refused.

## [2.0.2] - 2026-08-26

### Fixed

- A `dc:title` of `none` was treated as a title. Some converters write that
  literal string into every metadata field a book has, including the one Apple
  derives its folder name from, and 31 books in a real library do. All 31 named
  themselves `none.epub`, collided with each other, and were suffixed into a
  pile — `none.epub`, `none (2).epub`, up to `none (30).epub`. A placeholder
  title is now no title, so those books keep the folder name Apple set, which
  is unique per book. Collisions across that library fall from 72 to 43.
  Matched whole and case-folded: `None of This Is True` is a real book. A
  *creator* of `Unknown` is deliberately left alone — 300 books declare it, and
  filing them together under U is a real answer where a title of `none` is not.

## [2.0.1] - 2026-08-26

Fixes only. Three of them are places where a rule was applied on one path and
not on another, which is why they were invisible until someone ran a real
library through them.

### Fixed

- Copied files never met the naming layer. `--portable-names` sanitises the
  names of books it converts, and wrote a copied file's name verbatim — so a
  colon reached a shelf bound for a Kindle, which is the one thing the flag
  exists to prevent. `--name-by author-title` had the matching gap, leaving
  half a mixed library named the old way. An already-zipped epub is now named
  from its own `dc:title` and `dc:creator` like any other book; a PDF keeps its
  filename, cleaned.
- `--list` printed the source package name rather than the name it would write,
  so previewing a renaming policy showed nothing about the renaming. The name
  was already computed correctly and the listing read the wrong field. This
  affected `-p` as far back as 1.2.0, where the difference was one character.
- Clamping a name that carried an undecodable byte crashed the run.
  `os.walk` on a network share returns names with surrogate escapes; a
  surrogate is three UTF-8 bytes, so a byte limit landing inside one raised
  `UnicodeDecodeError` out of planning, outside any handler. Truncation now
  backs off to a character boundary. The measurement had been made safe for
  these names some time ago; the truncation had not.

### Changed

- The two package readers, one for an archive and one for an unpacked
  directory, now share everything except fetching the bytes. A book that is
  broken the same way is described the same way whichever shape it arrives in:
  the directory reader used to name `container.xml` where the archive reader
  named `META-INF/container.xml`.

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
- A trimmed title backs off to the last whole word, so a shortened name reads
  as deliberate rather than damaged: `...watch television` rather than
  `...watch television a`.
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
- Installation instructions cover the published distribution rather than only
  an editable checkout, and say what the `portable` extra actually buys: it is
  needed for `--portable-names romanize` and for nothing else.
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

[Unreleased]: https://github.com/raeq/ibook2epub/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/raeq/ibook2epub/compare/v2.0.4...v2.1.0
[2.0.4]: https://github.com/raeq/ibook2epub/compare/v2.0.3...v2.0.4
[2.0.3]: https://github.com/raeq/ibook2epub/compare/v2.0.2...v2.0.3
[2.0.2]: https://github.com/raeq/ibook2epub/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/raeq/ibook2epub/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/raeq/ibook2epub/compare/v1.2.1...v2.0.0
[1.2.1]: https://github.com/raeq/ibook2epub/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/raeq/ibook2epub/releases/tag/v1.2.0
