# ibook2epub

[![CI](https://github.com/raeq/ibook2epub/actions/workflows/ci.yml/badge.svg)](https://github.com/raeq/ibook2epub/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Export your **Apple Books** (formerly **iBooks**) library as standard **epub**
files, ready for Calibre, a Kindle, a Kobo, or any other e-reader.

On macOS, Books.app stores each book as a bare `*.epub/` *folder* — typically
under `~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents/` — rather
than as a real `.epub` archive, so books can't simply be copied out of the
library and opened elsewhere. `ibook2epub` batch-converts those package folders
into spec-valid epub files: a pure-Python command line tool with no
dependencies, safe to re-run, with optional Kindle/Windows-safe file naming.

## The Problem

Apple stores books in a proprietary format, which is a directory with a bunch of files in it. This is not a valid epub
file, and cannot be read by most epub readers.

## The Solution

This program converts the directory into a valid epub file. It does this by removing the `iTunesMetadata.plist` file,
and using the remaining files in the folder to create a valid epub file.

## Special Considerations

This program implements the following special considerations:

- Valid epub files are zip files.
- The first file must be named "mimetype",
  - it must not be compressed,
  - it must contain the string "application/epub+zip".
- The rest of the files must be compressed.

Apple's own bookkeeping is left out of the exported archive: `*.plist` files
(including `iTunesMetadata.plist`) and names beginning with `bookmarks`. Those
patterns are applied **only at the package root**, because that is the only
place Apple puts them — a chapter legitimately named `bookmarks.xhtml`, or a
`.plist` data asset under `OEBPS/`, is real content and is kept. `.DS_Store` is
dropped at any depth.

## Requirements

Python 3.10 or newer. There are no runtime dependencies — the tool uses only
the standard library. The optional `portable` extra adds
[disarm](https://pypi.org/project/disarm/), which is needed for
`--portable-names romanize` and for nothing else.

## Installation

```bash
pip install ibook2epub
```

That is the whole thing: no compiled wheel, no third-party package, nothing to
pull in at runtime. It installs an `ibook2epub` command.

### With the portable extra

```bash
pip install "ibook2epub[portable]"
```

The extra buys one mode. `-p` and `-p strip` remove the characters Windows,
exFAT and Kindles reject using the standard library alone, and are what most
people want. `-p romanize` additionally transliterates non-Latin titles
(`こころ.epub` becomes `kokoro.epub`) and folds accents when deciding whether
two files are the same book — that is the part `disarm` provides. Install the
base package unless you need it; the quotes around the argument matter in zsh.

### As an isolated tool

A command-line tool does not belong in the same environment as your projects:

```bash
pipx install ibook2epub
uv tool install ibook2epub
```

Add the extra the same way — `pipx install "ibook2epub[portable]"`.

### From source

```bash
git clone https://github.com/raeq/ibook2epub
cd ibook2epub
pip install -e ".[dev]"   # test and lint tooling, and the portable extra
```

You can also run the package without installing anything:

```bash
python3 -m epubconvert --help
```

### Upgrading and removing

```bash
pip install --upgrade ibook2epub
pip uninstall ibook2epub
```

Uninstalling leaves your exported books alone. The tool keeps no state outside
the output directory, so there is nothing else to clean up.

## Performance

Each book is compressed in its own worker thread, so books are converted in
parallel; `zlib` releases the GIL while compressing, so this is genuine
parallelism rather than interleaving. The asyncio layer on top gathers the
results and keeps one book's failure from taking down the rest of the run.

On a cloud-backed library the bottleneck is usually **not** compression. A
measured run of 50 books took 80 seconds at 8% CPU — almost all of it waiting
on iCloud. The pool is sized for that: it defaults to four times the CPU count,
capped at 64, rather than to the CPU-derived figure a thread pool normally
picks. Under a model of that iCloud stall, 14 threads took 19.2 s where 64 took
4.76 s, and the cost of the larger pool on a purely local library was 6%.

Two smaller choices follow from measurement rather than intuition. Members are
deflated at zlib's default level 6: level 9 measured 3.2× the CPU for 0.6% less
size on a real book. Members that are already entropy-coded — JPEG, PNG, fonts
— are stored rather than deflated, which measured 3.8× faster for 0.02% more
size. Both rules depend only on the filename, so exports stay byte-identical.

`--validate` is cheaper than it looks: the archive it re-reads is still in the
page cache, and inflating is far cheaper than deflating, so it measured about
7% on top of a run rather than doubling it.

## Safety

Each archive is built under a short temporary name in the output directory and
moved into place with `os.replace()` only once it is complete. An interrupted
run therefore never leaves a truncated `.epub` behind — which matters, because
the output directory is what the program uses to decide what has already been
done. The temporary name is deliberately *not* derived from the book's name: a
title already at the filesystem's 255-byte limit would overflow it once a
suffix was appended.

Exports are **byte-for-byte reproducible**. Zip members normally embed a
modification time, so converting the same book twice would produce different
bytes; entry timestamps and permissions are pinned instead. Re-exports
therefore hash identically, which lets backups deduplicate, stops `rsync`
re-copying unchanged books, and lets you compare two exports by checksum.

**Ctrl-C is a normal way to stop.** An interrupted run reports what it
finished, exits `130`, and leaves every completed book intact — rerun to carry
on. Books already being written are allowed to finish so their replace stays
atomic; queued ones are dropped.

Only one run at a time may write to a given output directory. A second
concurrent run exits `3` rather than duplicating work — useful when this is
scheduled from cron or launchd, which the rerun-safety invites. The lock file
records the holder's PID for diagnostics only: `flock` is released by the
kernel when a process dies, even on `SIGKILL`, so a lock file left behind by a
killed run is inert and needs no cleanup.

## Usage

### Command Line

```text
usage: ibook2epub [-h] [-m N] [-o OUTPUT_DIR] [-s SOURCE_DIR] [-d]
                  [--version] [-f] [--match PATTERN] [-w N] [-p [MODE]]
                  [--list] [--json] [--refresh] [--skip-incomplete]
                  [--name-by {passthrough,author-title}]
                  [--on-collision {skip,suffix}] [--min-free MB]
                  [--no-copy-through] [--covers] [--validate] [--epubcheck]
                  [--verify] [--no-shuffle] [-v] [-q] [--log-file PATH]

Convert Apple iBooks epub packages to zipped epub files.

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  -w, --workers N       Number of compression threads (default: 4x the CPU
                        count, capped at 64). The work blocks on iCloud
                        rather than on the CPU, so raising this well past the
                        CPU count is what helps; 48-64 is reasonable for a
                        cloud library.

Choosing books:
  Which books this run considers.

  -m, --max-export-files N
                        Maximum number of epub files to export, default=5,
                        0=no limit.
  -s, --source-dir SOURCE_DIR
                        Path of the source directory containing *.epub/
                        packages. Defaults to whichever known iBooks location
                        holds books.
  --match PATTERN       Only convert books whose name matches PATTERN. A
                        pattern without wildcards matches anywhere in the
                        name, so --match hobbit finds 'The Hobbit.epub';
                        otherwise it is a glob. Case-insensitive.
  --no-shuffle          Take the first N books still needing export, in
                        sorted order, instead of a random selection when
                        --max-export-files applies.

Naming and output:
  Where books go and what they are called.

  -o, --output-dir OUTPUT_DIR
                        Path of the output directory; created if it does not
                        exist.
  -p, --portable-names [MODE]
                        Rewrite output names so they survive a copy to
                        Windows, exFAT or a Kindle. 'strip' (the default when
                        -p is given alone) removes the characters those
                        filesystems reject and needs no extra packages.
                        'romanize' also transliterates non-Latin titles and
                        folds accents when deciding whether a book is already
                        exported, and needs the 'disarm' extra. Either mode
                        renames books an earlier run already exported.
  --name-by {passthrough,author-title}
                        Where an output name comes from: 'passthrough' uses
                        the package folder name, 'author-title' uses the
                        book's own dc:title and dc:creator to write 'Author -
                        Title.epub'. Composes with --portable-names. Adopting
                        it renames every book already exported; the old files
                        are reported as orphans, not deleted.
  --on-collision {skip,suffix}
                        What to do when two books want the same output name:
                        'skip' exports only the first, 'suffix' keeps both. A
                        suffixed book is marked with a digest of its own
                        dc:identifier, so adding another book later does not
                        rename it; books whose identifier is missing or
                        shared fall back to ' (2)', which does move.
  --no-copy-through     Do not copy already-valid .epub files and .pdf files
                        to the output directory. They are copied by default,
                        because a real library holds both Apple's package
                        folders and books that arrived already zipped, and
                        exporting only the first produces half a shelf.
  --covers              Also write each book's cover image beside its epub
                        file.

Deciding what to do:
  What the run does, or reports without doing.

  -d, --dry-run         Report what would be exported without writing
                        anything.
  -f, --force           Re-export books even if they are already in the
                        output directory.
  --list                List every *.epub/ package with its status (pending,
                        exported, collision, drm, incomplete, orphan) and
                        exit without converting anything. Anything in the
                        source that is not a package is counted, not listed.
  --json                With --list, emit machine-readable JSON instead of a
                        table.
  --refresh             Re-export a book when its source directory is newer
                        than the exported file. Compares directory
                        timestamps, so a book re-downloaded in place may not
                        be noticed; use --force for that.
  --skip-incomplete     Skip books iCloud has not downloaded, which would
                        otherwise export as empty files. Requires walking
                        every package, which is slow on a cloud library, so
                        it is off by default.

Checking the result:
  Verifying archives and protecting the volume.

  --min-free MB         Stop before the output volume drops below this many
                        megabytes, default=128. 0 disables the check. Useful
                        when writing to an SD card or a Kindle.
  --validate            Check each archive before it is moved into place: zip
                        integrity, the mimetype entry, and that every file
                        the package document lists is really present. A book
                        that fails is not written, so it is retried on the
                        next run.
  --epubcheck           Also run the external 'epubcheck' tool on each
                        archive. Implies --validate and requires epubcheck on
                        PATH.
  --verify              Check the archives already in the output directory
                        and report any that are damaged, then exit without
                        converting anything.

Output and logging:
  How much the run says, and where.

  -v, --verbose         Increase log verbosity; -v for debug, -vv for trace.
  -q, --quiet           Only log warnings and errors.
  --log-file PATH       Also write log records to this file.
```

The source directory is searched recursively, so books stored in subfolders are
found too. The search does not descend into a `*.epub/` package once it has been
found.

### Examples

Convert up to 5 books from the default iCloud iBooks location to `~/Books/`:

```bash
ibook2epub
```

Convert from a custom source directory to a custom output directory, capped at 100 files:

```bash
ibook2epub \
    -s "$HOME/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents/" \
    -o "$HOME/Downloads/epubs/" \
    -m 100
```

Convert *every* book (no cap). The output folder is created if it does not exist:

```bash
ibook2epub -s "$HOME/iBooks/" -o "$HOME/Downloads/epubs/" -m 0
```

Dry run — list what *would* happen without writing any files:

```bash
ibook2epub -s "$HOME/iBooks/" -o "$HOME/Downloads/epubs/" -d
```

Verbose run, also written to a log file:

```bash
ibook2epub -s "$HOME/iBooks/" -o "$HOME/Downloads/epubs/" -vv --log-file run.log
```

### Selecting which books to convert

`--max-export-files` caps the books actually written, not the books looked at,
so repeated runs work through the library a batch at a time whatever order they
take. The capped subset is chosen at random by default. Pass `--no-shuffle` to
take books in sorted order instead, which makes a run reproducible without
stalling it.

Each run reports how much is left, so the default batching reads as progress
rather than a limit. The figure counts only books that can still be
exported — a DRM-protected or undownloaded book is reported on its own line
instead, so the count can actually reach zero:

```text
Exported 5 epub file(s) (412 member files) to /Users/you/Books, skipped 37. 212 remaining; rerun to continue.
```

Convert one book, or a handful, with `--match`. A pattern with no wildcard
matches anywhere in the name; anything else is treated as a glob. Matching is
case-insensitive either way:

```bash
ibook2epub --match hobbit -m 0      # The Hobbit.epub, Hobbit Notes.epub
ibook2epub --match "The Lord*" -m 0 # anchored glob
```

### Re-exporting

A book already in the output directory is skipped. Pass `-f` / `--force` to
export it again anyway — useful after re-downloading a book, or when you
suspect an output is damaged:

```bash
ibook2epub --match dune --force -m 0
```

### Portable filenames (`-p`)

macOS happily stores a book called `Sapiens: A Brief History.epub`, but the
colon is illegal on Windows and exFAT, so copying that file to an SD card, a
Kindle or a Windows share fails.

How often that bites depends entirely on the library. Measured against a real
2,805-book iBooks library, **1.3% of titles contain a character from
`<>:"/\|?*`** and 1.6% are changed by sanitisation — a few dozen books, not a
large fraction. If you only ever read from `~/Books` on APFS, `-p` buys you
close to nothing. If you copy to a Kindle or an SD card, it is the difference
between those books arriving and failing.

`-p` / `--portable-names` rewrites output names so they survive that copy. It
has two modes.

**`-p` (or `-p strip`) — standard library, nearly loss-free.** Removes the
characters those filesystems reject, escapes reserved device names like `CON`,
and clamps the result to 255 bytes. **The script is preserved**: `こころ.epub`
stays `こころ.epub`, because APFS, ext4, NTFS and exFAT all store it correctly.

```bash
ibook2epub -o /Volumes/KINDLE/documents/ -m 0 -p
```

```text
Sapiens: A Brief History.epub  ->  Sapiens A Brief History.epub
The Hobbit.epub  +  THE HOBBIT.epub  ->  one export, not two
こころ.epub                     ->  こころ.epub   (unchanged)
```

**`-p romanize` — needs the `portable` extra.** Everything `strip` does, plus
transliteration and accent-insensitive duplicate detection:

```bash
pip install "ibook2epub[portable]"
ibook2epub -o /Volumes/KINDLE/documents/ -m 0 -p romanize
```

```text
こころ.epub        ->  kokoro.epub        (lossy)
Düne.epub + Dune.epub  ->  one export, not two
```

Spaces are preserved in both modes — they are legal everywhere, so replacing
them would be gratuitous churn. Two things to know before turning either on:

- **It renames books an earlier run already exported.** A book whose name
  changes will be exported again under its new name, and the old file is left
  behind. Start with `-d` to preview.
- **Distinct books can want the same name.** `Vol 1:2` and `Vol 1?2` both
  become `Vol 1 2`. The first wins; the rest are reported as name collisions
  and skipped rather than silently overwritten. `--on-collision suffix` keeps
  them all instead — see below.

Without `-p`, names are used exactly as they appear in the source directory.

### Naming books after their author

Apple names a package directory after the title, so an exported shelf sorts by
title. A Kobo, a Kindle and a file manager all show what the filename says, and
nothing but renaming every file by hand will make that shelf sort by author.

`--name-by author-title` builds the name from the book's own `dc:title` and
`dc:creator` instead:

```bash
ibook2epub -o ~/Books --name-by author-title
```

```text
A Wizard of Earthsea.epub  ->  Le Guin, Ursula K. - A Wizard of Earthsea.epub
Clock Dance.epub           ->  Anne Tyler - Clock Dance.epub
```

Those two lines show the one thing to understand before turning it on. **The
shelf will mix two conventions.** A sort name is read from either EPUB dialect
— the EPUB2 `opf:file-as` attribute or the EPUB3 `<meta refines="#id"
property="file-as">` element — and 26% of books in a real 2,805-book library
supply neither. There is no safe way to invent one: the raw `dc:creator` text
is sometimes already inverted, so a rule that flips `Anne Tyler` into `Tyler,
Anne` would also flip `Patterson, James` into `James, Patterson`. The
publisher's sort name is used when it exists and the creator is used verbatim
otherwise.

The rest of the behaviour, measured on that same library:

- 2,713 of 2,729 books get an author prefix, and 1,609 of them sort by
  surname. The rest declare no creator and are named by title alone.
- The run says how many books were named without an author, and how many
  kept their folder name because the package document gave no title.
- A book that cannot name itself keeps its directory name — one whose
  package document cannot be read, and one whose `dc:title` is a
  placeholder. Some converters write the literal string `none` into every
  metadata field; 31 books in a real library do, and the folder name Apple
  set is both more informative and unique per book.
- Up to two contributors are named in full. Beyond that the list collapses:
  `Peralta, Samuel et al. - The Galaxy Chronicles.epub`. Publishers often put a
  book's whole contributor list in one field, and a twelve-author name is not a
  usable filename even when it fits.
- Names are sanitised and clamped to 255 bytes, and neither half of the name
  can squeeze the other out. A long author list is collapsed before the title
  is touched; a book whose `dc:title` is its entire jacket blurb still keeps
  its author prefix.
- Reading every package document to build the names costs about three seconds
  over the whole library. Without `--name-by author-title` no package document
  is read at all, so a plain rerun stays free.

It composes with `-p`, which decides how a name is cleaned rather than where it
comes from.

**Adopting it renames every book already exported.** The old files stay where
they are — this tool never deletes anything — and each is reported as an
orphan so you can see the full list before deciding:

```bash
ibook2epub -o ~/Books --name-by author-title --list
```

### When two books want the same name

`--on-collision skip`, the default, exports the first and reports the rest.
`--on-collision suffix` keeps them all, and how it tells them apart matters if
you run this on a schedule.

A book that has to share a name is marked with a short digest of its own
`dc:identifier`:

```text
Cook, Glen - The Tyranny of the Night [b17c15bc].epub
Cook, Glen - The Tyranny of the Night [d5c52684].epub
```

The marker describes the book, not its position in the group, so adding a
fourth copy of something next month does not rename the three already on the
shelf. Numbering did: a book that sorted earlier pushed every later member of
its group down by one, and the old files stayed behind under names nothing
claimed any more.

Two cases still fall back to ` (2)`, and both say so rather than pretending:

- **No usable identifier.** Some books carry a placeholder where the identifier
  should be. In a real 2,805-book library the literal string `none` is the
  identifier for 92 books, and `ISBN` for three.
- **Two books, one identifier.** Publishers reuse them. Four different novels
  in that library share a series ISBN, and six unrelated technical books share
  one converter's template UUID.

Measured on that library with `--name-by author-title --on-collision suffix`:
2,692 names untouched, 78 marked with a digest, 10 of those needing a number as
well, and 29 falling back to a number outright. Every one of the 2,799 names
came out distinct, and two independent runs produced identical results.

One thing the marker cannot fix: a book *entering* a collision gains its marker,
which is a rename. That happens once, when the second copy shows up, instead of
every time the group changes.

### Tracking what's been converted

The output directory is the source of truth — there is no state file. On each
run, any book already present in the output directory is skipped and logged as
`Already exported, skipping: <name>`. Re-running the command is therefore
safe: it only exports books that have not yet been converted.

With `-p` this still holds. A book's identity is derived from its *sanitised*
filename, so the identity of completed work can be recomputed by reading the
output directory back off disk.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Every book that could be converted was. |
| `1` | At least one book failed to convert. |
| `2` | The command line was wrong: unknown or contradictory flags. |
| `3` | Another run holds the output lock. Retry later. |
| `4` | The source directory does not exist, or no library was found. |
| `5` | The output directory could not be created, opened or found. |
| `6` | A required extra or external tool is not installed. |
| `7` | --verify found at least one damaged archive. |
| `130` | Stopped with Ctrl-C. Finished books are intact. |

Each code means exactly one thing, so a scheduled run can act on the status
without reading the message: `3` is worth retrying later, `6` needs something
installed, `4` and `5` are paths to fix, `7` means a book on the shelf is
broken. `2` keeps its universal meaning of a bad command line.

`2` is not specific, and a script cannot tell its causes apart from the code
alone. It covers a usage error from `argparse` (an unknown or malformed flag,
contradictory flags), a source directory that does not exist, `--portable-names
romanize` without the `portable` extra, `--epubcheck` without `epubcheck` on
PATH, and `--verify` pointed at an output directory that is not there. All of
them are "this run was asked for something it cannot do", and all of them print
the reason on stderr. If a script needs to distinguish them, match the message
rather than the code.

`1` also covers a run that could not proceed at all — for example when the
output volume is below `--min-free`. Nothing is counted as *failed* in that
case, because nothing was attempted.

A book that fails to convert is logged and the run continues with the rest.

Name collisions do **not** change the exit code. If you run this from a script
or a cron job and need to know that books were skipped, check the summary line
or watch for `Name collision, skipping:` in the log — a run that skips books
for that reason still exits `0`.

## Development

```bash
pip install -e ".[dev]"

pytest                              # tests
ruff check epubconvert tests        # lint
ruff format epubconvert tests       # format
mypy                                # type check (strict)
pylint epubconvert tests
```

CI runs all of these: lint and type checks once on Python 3.14, and the test
suite across Python 3.10 through 3.14.
