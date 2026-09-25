# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Every archive the tool writes names the book it was written from in its zip
  archive comment, `ibook2epub/1 src=<digest>`: a digest of the book's path in
  the library, NFC-normalized and case folded. Readers ignore it and the book
  validates as before. `-ae -ar` keeps it, and writes it into an older archive
  it rewrites anyway; nothing is rewritten only to add one, and files copied
  through stay byte for byte.
- Where two books that nothing tells apart -- no usable identifier, or one
  between them -- want an archive written before markers, neither is given
  it: under `--on-collision suffix` each is written under a name of its own,
  in skip mode each is a collision, and the run warns once, naming the books.

### Changed

- The planner takes an archive's marker over its name and over an identifier
  two books share: a book keeps the file its marker names, numbered or not,
  in either mode, and a file whose marker names a book no longer in the
  library is listed as an orphan and never taken or written over. The marker
  is read where the identifiers are read anyway, from the same open, where
  two books want one name, and before a write: a rerun over a shelf of books
  with identifiers of their own reads nothing more.
- A book moved to another folder in the library has another source: its old
  archive is listed as an orphan and the book is written again, or in skip
  mode is a collision.
- `export/archive.py` hands `copy_through`, `write_atomically` and the
  partial-file names to `export/writing.py`, and re-exports them.

- `collect/validate.py` is split into `collect/identifiers.py` (identifier and
  title canonicalisation), `collect/package.py` (reading a package) and
  `collect/validate.py` (archive checks and epubcheck).
- `run/run.py` hands the `--verify` advice to `run/repair.py` and the
  pre-flight checks to `run/preflight.py`; the summary line moves from
  `run/convert.py` to `run/summary.py`.
- `run/planning.py` hands the listing and the report's tallies to
  `run/reporting.py`, and the orphan check to `run/orphans.py`.
- Naming a zip member, and opening an archive the same way on every Python,
  moves from `collect/package.py` to `collect/zipnames.py`.

### Fixed

- A book with no usable identifier, or one it shares, is no longer reported
  exported from another book's archive of its name, nor written over it by
  `--force`, `--refresh` or `-ae -ar`, wherever that archive names its source
  (formal/README.md, `Unidentifiable`). A book known to declare no usable
  identifier is never given a file that declares one. What remains is a file
  with no marker -- from an earlier release, or copied through -- that
  declares no identifier, taken by the name by a book alone in wanting it.
- Three books of one name with no usable identifier keep their numbered
  files as one of them leaves (`NumberedRemovalsCrowd`).

- `-ae -ar` writes `mimetype` first and the other members in the order the
  file stores them, not the order its central directory lists them. A book
  storing `mimetype` first but listing it later passed `--verify`, was
  rewritten with `mimetype` not first, and then failed it; a book listing
  `mimetype` first but storing it later is still repaired by a refresh.

- `--verify` reads nothing of an archive whose directory entries share a
  local header, so Python 3.13 and later no longer print zipfile's own
  `UserWarning: Overlapped entries: 'mimetype' (possible zip bomb)` above
  the report. Naming a book by its metadata refuses such an archive too.

- A container's rootfile media type is compared without regard to case or
  parameters, so `application/oebps-package+xml; charset=utf-8` names the
  package document. Without a match, the first rootfile declaring no media
  type is taken ahead of one declaring another: a PDF listed ahead of an
  untyped package document was parsed as the package document.

- A zip member is named the same way on every Python, as Info-ZIP's unzip
  names it: an unflagged name takes the UTF-8 name of a Unicode Path extra
  field (0x7075) whose CRC matches the name's bytes, and a flagged name is
  its own. Python 3.14 took the field's name even for a flagged name, where
  3.10 and 3.11 ignore the field, so `--verify` and `-ae -ar` read one book
  differently on each; and a book zipped by WinZip, or by Info-ZIP on
  Windows, which stores a non-ASCII name in the OEM code page with its
  UTF-8 name in that field, had its chapter reported missing, and a refresh
  rewrote the chapter under a misreading of its name.

- `-ad` naming the shelf or a directory the run makes above it (`-ad Books
  -o Books` on a first run) is refused before anything is converted, with
  "Is a directory" and exit code `5`. The dry run exited `0`, and the real run
  converted every book and then exited `5`.

- A vault of notes (`--annotations-format markdown`) that is a file, sits
  under a file, or is on a volume the run cannot write is refused before
  anything is converted, in the dry run and the real run alike, with exit
  code `5`. The dry run exited `0`, and the real run converted every book and
  then refused the vault. A vault that is not there yet is still created.

- A dry run of `--library-export` refuses a file the real run would refuse,
  with exit code `5`, as a dry run of `-ao` does. It only warned ("A real run
  would refuse to write") and exited `0`. So does `-ao --library-export -d`.

- `--library-export` into a directory the run cannot write into, one on a
  read-only volume, or one it may not search is refused with exit code `5`
  before the library is read, in the dry run and the real run alike, and
  beside `-ao` before the highlights are written. The dry run exited `0`,
  the real run read the whole library before the write refused it, `-ao`
  wrote the highlights and then failed the catalogue, and on Python 3.10 and
  3.11 an unsearchable directory was a traceback and exit `1`.

- `-ad` or `-ao` naming an existing directory in the JSON format says it is a
  directory: name a file, or pass `--annotations-format markdown` to write one
  note per book into it. It was said to be a file "already there and could
  not be read (not a regular file)", with advice to move it aside.

- A Ctrl-C landing as the run counted, on its own thread, the copies it could
  not make or a book whose worker failed no longer leaves the report's lock
  held, which hung a second run started in the same process.

- A Ctrl-C landing as a highlights file, a note or a library export was given
  its temporary, before the temporary's name was known, no longer leaves a
  `.ibook2epub-*.part` file beside it. Nor is a Ctrl-C as the run asks
  whether its report was lost, once all the work is done, a traceback: it
  exits `130`.

- A highlights file in a directory the run may not search is refused as
  "Permission denied" on every Python; on 3.10 and 3.11 it was said to be
  "already there and could not be read" when it was not there at all. A
  directory on a read-only volume is named as "Read-only file system" rather
  than "Permission denied".

- A copy whose name on the shelf cannot be looked at (an I/O error from a
  share or USB volume that dropped, on Python 3.10 and 3.11) no longer ends
  the run, a dry run or `--list` in a traceback with exit code `1` and no
  summary: the name is taken as free, as the copy workers already took it,
  and a copy that then fails is counted and says why.

- A Ctrl-C landing exactly as the `--min-free` sampler released its lock no
  longer leaves the report's lock held for the rest of the process, which
  hung a second run started in it; nor can one leave a measurement marked
  as under way for good.

- Under `--on-collision suffix`, a book declaring no usable identifier no
  longer keeps a numbered-looking file that declares one. A deleted
  `Dune (1965)` left `Dune (1965).epub`, and an unidentified `Dune` added
  since was reported exported from it and never written, and the deleted
  book's archive was not listed as an orphan.

- Under `--on-collision suffix`, two books whose names are one file on the
  shelf -- two folders of one name, or a book renamed by case and a namesake
  added under its old spelling -- now read their identifiers, and the book
  the file declares keeps it. The newcomer was reported exported from the
  other's archive and never written, the other was written again under a
  number, and the next run wrote the newcomer too and left the second copy
  an orphan.

- Under `--on-collision suffix --skip-incomplete`, a book renamed by case
  and evicted by iCloud, beside a namesake added under its old spelling,
  keeps its archive. The newcomer was reported exported from that archive
  though both identifiers had been read and differ, and with `--refresh` a
  newcomer declaring no usable identifier wrote over it, the evicted book's
  only one. A book whose plain file declares a usable identifier it does not
  have is now written under a number of its name.

- Under `--on-collision suffix`, a book titled like a number (`Dune (2)`)
  and renamed by case keeps its archive beside two books `Dune` added
  since. The second `Dune` was reported exported from it on every run, and
  the renamed book was written again as `dune (2) (2).epub`. Two books of
  such a title, one exported alone and the other added since, now read
  their identifiers too: the newcomer was reported exported from the
  other's archive and the other written again under a number. A zipped
  book of such a title keeps its copy when packages of the plain name are
  added, rather than being copied again under a number while a package is
  reported exported from its file.

- Under `--on-collision suffix`, a book named from its folder whose
  identifier was read to find its numbered file is no longer reported
  exported from a number a deleted namesake left: it is written under the
  next free number, and the deleted book's archive is listed as an orphan.

- A package added beside a zipped book already copied, when either declares
  no usable identifier, is no longer placed at the copy's file: under
  `--on-collision suffix` it gets a numbered name of its own, under `skip` it
  is a collision. `--force`, `--refresh` and `-ae -ar` no longer write over
  the copy, and `--list`, the orphan list and `-ao` agree.

- A copy is taken for its own bytes only when its size and modification time
  both match. A file matched by size alone is judged by the identifiers
  first, so a same-size book replacing a deleted one is copied or reported,
  not skipped.

- `--skip-incomplete` now opens no evicted book anywhere it is given:
  - An evicted package beside a zipped book of its name is reported exported
    from its own archive, which is no longer listed as an orphan.
  - An evicted zipped book under a deleted book's name is reported not
    downloaded ("cannot tell whether <file> is its copy") rather than copied;
    under `--no-copy-through` the deleted book's archive stays an orphan, and
    the reason says why.
  - An evicted numbered book's numbered and marked files are not listed as
    orphans; without the flag its identifier is still read to keep its
    number.
  - Under `--name-by author-title` an evicted package is reported not
    downloaded, with a warning that it could not be named.
  - `-ao`, `-ad` and `-ae -ar` place, compare and read highlights without
    opening one; a vault says a book's highlights wait on its download
    rather than calling it a lost collision.

- A rerun with nothing to do reads a package's document once rather than
  three times when a zipped book wants its name, and no longer opens a
  zipped book that lost its name to a package.

- A library or shelf behind a directory the run may not search (Full Disk
  Access on macOS) ends with an exit code instead of a traceback: exit 8
  with the Full Disk Access advice for the library, exit 5 ("Cannot read
  output directory") for the shelf. A vault is refused the same way when it
  would be named against a shelf it cannot read.

- A read-only shelf is refused with exit 5 ("Cannot write into output
  directory") once the plan has work for it, in the dry run as in the real
  run, instead of the dry run passing and the real run failing every book.

- A second Ctrl-C no longer breaks the wait for books already being written:
  the run says "Finishing N book(s) already being written...", waits until
  they are done, counts them in the summary, and only then releases the
  output lock and exits 130.

- `-ae -ar` no longer downloads books iCloud evicted in order to refresh the
  others' highlights, and accepts `-w` and `--skip-incomplete`.

- A report that cannot be written (`--list > /dev/full`) is said once and
  exits 5 instead of a traceback; a summary on a closed standard error under
  `-ad -` ends quietly.

- An interrupted dry run says it was interrupted, instead of reading as a
  finished rehearsal.

- With `--min-free`, workers wait while the free space is being measured,
  so no book starts during the measurement that would have refused it.

- `--min-free` refuses a negative value instead of silently disabling the
  floor.

- The last messages that printed a path unescaped (an output directory
  inside the library, the probed library homes, and the container-read
  errors of `-ae`, `-ao` and `-ar`) now escape it.

- A note written before notes were tagged goes only to the one book holding
  every highlight in it: two editions sharing a passage no longer hand one
  edition's note to the other, and a note two books match equally goes to
  neither (it is reported, or numbered past under `--on-collision suffix`).

- A note nothing claims is no longer adopted by whichever namesake comes
  first. Notes record a digest of their book's file (`src=`), so a book
  removed from Books and added again keeps its note even beside a namesake,
  and under `--on-collision suffix` takes back its numbered note instead of
  starting a fresh one.

- A note whose frontmatter identifier Obsidian or a YAML linter rewrote
  (unquoted, single-quoted, escaped, or another ISBN/UUID spelling) is no
  longer taken for another edition's note.

- A book's tagged note stays its own on runs where the book has no
  highlights, so a namesake's outcome no longer depends on that.

- When a book's note name changes (after adopting `--name-by author-title`,
  say), its note is moved to the new name with everything the reader wrote,
  and only onto a name that is still free; if it cannot be moved the run names
  the old file and exits 1 instead of starting a second note.

- That now holds for a note written by 2.3.1 or earlier, which names no book,
  and for the note of a book removed from Books and added again, tagged for
  its old id: each moves with its renamed book instead of being left behind
  while a second note is started. A namesake that keeps the old name (a
  `Dune.pdf` beside the renamed `Dune.epub`) is no longer handed such a note
  to write its own highlights over; the run names the note and exits 1, or
  under `--on-collision suffix` numbers the namesake.

- A note written by 2.3.1 or earlier that two editions both match now goes to
  neither on every run, not just the first. Once that first run had given one
  edition a fresh note, the next run, with nothing changed, handed the old note
  to the other edition and wrote its highlights over what you had written for
  the first. An edition that has another note of its own now steps aside, so
  the PDF's old `Dune.md` beside the EPUB's `Dune (2).md` stays the PDF's. A
  renamed edition's old note is never handed to a namesake that highlighted the
  same passage. A rerun with nothing changed writes nothing.

- When a renamed book's note cannot be moved because a file is already at its
  new name, the error now says what that file is (a file ibook2epub did not
  write, or another book's note) and asks you to move that file aside, instead
  of telling you to move the note onto it, which would have replaced it. When
  the book has two old notes, you are asked to merge them.

- A renamed book's note is no longer held back, with exit 1 on every run,
  because a namesake with no highlights (a `Dune.pdf` beside the renamed
  `Dune.epub`) keeps the old name. The note moves to the book's new name, and
  the namesake starts its own note once it has highlights.

- A package holding a file whose name is not valid UTF-8 is reported failed
  with that file's name ("member name is not UTF-8"), instead of a bare codec
  error that named no file.

- A re-imported book whose note was adopted under different letter case has
  its highlights written into that note, and an older note whose `# `/`## `
  heading an editor trimmed is still recognised.

- A book whose container, package document or encryption.xml declares a DTD
  internal subset is refused: an `<!ATTLIST>` default was copied onto every
  element it named, so a 1.6 KB book reached 2.9 GB and `--verify` and
  `--list` crashed with MemoryError. A book's XML may also no longer hold more
  than 200,000 elements or nest deeper than 1,000 levels.

- Names differing only by case are treated as one name even when they carry
  combining marks (Unicode canonical caseless matching), so `--verify` reports
  such members and the writer never puts two books on one case-insensitive
  path.

- `-ar` no longer hangs when a FIFO is swapped in for a book on the shelf; the
  book is counted as not refreshed.

- A vault run reads the Books library once, so an unreadable library is warned
  about once.

- A detached export whose `generator` holds a key this tool does not write, or
  is not an object, is refused rather than having it dropped.

- `--verify` no longer inflates a book whose members declare more than 1 GiB
  and more than 32 times the book's size; it reports it as too large to check.

- Two zipped copies of one book, a zipped copy and a package of one book, or
  two PDFs of one size, no longer both take one file on the shelf for their
  own: the collision no longer disappears on the next run, and in suffix mode
  the second copy's file is no longer listed as an orphan.

- In suffix mode a numbered or digest-marked book keeps its file when the book
  before it, or its crowd, leaves the library, instead of being written again
  with its archive listed as an orphan. A package zipped in place keeps its
  archive under its digest-marked name.

- A different zipped book or PDF of the same size replacing a deleted one is
  copied: copies keep their source's modification time, and a copy on the
  shelf is recognised by size and time together. Copies made by earlier
  releases are still recognised by size. A PDF of a different size under a
  deleted book's name is copied beside it, or reported as a collision.

- `-ae -ar` finds a package the run moved on past a copy's file and refreshes
  its highlights, and `-ao` names a note after the same file the run writes.

- A DRM-protected or not-downloaded book's vault note is named after the file
  the plan places it at, and the run no longer exits 1 claiming two books
  want one note.

- `--no-copy-through` and `--skip-incomplete` no longer open (and so
  download) books iCloud has evicted to name them or to judge a file under
  another spelling of their name.

- A book that loses its name to a namesake differing only in case is told
  which file holds it, and the identifier read from a book's source to judge
  such a file is read once per run.

- `-ae` and `-ae -ar` warn about highlights Apple recorded against no book,
  which only `-ad` or `-ao` can carry.

- A vault note's name no longer depends on which books have highlights. Every
  book in the library claims its note name, and a note already written for a
  book stays that book's, so a book gaining its first highlight or losing its
  last never moves or blocks another book's note.

- A note is refused as another book's only when that book is still known to
  Books. A book removed and re-added under a new asset id keeps its note,
  which is re-tagged the next time it changes, and the refusal's advice fits
  the case.

- A `.md.new` sidecar the reader has edited is left alone and named, with
  advice to merge or remove it; nothing is written to `.md.new.new`.

- An editor trimming trailing white space no longer makes a note look edited:
  the tool writes no trailing spaces, and notes from older versions are still
  recognised.

- `-ad`/`-ao` refuse an existing export (exit 5, untouched) holding a key of
  the reader's own on an annotation or its book, as they did for a top-level
  key.

- A one-column table delimiter row without a pipe (`:---`, `---:`, `:-:`) in
  a highlight or note is escaped, and an author of `*` or `***`, or one with a
  leading or trailing star, no longer renders as a horizontal rule.

- The free-space warning and the detached export's refusals escape the path
  they print.

- A lock file that is a symlink or a hard link is refused (exit 5, "not a
  plain file") instead of followed: a planted `.ibook2epub.lock` no longer
  truncates the file it points at, creates a file elsewhere, or truncates its
  other name. A dry run judges the lock file the real run opens, so one that
  cannot be opened stops both with 5, and a read-only shelf with a writable
  lock file is no longer refused.

- An output directory that cannot be listed stops `--list`, `--verify`, `-ar`
  and every run with exit 5 ("Cannot read output directory"), where it read
  as empty: `--verify` checked nothing, `--list` showed every book pending,
  and a run exported them all again.

- The repair command `--verify` prints reads back exactly when `-o` or `-s`
  holds a control character or an undecodable byte (bash/zsh `$'...'`
  quoting), and finds a shelf file whose name is stored decomposed. Before,
  running it created a new directory and repaired nothing.

- The summary and every log line that names the output or source directory
  escape it, so a path that is not UTF-8 no longer crashes a strict UTF-8
  stdout after the books are written. A dangling symlink or a symlink loop
  under `-o` is no longer described as "a file".

- `-ae -ar -ad FILE.json` (or CSV) no longer names the zipped books a second
  time for a file that never uses the names. The refresh's own naming still
  opened, and so downloaded, every one iCloud had evicted.

- `-ae` says the highlights of books the `--min-free` floor stopped wait for a
  rerun, rather than that they reached no file. `-ae -ar` no longer says books
  were "copied through unchanged" over a shelf built with `--no-copy-through`.

- Ctrl-C during `--annotations-refresh` says how many books it had refreshed,
  and exits 130.

- A refresh refusing an archive whose directory repeats a member names the
  member.

- A FIFO named `*.epub` in the library no longer hangs `--name-by
  author-title` runs, `--list` and `-d` included, or fails every passthrough
  run: it is skipped with a warning, and an archive's metadata, and
  `--verify`'s check, read only a regular file, judged on the descriptor
  opened rather than a stat of the name.

- `--validate` and `--verify` no longer inflate a member once for every time
  the central directory lists it; they report "members share a local header
  (possible zip bomb)" and skip the content checks. An annotation refresh
  refuses to rebuild such an archive and counts it as failed, where on Python
  3.13 it rewrote a 4 MiB book as 800 MiB.

- An annotation refresh no longer parses an embedded set far larger than the
  one it would write, which cost up to about 1.6 GiB within the 64 MiB cap; a
  set this tool wrote is recognised as current without being parsed.

- `--validate` and `--verify` report member names that differ only by case or
  Unicode normalization, which OCF forbids and case-insensitive filesystems
  merge into one file.

- The check that refuses XML entity declarations stops at the first
  declaration, instead of expanding the document's entity references first.

- Under `--on-collision suffix`, a zipped book copied before a package of its
  name was added is no longer copied again as "Book (2).epub" and listed as
  an orphan: it keeps its file, and the package is written as "Book (2).epub".
  A copy already on the shelf under a numbered name keeps that name too.

- `--no-copy-through` only stops the copying. It no longer reports a package
  as exported from a zipped book's file, lists copied books as orphans, counts
  zipped books as "ignored (not books)", or leaves them out of a vault.

- A book already on the shelf keeps its name when a book whose name differs
  only in case is added: in skip mode the newcomer is the one collision, and
  in suffix mode it becomes "Dune (2).epub". Every route (a run, `--list`,
  `-ar`, vaults) names the library the same way.

- A book renamed only by case finds its own archive under the old spelling
  instead of colliding with it on every run, and `-ar` and the vault name its
  note after the archive's actual spelling.

- The shelf is read case-insensitively, PDFs included: a copy named
  `Foo.EPUB`, or a PDF, is seen, so a package `Foo.epub` is not judged free to
  write over it, a same-named PDF added later is reported or numbered, and
  such files no book claims are listed as orphans.

- Two different zipped books with the same name and size are told apart by
  their identifiers; the second is no longer silently never copied.

- `-ae` and `-ae -ar` warn when highlights belong to zipped books or PDFs
  copied through unchanged, which cannot hold embedded highlights. They no
  longer say the highlights of books held back by `-m` "reached no file";
  those wait for the books to be converted.

- A run over only PDFs and zipped books, or a `--match` selecting only those,
  no longer says "No matching *.epub packages found".

- `--list` and `--list --json` show each file copied through as `copy` or
  `copied`, and say "N not copied (--no-copy-through)" under that flag.

- A small epub whose members declare a small size but decompress to
  gigabytes (a "zip bomb") no longer makes the reader, `--verify` or an
  annotation refresh allocate gigabytes and crash with an uncaught
  MemoryError. Members compressed with anything but stored or deflate, which
  OCF forbids, are refused unread, and every other read is bounded.

- An already-zipped book whose zip directory holds a name flagged UTF-8 that
  is not, or that needs a zip version newer than Python supports, no longer
  ends a `--name-by author-title` run with a traceback; it keeps its own
  filename.

- `--epubcheck` no longer crashes when epubcheck writes output that is not
  UTF-8, such as a member name in ISO-8859-1.

- Placeholder ISBNs such as 0000000000, any repeated digit, 0123456789 and
  their ISBN-13 forms are no longer exported as ISBNs to the Goodreads CSV or
  note frontmatter.

- Highlights Apple recorded against no book are no longer embedded in, or
  written into the note of, a book that happens to be called "Unknown book".
  They still appear in detached exports.

- `--verify` and `--validate` check an archive with many duplicate member
  names in linear time, and name at most five of them.

- A book whose `META-INF/encryption.xml` or `sinf.xml` is a directory or a
  FIFO is treated as protected rather than exported, and `encryption.xml` may
  no longer declare XML entities, like every other document read.

- `--verify`'s repair command names the output directory (`-o`) and, when you
  gave one, the library (`-s`), so it works as printed; it reminds you to add
  the naming flags you export with. It also works for names containing
  control characters, tabs or undecodable bytes, and for names starting with
  a dash.

- A symlink loop in `-o` or `-s` no longer ends argument parsing in a
  traceback on Python 3.10-3.12. A dry run whose output path runs through a
  symlink loop, or onto a read-only or unwritable location, exits 5 as the
  real run does, instead of 0.

- `--list`, `--list --json`, `--verify` and a conversion's summary piped into
  `head` or a pager end quietly with their own exit code instead of a
  BrokenPipeError traceback.

- `-ao`/`-ad` with a Markdown vault accept `--skip-incomplete` and
  `--workers`, which they use when naming notes.

- `--match` finds book names stored in decomposed Unicode (from HFS+), so
  `--match café` matches "Café Society".

- The `-m` help says `0=unlimited`, which no terminal width splits.

- Bidirectional control characters (such as U+202E) in book names are escaped
  in terminal and log output and in JSON printed to standard output. The
  Goodreads CSV keeps them, so a right-to-left title imports intact.

- The lock holder quoted when another run holds the output lock is escaped,
  and a lock file that is not UTF-8 no longer turns "another run is using
  this directory" (exit 3) into a traceback.

- `--list` and `--verify` say they are reading the output directory, not
  "Writing output to".

- Two books whose names differ only in extension or case (a package and a
  PDF, say) no longer share one vault note. The second is reported as a name
  collision, or gets `Name (2).md` under `--on-collision suffix`.

- `-ao` names vault notes after the book's file on the shelf, as `-ad` does,
  so it no longer writes one edition's highlights over another edition's
  note. Each note's marker now names its book, and a note tagged for another
  book is never rewritten (exit 1). Older untagged notes are still
  recognised, and gain the tag only when their highlights change.

- A line between the frontmatter and a note's start marker, or above the
  marker once the frontmatter is deleted, no longer makes ibook2epub call its
  own note foreign and exit 1 on every run.

- The README states which annotation destinations keep highlights deleted in
  Books (only the `-ad`/`-ao` JSON file) and which mirror Books.

- An `-ad`/`-ao` JSON export with id-less, non-string-id or duplicate-id
  entries, or unknown top-level keys, is refused (exit 5, left untouched)
  instead of losing those entries on merge.

- `--verify` checks books on the shelf whose extension is not lower case,
  such as a copied-through `Foo.EPUB`.

- `-ao -` and `--library-export -` write UTF-8 whatever the terminal's
  encoding, instead of crashing with UnicodeEncodeError.

- `--library-export` refuses a vault note's name in any case (`Dune.MD`), so
  `--force` cannot write the catalogue over a note on a case-insensitive
  volume.

- A package and an already-zipped book that want one shelf name no longer
  shadow each other. The package gets the name; the copy is reported as a name
  collision, or copied as `Name (2).epub` under `--on-collision suffix`.
  Before, the copy could land first (and the package was then "exported" from
  the zipped book's file), be dropped with no message, or be overwritten by
  `--force`. Two zipped editions under one name are handled the same way.

- A book that lost its name no longer has its own archive listed as an orphan
  under the default policy (`b/dune.epub` beside an added `a/Dune.epub`).

- With `-ae`, the run warns that highlights reached no file for a book that
  `--force` or `--refresh` found to be a collision under a folder-named policy.

- `-ae -ar` under a folder-named policy reads only the books with highlights
  again: 2,000 books with one highlight went from 2,000 package reads and
  2,002 archive opens to 1 and 3.

- `--match` narrows the PDFs and zipped books copied through. `-m` still does
  not cap copies, and its help now says so.

- A vault note is named after the file the book is on the shelf at, so an
  edition moved to its marked name no longer writes into another edition's
  note.

- A vault writes a note for each already-zipped book or PDF that has
  highlights, under conversion, `-ar` and `-ao`.

- A dry run reports the files it would copy ("N to copy") and the copies it
  would skip.

- A manifest href such as `" //[x#f"` (a space, tab or carriage return before
  `//[`) no longer ends the whole run with "Invalid IPv6 URL", including under
  `--dry-run` and when a zipped book is named with `--name-by author-title`;
  the other books are still exported.

- `--epubcheck` and `--verify` escape control characters in the problems
  epubcheck reports and in the archive name they log, so a book's member
  names can no longer send escape sequences to the terminal.

- `--verify` no longer hangs on a FIFO named `*.epub` in the output directory,
  and no longer reports a directory named `*.epub` as damaged (exit 7).

- A highlight with no asset id is exported under "Unknown book" again instead
  of being skipped with a warning.

- A file that is not UTF-8 at a note's or sidecar's path no longer crashes a
  Markdown vault run; it is left untouched, reported as unreadable, and the
  other books' notes are still written (exit 1).

- A vault note whose frontmatter the reader deleted is recognised as the
  tool's own and updated again, instead of being reported as "not written by
  ibook2epub" on every run; the frontmatter stays deleted.

- JSON written to standard output (`-ao -`, `-ad -`, `--library-export -
  --library-format json`) escapes C1 control characters such as CSI and still
  decodes to the same data; files keep the exact characters.

- An annotation export saved with a UTF-8 byte-order mark is merged into on
  the next run instead of being refused (exit 5).

- `--verify`'s repair advice now selects the damaged book. A title containing
  `?`, `[` or `*` is escaped, and a pattern that would also match another book
  is anchored. A damaged file not named after any package (copied through, or
  suffixed) is to be moved aside; the next run puts it back. The advice
  escapes control characters.

- A Ctrl-C while the detached highlights or a vault are written, or while the
  output lock is taken and the shelf swept, still prints the run's summary,
  says the highlights were not written, and exits 130.

- `-ae -ar -d` exits 5 for an output directory that does not exist, as the
  real refresh does.

- `--covers` escapes the book's name and the reason when it logs that no
  cover was written.

- `--list` and `--verify` refuse `--dry-run` and `--annotations-none`, which
  neither consults.

- A note no longer escapes a line that only starts with a block opener's
  character: `**bold**`, `#tag`, `2.5 million`, `-5 degrees` and `+1` are
  written as typed. A table delimiter row led by a colon (`:-- | --:`) is
  escaped, so a note can no longer turn into a table.

- Control characters in a title or author -- U+0092 left by mojibake, say --
  are escaped in a note's frontmatter, where they made Obsidian drop every
  property of the note.

- `-ao DIR --annotations-format markdown` names each book whose highlights
  were left out because it lost a name collision, and suggests
  `--on-collision suffix`; they were dropped without a word.

- A notes run exits 1 when a file it cannot read, or did not write, stands
  where a book's note should go, so that book's highlights were saved
  nowhere; the file is still left alone.

- `--verify` refuses the conversion and naming flags it never consults --
  `--match`, `--force`, `--portable-names`, `--name-by`, `--on-collision` --
  and `--list` refuses `--covers`, `--validate`,
  `--epubcheck`, `-m`, `--min-free` and `--no-shuffle`.

- The README no longer says every run exits 8 without Full Disk Access:
  `-ao` and `-ar` do; `-ae` and `-ad` log the refusal and convert anyway.

- `--annotations-refresh` writes a book's highlights only into the archive
  that is that book's own. It wrote them into whatever file had the book's
  name -- which can hold another edition, likely the last copy of one deleted
  from the library -- and a book moved on to its marked name under
  `--on-collision suffix` never got its own. The warning that highlights
  "reached no file" now also names a book whose name is held by another.

- A highlight from an already-zipped book is no longer embedded in, or
  written into the note of, a package with the same name; it goes to
  neither, with a warning, as for two same-named packages.

- `--annotations-refresh` stops at the `--min-free` floor and exits 1 when a
  book could not be refreshed, instead of logging and exiting 0; a dry run
  of it reads Apple's container as the real run does.

- After Ctrl-C the run no longer writes the detached export or notes, or
  warns that highlights reached no file; it says they were not written. The
  exit code follows one documented order -- 130, then a failed book, then
  the annotation destination -- so it cannot contradict the summary.

- A failed copy no longer turns books the `--min-free` floor stopped into
  "failed" books in the summary, and `-o` pointing beneath a file is
  refused by `--dry-run` and `--list` as by a real run.

- One annotation whose style is an infinity no longer ends the whole
  annotation export; it loses its style. A book whose recorded path holds a
  NUL byte loses only its package metadata, not its highlights or its
  catalogue entry.

- `--verify` and `--validate` escape control characters in the names they
  report -- manifest hrefs, item ids, spine idrefs, member names, rootfile
  paths -- so a percent-encoded `%1B` in a book can no longer erase the line
  reporting it.

- The annotation export no longer writes entries that break its own schema:
  an empty id is skipped, a negative style left out, a CFI stored after a
  book address written bare as `epubcfi(...)`, and a location that is not a
  CFI left out.

- `encryption.xml` is read with the same bound as its size check, so a file
  that grows in between fails closed.

- In `--on-collision skip`, the archive of a book that lost its name to an
  edition added later -- its only copy -- is no longer listed as an orphan.

- Under `--name-by author-title`, each archive on the shelf is read once per
  run instead of twice.

- A run without `-s` no longer crashes with a traceback when macOS refuses
  to let it look into the second place Apple has kept the library; that
  place is passed over and the first is used.

- A package member swapped for a hard link after it was checked is refused
  when opened, as a swapped symlink already was.

- The test suite shipped in the source distribution passes: its check of
  the CI configuration skips where `.github` is not included.

- Refreshing a book's annotations keeps the permission bits the user set on
  it, and refreshes through a shelf entry that is a link instead of
  replacing the link with a plain file.

- `--validate` and `--verify` report an archive holding two members of one
  name, which OCF forbids and readers resolve differently. It passed.

- A run narrowed by `--match` gives each book the name a full run gives it.
  Names were assigned over the matched books alone, so under
  `--on-collision suffix` one edition of a crowded title lost its marker: a
  book already exported was written a second time under the plain name, and
  every later full run reported the duplicate as an orphan.

- `--refresh` and `--force` no longer write one book over another's archive
  when their package folders share a name: same-named packages in different
  subfolders, or names `--portable-names` folds together such as `Café` and
  `Cafe`. The run reads that one source's identifier before replacing an
  archive, and reports a collision when it is another book's.

- A book whose archive reads back with a decomposed name, as HFS+ stores
  names, is recognised as exported rather than colliding with itself for
  ever, so it can be refreshed and forced again.

- `--list` and the run log escape control characters in the reason shown
  beside a book, and `--list` and `--list --json` no longer crash on a file
  name that cannot be decoded.

- An archive holding a different book from the one its name belongs to --
  an edition deleted from the library, say -- is reported as an orphan
  rather than hidden.

- Under `--on-collision suffix`, a book whose plain name is held by another
  book's archive is exported under its digest-marked name, where it used to
  be a collision on every run.

- `--min-free` stops every book not yet started once any check finds the
  volume below the floor; only the sampled book used to stop while the rest
  kept writing. Those books are reported as not attempted, not failed, and
  the floor now applies to files copied through (PDFs and already-zipped
  books) as well.

- A copy-through that fails is counted as a failure, so the run exits 1 and
  says so, where it exited 0 with a clean summary.

- `--annotations-refresh` exits 3 when another run holds the output lock and
  5 when the lock file cannot be opened, instead of crashing; and which of
  the two a run gets no longer depends on the text of the output path.

- A full output volume no longer crashes the run while it records its
  process id in the lock file.

- Ctrl-C while the library is being read and named, or during
  `--annotations-refresh`, exits 130 with a summary instead of a traceback;
  the refresh's says how many books it had already refreshed. Ctrl-C during
  `--list` or `--verify`, which have no summary to give, exits 130 without a
  traceback.

- A dry run no longer counts the books it would export as remaining, and
  says when `--max-export-files` held books back.

- `--dry-run` and `--list` exit 5 when `-o` names a file, as a real run does.

- A highlight that two same-named packages in different folders could both
  own is embedded in neither book and written to neither note, with a
  warning. Only `--annotations-refresh` checked for this before; the
  conversion and the notes vault gave each book the other's highlights.

- One unusual value in Apple's databases no longer costs the whole
  annotation export or library catalogue. A BLOB where a note, chapter or id
  belongs, or TEXT that is not valid UTF-8, made the export crash or fail
  outright; now the bad value is left out or its bytes replaced.

- A FIFO inside a package no longer hangs the library or annotation export,
  metadata naming or cover extraction. Package members must be regular files,
  checked on the open descriptor, and reads are bounded.

- An `encryption.xml` or package document declaring an encoding the XML
  parser refuses (Shift_JIS, EUC-JP, UTF-32, or an unknown one) no longer
  crashes the run: encryption fails closed as protected, and the package is
  treated as unreadable. A package directory that is a symlink loop is
  unreadable too, rather than a crash on Python 3.10 to 3.12.

- `encryption.xml` is read in linear time. Deeply nested blocks could take
  close to a minute per book.

- A 13-digit identifier counts as an ISBN only under the book prefixes 978
  and 979. Other EAN-13 barcodes were exported as `urn:isbn` and as the
  Goodreads ISBN13.

- Text-fragment locators percent-encode `-`, so a highlight that starts or
  ends with a dash is no longer read as a prefix or suffix.

- `--validate` and `--verify` require `mimetype` to be physically first in
  the archive, not only first in its index.

- Exported dates always carry a four-digit year.

- A package already carrying `META-INF/annotations.json` no longer produces
  an archive with two members of that name when annotations are embedded.

- A book with a member larger than 2 GiB can be exported; ZIP64 headers are
  written when needed, and every other book exports byte for byte as before.

- A cover is written to a temporary file and published only when complete,
  so a failed copy no longer leaves a truncated cover no later run replaces.
  Its extension is lower-cased and limited to image types, so a book can no
  longer write `Book.EPUB` beside `Book.epub`, or a non-image file.

- `--portable-names` escapes Windows device names followed by any extension
  (`NUL.tar.epub`), the superscript `COM¹` to `LPT³` forms and
  `CONIN$`/`CONOUT$`, and replaces undecodable bytes rather than writing
  invalid UTF-8.

- Rewriting the detached annotation export or a note keeps the file's
  permission bits, writes through a symlink instead of replacing it, and
  flushes the new contents to disk before the rename.

- Refreshing the annotations in an exported book streams its members rather
  than holding the whole book in memory.

- Names from the library are escaped in log lines, so control characters in
  a file name no longer reach the terminal.

- `--list` and `--verify` refuse a flag that would write: `-ae -ar` used to
  run the refresh instead, rewriting every archive on the shelf and neither
  listing nor verifying anything, and `-ae` or `-ad` wrote nothing and
  exited 0.

- `--annotations-refresh` refuses `--match`, `--force` and every other
  conversion-only flag. `-ae -ar --match X` used to refresh every book on
  the shelf.

- A note whose name is at the filesystem limit gets a sidecar it can write.
  `<note>.md.new` passed 255 bytes and the whole vault write crashed; a note
  that cannot be checked now costs that note, not the run.

- A highlight or note that opens a code fence, an HTML block such as
  `<!--`, a setext underline, a `___` rule, a link reference definition, a
  table row or an indented code block no longer hides or restyles the rest
  of the note. Indentation is kept as no-break spaces.

- An existing detached export holding a lone surrogate (`"\ud83d"`) is
  refused with exit 5 and left as it is, where it crashed with a traceback.

- The README gives exit code 8, not 4, for missing Full Disk Access, and its
  exit-code table matches `exits.MEANINGS` word for word, which a test now
  enforces.

- One package the run could not search no longer ends the whole run. A
  directory without search permission -- or, on macOS, one refused by the
  privacy settings -- raised out of the check that keeps every read inside
  its book, with a traceback and nothing exported. That package is now
  refused like any other path the check cannot vouch for.

- `--log-file` no longer drops a line that names a file whose name is not
  valid UTF-8. The line is written with the undecodable bytes escaped,
  where it used to be lost and replaced by a traceback on the console.

- `--validate` no longer rejects a book whose manifest names a URL with a
  scheme it did not know. Only `http`, `https`, `ftp`, `ftps`, `data` and
  `mailto` counted as remote. Any other scheme -- `kindle:embed:`, `tel:`,
  `urn:` -- was looked for as a file inside the book, reported missing, and
  the book was never written and was retried on every run. An href with any
  scheme, or starting `//`, now names something outside the book. A query is
  also no longer read as part of a file name, so `ch1.xhtml?x=1` finds
  `ch1.xhtml`.

- Under `--name-by author-title`, a book is no longer reported exported by an
  archive that holds a different book with the same name, and `--refresh` and
  `--force` no longer write over that archive. With no state file, a run took
  a book whose name was on the shelf to be exported. But a run narrowed by
  `--match` names only the books it selected, so two editions of one title
  could each take the name alone. And once the edition holding a name was
  deleted from the library, the next edition took it. Each case was reported
  `exported` and never written, and `--refresh` then replaced the other
  edition's archive, which for a book deleted from Apple Books could be its
  last copy. The run now reads the identifier of the archive already on the
  shelf and reports a collision naming both books when they differ. A book
  with no usable identifier cannot be told apart this way, and its name is
  trusted as before. A TLA+ model of the planner found all three cases.

- A run no longer deletes the temporary file of another run that is still
  writing it. A run that cannot take the output lock carries on unlocked --
  on NFS, `flock` fails with `ENOLCK` when the remote lock manager does, and
  only for as long as it does -- so a later run could hold the lock while that
  one was mid-write, and its sweep of abandoned temporaries deleted the other
  run's file and failed that book. The sweep now takes only a temporary left
  untouched for an hour. A TLA+ model of the output directory, now checked in
  CI, found the interleaving.

- A damaged compressed member no longer ends a run with a traceback. Reading
  one raises `zlib.error` for deflate or `lzma.LZMAError` for LZMA, and nothing
  caught either. `--verify` stopped at the first such archive instead of
  reporting it; a run naming zipped books from their own metadata, as
  `--name-by author-title` does, stopped on one damaged book in the library
  before writing anything; and `-ar` gave up on every book after a damaged
  archive. Now `--verify` counts the archive as damaged, the book keeps its own
  filename, and the refresh names the archive it could not update and goes on.
  ([#21](https://github.com/raeq/ibook2epub/issues/21))

- A manifest item whose `properties` only contain the word `cover-image`, such
  as `not-cover-image` or `x:cover-image`, is no longer taken for the cover; the
  attribute is a list of values separated by XML white space, and only a whole
  value counts.

- A book identifier made of superscript digits no longer stops the run with a
  `ValueError`, and one in Arabic-Indic digits is no longer written back as an
  ISBN. Only ASCII digits make an ISBN.

- A highlight's document is read from the step just before the CFI's first
  `!`. A CFI that also points inside an embedded SVG or iframe no longer
  resolves to that element's id, and neither an assertion on an earlier step
  nor a location that is not a CFI names a document. An ID written with an
  escaped character, such as `[ch^[15^].xhtml]`, resolves to its document, and
  one followed by parameters (`[ch15.xhtml;s=b]`) is looked up without them.

- In an exported note, a line that would open a numbered list is escaped as
  `1\.` rather than `\1.`, which Markdown showed with its backslash. A line
  starting with a digit from another script, such as `١.`, is left alone, and a
  line that forges the start marker followed by white space is escaped like any
  other.

- `--epubcheck` no longer fails a book it could not check. An epubcheck that
  cannot be run, or runs past its timeout, is logged: a book that took longer
  than the timeout was left out of the output directory and retried on every
  run, and `--verify` counted it as damaged. A `FATAL` message is reported like
  an `ERROR`, and both of epubcheck's output streams are read, so a JVM notice
  on one no longer hides the errors on the other.

- A book zipped with Info-ZIP (`zip -X0 book.epub mimetype`, then
  `zip -rX9 book.epub META-INF OEBPS`), whose non-ASCII member names are
  UTF-8 but not flagged as such, is read by those names:
  - `-ae -ar` no longer renames such a member while refreshing the book, which
    left `OEBPS/第1章.xhtml` stored as `OEBPS/τ¼¼1τ½á.xhtml` and the book
    broken, and reported it refreshed.
  - `--verify` and `--validate` no longer call such a book damaged ("manifest
    item is not in the archive"), and a package document at a non-ASCII path
    is found. A name stored twice, once flagged and once not, is reported as
    a duplicate.

- `--verify` and `--validate` warn of a `mimetype` member whose local header
  carries an extra field, which OCF asks against and epubcheck rejects.
  `zip` run without `-X` writes one, and readers open such a book, so it is
  said on stderr (`Hand Made.epub: mimetype carries a 28-byte extra field;
  OCF asks for none, readers open it`) and not counted damaged: the exit
  code is unchanged and no repair is advised. A book copied through keeps
  its bytes, so calling it damaged had `--verify` advise a rerun that copied
  the same bytes back, and it never passed. Under `--epubcheck`, epubcheck's
  own verdict still stands.

- A container that lists another rendition, such as a PDF, ahead of the
  package document is read at the package document: the first rootfile whose
  media type is `application/oebps-package+xml`. The PDF was parsed as the
  package document and a sound book called damaged.

- A book retitled in Books, or whose language, year or declared identifier
  changed, takes its new values in a merged `-ao` or `-ad` file rather than
  keeping the old ones for as long as its highlights were not edited.
- A dry run judges the `--min-free` floor where the real run does: before the
  first copy or book, and at the first archive `-ae -ar` would rewrite. On a
  volume below the floor it said "would export" every book and exited `0`,
  where the real run wrote nothing and exited `1`; it now ends as the real run
  does. A rerun with every file already copied is no longer stopped by a full
  volume it has nothing to write to, and a run the floor stops says
  "Only N MiB free" once, not twice. A dry `-ae -ar` says how many books it
  would refresh, and exits `1` for an archive the refresh could not read.

- A file named by `-ad FILE` or `-ao FILE` is judged before anything is read
  or converted, in a dry run too: a directory that is not there or cannot be
  written, or an existing file that is not an annotation export, is refused
  with exit `5`. The dry run exited `0`, and `-ad` converted the whole library
  before refusing the file. The message names the file, not the temporary
  written beside it.

- A Ctrl-C landing as `-ae -ar` closes a member of the archive it is
  rebuilding stops the refresh with `130`. It was taken for a book that could
  not be read, and the refresh went on through the rest and exited `1`.

- A copy whose target cannot be looked at (an I/O error on a network share
  or a USB volume, on Python 3.10 and 3.11) is counted as a failed copy. It
  ended the run in a traceback with no summary.

- A closed standard output is a lost report (exit `5`): `--list >&-` lost its
  listing and exited `0`. Under `-ad -` with standard error closed, the summary
  no longer lands in the JSON on standard output. `-ao -` and
  `--library-export -` into a full or closed standard output say so and exit
  `5`, where each ended in a traceback.

- A Ctrl-C just after a temporary is made no longer leaves a `.part` file
  beside a highlights export, a note or a book `-ar` refreshes.

- A `--log-file` that stops taking writes, such as one on a volume that fills
  up, is said once and then left alone; it printed a "Logging error"
  traceback for every line of the run.

- `-s` naming a file says the path is not a directory, rather than that it
  does not exist.

- A note written before notes named their book, holding only passages two
  editions both highlighted, goes to neither edition on every run. One that
  had a note of its own elsewhere -- which may be the fresh note the tie
  itself gave it -- stepped aside, so once the reader deleted the other's
  empty duplicate, or edited a line of its note, the other edition took the
  note: its highlights written over it, the note tagged for it, and the
  reader's writing on the first edition left under them, without a word.
  Such a note is now left as it is and each edition numbered past it, or
  refused it, as when neither had a note of its own; an upgraded vault's
  PDF note that both editions hold stays where it is and the PDF starts a
  fresh one.

- A note written before notes named their book is not moved to another
  book's new name while it lies at the name a book in the library is given,
  highlighted or not: the book with no highlights today cannot say the note
  is not its own. The note of a `Dune.pdf` whose highlights were all deleted
  was moved to another edition's note name and tagged for it, the reader's
  writing on the PDF with it, when that edition highlighted the note's
  passage. It is named and left alone, and the run exits `1`, as a note
  that cannot be moved is.

- A Unicode Path extra field (0x7075) never names a member `mimetype`, nor
  renames the one that is: a first member stored as `XXXXXXXX` with a field
  saying `mimetype` passed `--verify`, and `-ae -ar` renamed a real
  `mimetype` after a field saying `zzz`. `--verify` also requires the first
  local header's own name to be `mimetype`.

- A Unicode Path field Python 3.14's zipfile refuses — shorter than its
  version and CRC, or vouching for the name in bytes that are not UTF-8 — is
  damage on every Python: `--verify` exited `7` on 3.14 and `0` on 3.10 for
  one book. Naming a book by its metadata and `-ae -ar` refuse it too.
  zipfile's own `UserWarning: Empty unicode path extra field` no longer
  appears above the report on 3.14.

- A member's extra fields are read in time linear in their size: 64 KB of
  empty records cost some 512 MB of copying per member.

- `--library-export` through a symlink is judged in the directory the link
  leads to, where the catalogue is written: a link into a directory the run
  may not write passed the dry run and was refused after the library was
  read, and a link out of one was refused though the write would go through.

- `--library-export` naming the vault the same run makes, or a directory
  above it (`-ao out2/vault --annotations-format markdown --library-export
  out2`), is refused up front with exit code `5`; the dry run passed, and the
  real run wrote the notes and then failed. So is a name too long for its
  filesystem.

- `--list`, `--list --json`, `--verify` and a run's summary are written as
  UTF-8 under `PYTHONIOENCODING=ascii` or a Latin-1 locale. The first title
  in another script ended each in a traceback and exit `1`, after the books
  were written.

- A dry run that would write a vault says that it did not open the vault's
  notes: a note the real run leaves alone and exits `1` for went unmentioned,
  and the dry run exited `0`.

- A renamed book with two notes under older names, one of them at the name
  an idle namesake is given, is told to merge them with the caveat that that
  one may be the namesake's; the caveat was dropped. The log no longer calls
  such a note the renamed book's.

- A name ending in `(1)`, `(0)` or a number with a leading zero, such as a
  book whose folder is `Dune (1)`, is not read as a number of `Dune`: taken
  for that name's plain file, another book's, it had every `Dune` without an
  identifier refused its own file and renumbered, a book reported exported
  from another's file, and the first one's only archive listed as an orphan.

## [2.3.1] - 2026-09-11

### Changed

- A macOS permission refusal exits `8`, not `4`. Both used to exit `4`, whose
  meaning is "the source directory does not exist, or no library was found",
  so a scheduled run could not tell "grant the terminal Full Disk Access" from
  "there is no library here". A script that read `4` as "fix the path" now sees
  `8` for a refusal, from `-ao`, `--library-export`, and `-ar` when it converts
  nothing. The README's exit-code section also stops claiming that `2` covers
  failures that have long had codes of their own.
  ([#19](https://github.com/raeq/ibook2epub/issues/19))

### Fixed

- A missing Full Disk Access grant and a missing Books container are told
  apart. Each had one fixed message, and each fired for the other: a refused
  container read as "Apple Books may never have run here", and a folder that
  was there but empty, or absent, as "the terminal needs Full Disk Access". The
  tool now asks the operating system and blames the permission only when it
  refuses. A refusal names the grant and where to give it; an absent or empty
  folder says Books has not created the database yet.
  ([#9](https://github.com/raeq/ibook2epub/issues/9))
- Books taken along rather than converted — PDFs, and books that arrived
  already zipped — are copied concurrently, by as many workers as `-w` sets.
  They were copied one at a time, before the conversion pool started, so on a
  library iCloud had evicted every PDF was downloaded in turn while every
  worker sat idle: a run with `-w 64` was seen fetching them at about 4.5 MB/s.
  Files that would land on the same name are still copied in sorted order, so
  which one reaches the shelf does not depend on which download finishes
  first. ([#10](https://github.com/raeq/ibook2epub/issues/10))
- `--skip-incomplete` skips PDFs and already-zipped books iCloud has not
  downloaded, as it already skipped packages. It checked packages only, so a
  run told to leave evicted books alone downloaded every evicted PDF anyway.
  The check is a stat, which downloads nothing. A skipped file is counted in
  the summary's "not downloaded", and one whose copy is already on the shelf is
  not reported at all. Under `--name-by author-title` an evicted zipped book is
  not opened to name it, so it always counts as not downloaded.
  ([#12](https://github.com/raeq/ibook2epub/issues/12))
- Under `--name-by author-title` an already-zipped book is opened once per
  run, and in parallel. The orphan check named every one in a loop before the
  run started, and the copy then opened each again, so on a library iCloud had
  evicted every zipped book was downloaded one at a time before any work began.
  `--list` did the same. Under `--skip-incomplete` an evicted zipped book is no
  longer opened anywhere; since its name cannot be known without it, the run
  says how many went unnamed, and that a copy of one already on the shelf
  counts as an orphan. ([#14](https://github.com/raeq/ibook2epub/issues/14))
- The end-of-run summary says why books remain. It advised "rerun to continue,
  or pass -m 0" for every book not exported, which was wrong twice over on a
  real run whose one remaining book had failed under `-m 0`: the flag was
  already given, and a rerun fails the same book again. The count is unchanged;
  the advice is now split. Books the cap held back are pointed at a rerun or
  `-m 0`, books an interrupt or a full disk left unattempted at a rerun, and
  failed books at the errors above.
  ([#15](https://github.com/raeq/ibook2epub/issues/15))

## [2.3.0] - 2026-09-09

### Added

- `--library-export FILE` writes out your library. It reads the `BKLibrary`
  database the highlights already come from and records what you own: title,
  author, when each book arrived, and the collections you sorted it onto. By
  default it writes a Goodreads-format CSV, which The StoryGraph and most other
  trackers import directly; `--library-format json` writes the canonical record
  instead, described by a second shipped schema, `library.schema.json`. It
  converts nothing, like `-ao`, and composes with it.

  It is a catalogue, and named for what comes out. Measured against a
  3,620-book library, Apple holds titles, authors, collections and acquisition
  dates for all 3,620 and little else: the rating is zero on every row, the
  year and the finished flag are null throughout, and eleven books carry a
  finish date. The exclusive shelf therefore follows the evidence: a finish
  date, the finished flag, or reaching the end of the book. Lacking all three
  it stays blank, rather than defaulting to `to-read` for the 3,389 books that
  would have taken it. `--unknown-shelf` fills the blanks with a value you
  choose, and fills that column only.

  Most rows will import unmatched, because most books carry a UUID instead of
  an ISBN, and the run says so before writing anything: on that library, 1,108
  of 3,620 match. An ISBN also has to pass its check digit, so the 68
  identifiers there that run to ten or thirteen digits and fail it never reach
  a tracker as ISBNs. `--dry-run` gives you the estimate and writes nothing;
  `--no-isbn` skips opening each book's package document, one read per book
  where the highlights cost one per annotated book.

  The collection join runs from the asset side, on `ZASSETID`, because 749 of
  14,500 membership rows in that library point at deleted books and would each
  have become a title. `Date Added` comes from `ZPURCHASEDATE`, which carries a
  value on every row including sideloaded books and spans the life of the
  library. `ZCREATIONDATE` dates the database row instead, beginning in
  mid-2025 for a library going back to 2016, so nothing reads it. The CSV's
  `Bookshelves` leaves out the collections Apple fills itself (Library,
  Downloaded, Books, PDFs, Audiobooks, My Samples), which describe storage
  rather than shelving; the JSON carries all of them. A cell opening with `=`,
  `+`, `-` or `@` gains a leading apostrophe, because a title is input and a
  cell like that runs as a formula when you open the file in a spreadsheet.
  Control characters get the escaping this tool already gives the names it
  prints.

  `--library-export` leaves an existing file alone unless you pass `--force`,
  and it checks that, the destination's directory, and whether the name
  belongs to a note in a vault the same run writes, before it reads the
  library. The export replaces rather than merges, and a Goodreads export at
  the same path carries the very same header, so the tool cannot tell its own
  file from yours.

- The annotation export's `book` carries the author's sort name as
  `authorSort` when the package document declares one, for the author it
  sorts, and falls back to the package document's `dc:creator` when Apple
  recorded no author.

### Changed

- An annotation's `created` is optional, and absent when Apple recorded no
  usable creation date. The export used to stamp it with the moment it ran,
  which made an embedded set move with the clock and forced every `-ae -ar`
  run to rewrite that archive. Such an entry sorts after its book's dated
  highlights, where the invented stamp had put it, and a rerun regenerates one
  that an older export carries.

- A vault note carries an `isbn:` line only for an ISBN that passes its check
  digit. A package declaring `urn:isbn:` in front of digits that fail theirs
  keeps its `identifier:` line and gets no `isbn:` line. Existing notes keep
  what they have: the frontmatter is yours.

- The annotation export claims `book.filename` only when the naming policy can
  name the book, which under `--name-by author-title` means it read the
  package document. It used to fall back to the package directory's name and
  present that as the name on your shelf.

- A run that converts nothing (`--annotations-only`, and now
  `--library-export`) refuses the conversion flags it used to accept and
  ignore: `--list`, `--verify`, `--covers`, `--validate`, `--epubcheck`,
  `--refresh`, `--skip-incomplete`, `--match`, `-m`, `--workers`,
  `--min-free`, `--no-copy-through`, `--no-shuffle`, and `--force` where it
  has no file to replace. `-o` and the naming flags still work: `-o` because a
  released version took it beside `-ao` and it cannot change what an export
  contains, and the naming flags because a vault names its notes the way the
  shelf names its books.

- `coredata` now holds the code that reads Apple's databases, shared by the
  annotation and library exports, and `AnnotationsUnavailableError` becomes
  `coredata.ContainerUnavailableError`: the condition it names is the container
  being unreadable, whichever export asked. `library.describe_book` now
  describes a book from a library row for both exports, so the two cannot
  describe one book two ways. The detached-file writers moved out of `run` into
  `detached`. None of this changes the command line.

- The release workflow's artifact actions move to `actions/upload-artifact@v7`
  and `actions/download-artifact@v8`. v5 defaulted to Node 20, which GitHub now
  forces onto Node 24 with a deprecation warning on every run. Both new majors
  target Node 24 themselves, their new parameters are opt-in, and v8 makes a
  hash mismatch on download an error rather than a warning, worth having on
  the one job that holds `id-token: write`.

### Fixed

- A vault could write no notes at all and report success. A note takes its name
  from its epub, so writing one needs the library. Without it the run wrote
  nothing and exited 0. It now reports a missing library like any other run
  that needs one, and says so too when the library is there but holds none of
  the highlighted books. `--verify` still needs no library, and neither does
  the JSON export.

- `--annotations-only --dry-run` wrote the file. The dry-run guard sat on the
  conversion route and the refresh route and not on this one. That covers the
  standard-output form as well: `-ao - --dry-run` now prints nothing, as
  `-ad -` already did.

- A Core Data date of `0` rendered as midnight on New Year's Day 2001. Apple
  writes `0` where it holds no date, so an export now leaves `modified` off an
  annotation whose modification date is `0`.

## [2.2.0] - 2026-08-28

### Added

- Annotations can be written as Markdown, one note per book, for Obsidian and
  anything else that reads Markdown with YAML frontmatter.
  `--annotations-format markdown` turns the detached export into a directory of
  notes, so `-ad` and `-ao` name a directory rather than a file. `-ae` stays
  JSON: Markdown inside a zip serves nobody.

  A vault note is a file the reader writes in too, which is the whole reason
  for putting them there, so the file is four regions with an owner each. The
  frontmatter belongs to the reader and to Obsidian, which rewrites it whenever
  anyone adds a tag. The highlights between the two markers belong to this
  tool. Everything below the end marker belongs to the reader again. Only the
  middle region is hashed and only it is ever replaced, so tagging a note or
  writing three paragraphs underneath never stops it being updated.

  A note edited *inside* the generated region is left exactly as it is, and its
  new highlights go to a `.md.new` beside it, so nobody has to choose between
  keeping their edits and getting their highlights. That sidecar is itself a
  note and gets the same treatment, so one partly merged by hand survives too.
  A file this tool did not write is never touched. A rerun that finds nothing
  new writes nothing at all, so a vault kept in git stays quiet.

  The frontmatter is written once and never rewritten, because it is the
  reader's from the moment it lands. A title corrected upstream will not
  propagate into an existing note; deleting the note is the refresh.

  Notes are named from the library named once with `.epub`, with the suffix
  swapped on the result, so a note and its epub always share a stem. Naming
  again with `.md` would clamp long titles against a budget two bytes larger:
  swept across title lengths 180-339, 101 of them came out different.

### Fixed

- A note the reader had edited could have its new highlights written nowhere
  while the run said otherwise. Five faults in one path, all found by a review
  of the branch and all reproduced before fixing:

  The sidecar was named `NAME.new.md`, which is a name a book can hold: a book
  titled "Foo.new" is written as `Foo.new.md`, exactly the sidecar name for a
  book titled "Foo", so one book's sidecar overwrote another book's note with
  the wrong content. It is now `NAME.md.new`, which no naming policy can
  produce and no later run will adopt as a note.

  The sidecar write's result was discarded, so a run whose sidecar path was
  occupied still reported "your new highlights are in a file beside it" with
  nothing written. A note carrying this tool's start marker but no end marker
  was reported as "not written by ibook2epub" and given no sidecar at all,
  though its digest was ours. Both sentences are now true of every file they
  count.

  An `OSError` from writing one note escaped the loop and ended the run with a
  traceback, where every other per-file failure in this project is logged and
  stepped over. A file that could not be *read* was counted as one this tool
  had never written, which it had no way of knowing.

- A FIFO or an oversized file in the vault is no longer read. `read_text` on a
  FIFO blocks until a writer appears, which froze the whole run; an 8 MB cap
  now bounds what is read back, mirroring the cap already on the JSON export.

- Naming happens once per run. `_embed_in_shelf` computed the assignment
  locally without returning it, so the markdown route named the library a
  second time — under a metadata policy that re-parses every package document,
  measured at 2.10x. The dispatch between the markdown and JSON exports also
  moves to one function from three call sites.

- The coverage gate reported a failure and passed the build.
  `coverage.results.should_fail_under` is `round(total, precision) <
  fail_under`, and the precision defaults to 0 — so 96.92% rounded to 97,
  compared equal to the threshold of 97, and exited 0 while the report printed
  "FAIL ... not reached" from its own unrounded comparison. Anything from 96.5%
  upwards slipped through, which is a real coverage regression reaching main
  behind a green tick. `precision = 2` makes the exit code agree with the
  report, and a test pins both the setting and the arithmetic behind it — by
  asking coverage to parse the file, since `precision = 2` under
  `[tool.coverage.html]` satisfies a substring check while leaving the gate
  exactly as broken as it was.

## [2.1.1] - 2026-08-28

### Fixed

- Documentation said an annotation's text "must not be reproduced" for a
  DRM-protected book. That was wrong, and it was the stated justification for
  keeping the `cfi` field. An annotation is the reader's own work -- their
  selection and their note -- and it was never inside the protected file:
  Apple keeps it in a separate database, and the licensing of a book says
  nothing about who owns the sentence somebody chose to mark. The behaviour
  was already correct, so nothing about the export changes; the claim is
  removed from the module, the schema and the README. The `cfi` field's real
  reason stands on its own, which is that a text fragment cannot tell apart a
  phrase appearing more than once in one book.

  A test now pins it. A DRM-protected book is skipped by the converter because
  its file cannot be opened, and its highlights still come out in full, with
  their text intact. There is also a guard asserting that nothing in the
  annotations module reaches for encryption state at all -- consulting it
  would be the first step towards withholding a reader's own writing from
  them, and the shelf of books that cannot be converted is exactly where
  taking the highlights with you matters most.

### Added

- A run says so when highlights had nowhere to go. `-ae` puts a book's
  highlights inside the book, which needs the book to be on the shelf; a book
  that was never converted has no archive to embed into, so its highlights
  were read out of Apple's database and then reached nothing at all, silently.
  With a DRM-protected book that is permanent, since no rerun will ever open
  its file. The warning names the count and the books, and recommends
  `--annotations-detached FILE` or `--annotations-only FILE`. It stays quiet
  when `-ad` is already in force, because then the highlights are in a file
  and there is nothing to report.

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

[2.3.1]: https://github.com/raeq/ibook2epub/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/raeq/ibook2epub/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/raeq/ibook2epub/compare/v2.1.1...v2.2.0
[2.1.1]: https://github.com/raeq/ibook2epub/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/raeq/ibook2epub/compare/v2.0.4...v2.1.0
[2.0.4]: https://github.com/raeq/ibook2epub/compare/v2.0.3...v2.0.4
[2.0.3]: https://github.com/raeq/ibook2epub/compare/v2.0.2...v2.0.3
[2.0.2]: https://github.com/raeq/ibook2epub/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/raeq/ibook2epub/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/raeq/ibook2epub/compare/v1.2.1...v2.0.0
[1.2.1]: https://github.com/raeq/ibook2epub/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/raeq/ibook2epub/releases/tag/v1.2.0
