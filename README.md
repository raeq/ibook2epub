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
brew install raeq/tap/ibook2epub    # macOS
pip install ibook2epub              # anywhere
```

That is the whole thing: no compiled wheel, no third-party package, nothing to
pull in at runtime. It installs an `ibook2epub` command.

The Homebrew formula lives in [raeq/homebrew-tap][tap] rather than
homebrew-core, and brings its own Python, so it does not care which one you
have. It is the same distribution PyPI serves.

[tap]: https://github.com/raeq/homebrew-tap

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
atomic, even if Ctrl-C is pressed again while they do; queued ones are dropped.

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
                  [--list] [--json] [--refresh] [--skip-incomplete] [-an]
                  [-ae] [-ad [FILE]] [-ao [FILE]]
                  [--annotations-format {json,markdown}] [-ar]
                  [--library-export [FILE]] [--library-format {csv,json}]
                  [--no-isbn] [--unknown-shelf SHELF]
                  [--name-by {passthrough,author-title}]
                  [--on-collision {skip,suffix}] [--min-free MB]
                  [--no-copy-through] [--covers] [--validate] [--epubcheck]
                  [--verify] [--no-shuffle] [-v] [-q] [--log-file PATH]

Convert Apple iBooks epub packages to zipped epub files.

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  -w, --workers N       Number of worker threads, for converting books and
                        for copying PDFs and already-zipped books (default:
                        4x the CPU count, capped at 64). The work blocks on
                        iCloud rather than on the CPU, so raising this well
                        past the CPU count is what helps; 48-64 is reasonable
                        for a cloud library.

Choosing books:
  Which books this run considers.

  -m, --max-export-files N
                        Maximum number of packages to convert; 0=unlimited,
                        default=5. Files copied through are not counted.
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
                        shared fall back to ' (2)', which each keeps by the
                        source its archive names.
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
                        output directory. With --library-export, replace the
                        file it names.
  --list                List every *.epub/ package, and every PDF or zipped
                        epub file copied through, with its status (pending,
                        exported, collision, drm, incomplete, orphan, copy,
                        copied) and exit without converting anything.
                        Anything else in the source is counted, not listed.
  --json                With --list, emit machine-readable JSON instead of a
                        table.
  --refresh             Re-export a book when its source directory is newer
                        than the exported file. Compares directory
                        timestamps, so a book re-downloaded in place may not
                        be noticed; use --force for that.
  --skip-incomplete     Skip books iCloud has not downloaded, rather than
                        exporting packages as empty files or downloading PDFs
                        and already-zipped books. Requires walking every
                        package, which is slow on a cloud library, so it is
                        off by default.

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

Your highlights and notes:
  Taking annotations out of Apple Books. Any of these needs Full Disk
  Access on macOS. None is the default: a conversion touches Apple's
  container only when asked to.

  -an, --annotations-none
                        Do not touch annotations. The default, stated so a
                        script can be explicit about it.
  -ae, --annotations-embedded
                        Write each book's annotations into it at META-
                        INF/annotations.json, so they travel with the book to
                        whatever reads it next.
  -ad, --annotations-detached [FILE]
                        Write one file for the whole library. Survives losing
                        the books, which is what taking them with you means.
                        A rerun merges into it rather than replacing it. With
                        no FILE, or with '-', it goes to standard output and
                        everything else goes to standard error.
  -ao, --annotations-only [FILE]
                        Write the detached file and nothing else: no
                        conversion, no shelf. For somebody who wants their
                        highlights and not three thousand epub files. With no
                        FILE, or with '-', it goes to standard output.
  --annotations-format {json,markdown}
                        Shape of the detached export. 'json' is the
                        standards-shaped document the schema describes.
                        'markdown' writes one note per book into a directory,
                        for an Obsidian or Logseq vault; with it, the
                        argument to -ad and -ao names a DIRECTORY rather than
                        a file. Embedded annotations (-ae) are always JSON.
  -ar, --annotations-refresh
                        Update annotations for books already converted, and
                        convert nothing. A library is converted once and
                        annotated for years afterwards; this picks up new
                        highlights without rewriting every archive.

Your library:
  Taking the catalogue out of Apple Books: what you own, who wrote it, when
  you got it, and the collections you sorted it onto. Needs Full Disk
  Access on macOS.

  --library-export [FILE]
                        Write the library as a file and convert nothing. A
                        Goodreads-format CSV by default, which The StoryGraph
                        imports directly; see --library-format. A catalogue
                        rather than a reading history: Apple keeps titles,
                        authors, collections and acquisition dates, and
                        little else. Composes with --annotations-only. An
                        existing FILE is left alone unless --force is given.
                        With no FILE, or with '-', it goes to standard
                        output.
  --library-format {csv,json}
                        Shape of the library export. 'csv', the default, is
                        the Goodreads export format. 'json' is the canonical
                        record the schema describes, carrying every
                        collection and every date the database holds.
  --no-isbn             Do not open any book's package document. That is
                        where the ISBN comes from, and also the author's sort
                        name and, under --name-by author-title, the name the
                        book would have on the shelf, so those are left out
                        too. A tracker matches a row on its ISBN, so this
                        trades a matched import for speed: the read costs one
                        per book, where the highlights cost one per annotated
                        book.
  --unknown-shelf SHELF
                        What the CSV's Exclusive Shelf column says for a book
                        the database says nothing about: 'to-read',
                        'currently-reading' or 'read'. By default the column
                        is left blank rather than claiming 'to-read' for
                        every book merely bought. Some importers require the
                        column; this is how you choose the claim made on your
                        behalf.
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

A dry run refuses what the real run would refuse, with the same exit code: a
shelf it cannot write, a volume below `--min-free`, a highlights file or vault
it cannot write, a `--library-export` file it may not write. It does not open
the notes already in a vault, though, and says so: a note the real run leaves
alone and exits `1` for — a file ibook2epub did not write at a note's name, one
it cannot read, an edited `.md.new` — is found only by the real run.

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
Exported 5 epub file(s) (412 member files) to /Users/you/Books, skipped 37. 212 remaining. 212 held back by --max-export-files: rerun to continue, or pass -m 0 to convert everything.
```

The advice depends on why books remain. Only books the cap held back are
pointed at `-m 0`. A book that failed is reported as failed, and the error
lines above the summary say why; rerunning would fail it again. Books an
interrupt or a full disk left unattempted are sent back to rerun.

Convert one book, or a handful, with `--match`. A pattern with no wildcard
matches anywhere in the name; anything else is treated as a glob. Matching is
case-insensitive either way:

```bash
ibook2epub --match hobbit -m 0      # The Hobbit.epub, Hobbit Notes.epub
ibook2epub --match "The Lord*" -m 0 # anchored glob
```

`--match` narrows the PDFs and already-zipped books copied through too, by the
same rule, so converting one book does not download every copy in an iCloud
library. `--max-export-files` does not: it caps the books converted, and a
copy is not one, so every matching file is copied whatever `-m` says.

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

"First" is the first in sorted order, except that a book whose exact name is
already a file on the shelf keeps it. That matters for names that differ only
in case, which macOS treats as one file: `b/dune.epub`, exported on its own,
keeps `dune.epub` when `a/Dune.epub` arrives, and the newcomer is the collision,
or `Dune (2).epub` under `suffix`. A book renamed only by case, with no
namesake left in the library, still finds its archive under the old spelling:
the two identifiers are compared, and when neither book declares one the name
is trusted.

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

A book also takes its marked name when the file under its plain name holds a
different book: an edition since deleted from the library, or one a `--match`
left out of the run. That file is left alone, and reported as an orphan once
no book in the library claims it. Under `--name-by author-title` the planner
can tell, because it compares the file's `dc:identifier` with the book's.

Two cases still fall back to ` (2)`, and both say so rather than pretending:

- **No usable identifier.** Some books carry a placeholder where the identifier
  should be. In a real 2,805-book library the literal string `none` is the
  identifier for 92 books, and `ISBN` for three.
- **Two books, one identifier.** Publishers reuse them. Four different novels
  in that library share a series ISBN, and six unrelated technical books share
  one converter's template UUID.

A numbered book keeps its number when a book before it leaves the library,
rather than taking the freed name and being written again. It is found by the
source its archive names (below), or else by its identifier, which is read for
a name with numbered files on the shelf even when the book is named from its
folder; a book with no usable identifier and an archive from an earlier
release keeps its number only when it alone wants the name and nothing holds
the plain name, since nothing else can say whose the numbered file is.

Measured on that library with `--name-by author-title --on-collision suffix`:
2,692 names untouched, 78 marked with a digest, 10 of those needing a number as
well, and 29 falling back to a number outright. Every one of the 2,799 names
came out distinct, and two independent runs produced identical results.

One thing the marker cannot fix: a book *entering* a collision gains its marker,
which is a rename. That happens once, when the second copy shows up, instead of
every time the group changes. A book *leaving* one keeps the marked archive it
has on the shelf rather than being written again under the plain name.

A PDF or an already-zipped book that is copied rather than converted competes
for its name too, after every package: a package `a/Book.epub/` and a zipped
`b/Book.epub` want one file, and the package gets it. The copy is reported as a
name collision, or under `--on-collision suffix` is copied as `Book (2).epub`;
a copy has no digest marker, so it is always numbered. A copy already on the
shelf before the package arrived keeps its file, in either mode, and is not
copied again: the package is reported as a collision rather than as exported
from the other book's file, or under `--on-collision suffix` is written beside
it, as `Book (2).epub` when it has no digest marker either.

A copy is recognised on the shelf without opening it: a copy keeps its
source's modification time, and the file under its name is its copy when the
size and the time match. A file of another size, or older than the source, is
another book's, so a different PDF of the same name and size replacing a
deleted one is copied rather than taken as already there. Copies made by
releases before this one carry the time they were written, which is later than
their source's; such a file is still recognised by its size alone, so
upgrading copies nothing again.

#### How a book knows its own file

Every archive this tool writes names the book it was written from, in the zip
archive's comment:

```text
ibook2epub/1 src=fc47dad3
```

The digest is of the book's path in the library -- `a/Dune.epub` and
`b/Dune.epub` are two books -- with case folded, so renaming a folder only by
case changes nothing. Readers ignore the comment, `mimetype` stays first, and
the book validates as before. A refresh with `-ae -ar` keeps it, and writes it
into an archive from an earlier release that it rewrites anyway. Nothing is
rewritten just to add one, and a PDF or zipped book copied through is left
byte for byte.

The name says which book a file is *for*; the marker says which book it is
*from*, and that wins. Two books that declare no usable identifier, or one
between them, each keep the file their marker names, whatever order they sort
in and whichever of them leaves the library and comes back. A file whose
marker names a book no longer in the library is never taken for another book,
never written over by `--force`, `--refresh` or `-ae -ar`, and is listed as an
orphan. Identifiers that both books declare and that differ still say two
books, whatever the marker says, as does a file that declares an identifier
where the book declares none: a book moved or deleted and another added at the
same path is not written over the first one's archive. Where neither can say
-- the first declared no identifier -- the newcomer at its path is taken for
the book written from it, as the marker names a path, not a book.

Archives from an earlier release name nothing. Where two books that nothing
tells apart want such a file, neither is given it, and the run says so:

```text
cannot tell these books apart: a/Dune.epub, b/Dune.epub: Dune.epub names no
source, and nothing they declare tells them apart; none was written.
```

Under `--on-collision suffix` each is written under a name of its own, with its
marker, and the old file is listed as an orphan once they are ("each was given
a name of its own"); in skip mode both are collisions, and the file, which may
be either one's only archive, is not listed as an orphan. A book that declares
no usable identifier is never given a file that declares one. A book alone in
wanting an unmarked file of its name keeps it, so upgrading exports nothing
again -- which leaves what the marker cannot cover. A file with no marker, from
an earlier release or copied through, whose book has since left the library,
is judged as before: a book alone in wanting it takes it by its name wherever
the identifiers cannot say otherwise -- the file declares none, or the one the
book declares, shared with the book that left -- and `--force` or `--refresh`
can write over it.

A book moved to another folder in the library, or a library pointed at with
`-s` from another level, has another source, and its archive names one no book
has. Where the book declares a usable identifier that no other book in the
library is known to declare, and the archive declares it too, the archive is
still the book's: it is reported exported, is not listed as an orphan, and a
quiet rerun writes nothing. The archive keeps naming the old folder until it
is written for another reason -- `--refresh`, `--force` or `-ae -ar` -- when
it is written with the new one; nothing is rewritten just to update it.
(Under the policies that name a book from its folder only the identifiers the
run read are known, so another book declaring the same one is not always seen.)

The cost of that is one case the marker gave up: a book that shared its
identifier with a book since deleted, and has no file of its own -- a
collision in skip mode, or one not yet written -- looks exactly like that book
moved, and is taken for it.

Two things cost a rewrite, never a book. A moved book that declares no usable
identifier, or one another book declares, cannot be told from a deleted
book's namesake: its old archive is listed as an orphan and the book is written
again, or in skip mode reported as a collision until the old file is moved
away. And under the naming policies that name a book from its folder, a book
reported as exported is still trusted by its name when no other book wants it:
checking would read every archive on every run. The marker is read where two
books want a name, where the identifiers are read anyway, and before anything
is written over; a rerun over a shelf of books with identifiers of their own
reads nothing more than it did.

### Taking your highlights with you

Apple keeps your highlights and notes in its own database, not in the books. So
converting a library gets you the books and leaves the reading behind. Five
flags cover three separate decisions: whether to gather them at all, where they
should go, and whether to do that without converting anything.

```bash
ibook2epub -ao ~/highlights.json          # just the highlights, no conversion
ibook2epub -ae                            # convert, and put them inside each book
ibook2epub -ad ~/highlights.json          # convert, and write one file as well
ibook2epub -ae -ar                        # refresh books already on the shelf
ibook2epub -ao                            # to stdout, for piping
```

| Flag | |
|---|---|
| `-an`, `--annotations-none` | the default, stated so a script can be explicit |
| `-ae`, `--annotations-embedded` | into each book, at `META-INF/annotations.json` |
| `-ad`, `--annotations-detached [FILE]` | one file for the whole library |
| `-ao`, `--annotations-only [FILE]` | that file and nothing else — no conversion |
| `-ar`, `--annotations-refresh` | go back over books converted by an earlier run |

`-ae` puts the highlights in as each book is written, which is why it costs
almost nothing: the archive is built once, not built and then rebuilt. A book
that was already on the shelf is not rewritten by a run that had nothing else
to do with it, so bringing an older shelf up to date is what `-ar` is for.
Each book it refreshes is rebuilt beside the original, so `-ar` stops at the
`--min-free` floor as a conversion does, and exits `1` if the floor stopped it
or a book could not be refreshed. It copies nothing, so it never opens a
zipped book iCloud has evicted, and it takes `--skip-incomplete` and `-w` as a
conversion does.

`-ad` and `-ao` write to standard output when given no filename, so
`ibook2epub -ao | jq '.annotations[].text'` works. Everything else then goes to
standard error, because a run summary in the middle of the JSON would make it
unparsable.

A file given to `-ad` or `-ao` is judged before anything is read or
converted, in a dry run too. A directory that is not there or cannot be
written (a read-only volume is named as one), a file already there that is not
an annotation export, or a name the run itself makes a directory (`-ad Books
-o Books`) stops the run with exit code 5 and says why, rather than after
every book is converted. A directory already there is named as one: give a
file, or add `--annotations-format markdown` to write a note per book into it.

**On macOS this needs Full Disk Access.** The databases live inside Apple's
container. Without it, `-ao` and `-ar`, where the highlights are the whole run,
stop with exit code 8 and a message saying so, not a traceback. `-ae` and `-ad`
log the same message, `Could not read annotations`, and convert the books
anyway, since the books are the point; the exit code then describes the
conversion, so a script that needs the highlights should watch for that line.

#### Straight into an Obsidian vault

`--annotations-format markdown` turns the detached export into one note per
book, with YAML frontmatter and the highlights as blockquotes. With it, `-ad`
and `-ao` name a **directory** rather than a file. It is created if it is not
there; one that is a file, sits under a file, or cannot be written or created
is refused before anything is converted, in a dry run too, with exit code 5.

```bash
ibook2epub -ao ~/vault/Books --annotations-format markdown   # just the notes
ibook2epub -ad ~/vault/Books --annotations-format markdown   # convert too
```

```markdown
---
title: "Leviathan Wakes"
author: "James S.A. Corey"
identifier: "urn:isbn:9780316129084"
isbn: "9780316129084"
category: book
tags: [books]
source: ibook2epub
---
<!-- ibook2epub sha256=351e6dffa048ee4a book=3a90a3e2 src=57ce2d19 -->
# Leviathan Wakes
*James S.A. Corey*

## Chapter Fifteen: Holden

> Summary roadside justice

**Note:** the bit where Miller stops being a cop.
<!-- ibook2epub end — your notes below this line are never modified -->
```

`-ae` stays JSON: Markdown inside a zip serves nobody.

#### Your notes are yours

A vault note is a file you write in too, which is the whole point of putting
them there. So the file has four parts, and the tool owns exactly one of them:

| | |
|---|---|
| the frontmatter, and anything else above the marker line | yours, and Obsidian's — add tags, aliases and links freely |
| the marker line | the tool's |
| the highlights between the markers | the tool's |
| everything below the end marker | yours |

Only the middle part is ever rewritten. Tag a note, add an alias, write three
paragraphs underneath — the next run still adds your new highlights and leaves
all of it alone. An editor that trims trailing spaces on save is not an edit.
Edit *inside* the highlights and the tool stops touching that
note entirely, putting the new ones in a `.md.new` beside it so you never have
to choose between keeping your edits and getting your highlights. Edit the
`.md.new` too, part way through merging it, and it is left alone as well: the
run names it and exits `1` until you merge it into the note or remove it.

The middle part mirrors Books: a highlight you delete there leaves the note on
the next run, as it leaves each book's embedded set. Only the JSON file of
`-ad` or `-ao` keeps highlights deleted in Books (see
[Re-running](#re-running)). Anything of your own written below the end marker
is never touched either way.

A file at a note's path that the tool did not write, or one it cannot read,
is never touched either. That book's highlights then reach no file at all, so
the run names the file and exits `1`; move it aside and rerun. Under
`--on-collision suffix` a book passes over a file it did not write and gets a
numbered note beside it instead.

The marker line also names the book the note is of, as a digest of Apple's id
for it, and the file that book is read from, as a digest of its name, so a
note is never rewritten with another book's highlights, whatever a later run
names it. Which note a book gets depends on your library, never on
which books have highlights today. Two books that want one note — `Dune.epub`
and `Dune.pdf` — are a name collision, settled the same way whether you have
highlighted one of them or both: the note is the first one's, the other is
named in the summary and gets none, and `--on-collision suffix` numbers it,
`Dune (2).md`. A note already written for a book stays that book's, so a book
gaining its first highlight, or losing its last, never moves another book's
note. Notes written before the marker named its book are still recognised:
each stays with the book holding every highlight in it, and is tagged the next
time it changes. A note that says nothing of whose it is — one holding
highlights no book has any more, or only some of one book's — goes with its
name, or the numbered name `--on-collision suffix` gave it, when only one book
wants that name. When two do, neither is handed it: without
`--on-collision suffix` the run names it and exits `1`, and with it each book
gets a numbered note. So does a note two books each hold every highlight of,
whether or not either has a note of its own already.

A note is another book's when the book it names is one Books still knows —
in your library, highlighted or not — when its frontmatter names another
edition's identifier, however that line is quoted, or when it names another
file than the one the book is read from. Such a note is left alone: without
`--on-collision suffix` the run names it and exits `1`, and with it the book
gets a numbered note of its own. A book you remove from Books and add again
gets a new id from Apple; its note, which names its file, is still its own,
and is re-tagged the next time it changes.

A book whose name changes — you adopt `--name-by author-title`, say, or its
metadata is corrected — takes its note with it: the note is renamed to the
new name, with everything of yours in it. If it cannot be, because something
is already at the new name or the note's `.md.new` is still beside it, the
run names it and exits `1` until you move it yourself; no second note is
started beside it. So does a note written before notes named their book
that lies at the name of another book in your library, highlighted or not:
only its highlights say it is not that book's, and it is left where it is.

A rerun with nothing new writes nothing at all, so a vault in git stays quiet.

One consequence worth knowing: the frontmatter is written once and never
rewritten, because it is yours from the moment it lands. If a title was wrong
and you fix it upstream, the note keeps the old one — delete the note and it
comes back correct.

#### Why two places

The [W3C EPUB Annotations work][anno] defines both and prefers neither.
Detached annotations "can be shared independently of the publication"; embedded
ones are "always available to users". They answer different questions: a
library-wide file survives losing the books, an embedded set travels with the
book to whatever reads it next. `-ae -ad FILE` does both.

#### What an annotation looks like

```json
{
  "id": "644526A1-C87C-447A-AC0B-72F9092668FC",
  "book": {
    "title": "Leviathan Wakes",
    "author": "James S. A. Corey",
    "identifier": "urn:isbn:9781449340360",
    "source": "Leviathan Wakes.epub",
    "filename": "Corey, James S.A. - Leviathan Wakes.epub"
  },
  "text": "Summary roadside justice",
  "chapter": "Chapter Fifteen: Holden",
  "href": "OEBPS/Text/dummy_split_083.html",
  "locator": ":~:text=Summary%20roadside%20justice",
  "cfi": "epubcfi(/6/46[dummy_split_083.html]!/4,/80/2/1:25,/82/2/1:25)",
  "created": "2018-12-25T22:44:28Z"
}
```

Each one carries enough of its book to be cited without it, which is the
requirement that work opens with. `source` is where the book came from;
`filename` is what it is called on your shelf, which differs whenever a naming
policy renames.

`identifier` is the book's own `dc:identifier`, the one the package document
names through its `unique-identifier`. Every other field there is a name that
can change — a title Apple recorded, a directory name, a filename this tool
chose — so it is the only key that still matches an annotation to its book
after a rename, a re-download, or a move to somebody else's reader. Books that
declare nothing usable leave it out; `none` is the identifier for 92 books in
my library, which is to say it identifies none of them.

It is written canonically, because a key only works if the same book always
produces the same string, and books do not cooperate. My 2,805 packages write
one ISBN six ways — bare, `urn:isbn:`, `URN:ISBN:`, hyphenated, `ISBN 978...`
and `urn:ean:` — and one UUID two ways. A verifiable ISBN becomes `urn:isbn:`
and thirteen digits, an ISBN-10 becomes the ISBN-13 meaning the same book, and
a UUID becomes lowercase `urn:uuid:`. That rewrote 2,328 of 2,703.

Recognition is by check digit, never by counting digits. 68 identifiers in that
library are ten or thirteen digits and fail their check, so a rule that went by
length would have relabelled every one of them as an ISBN it is not. Anything
unrecognised is passed through exactly as declared, and whenever
canonicalising changed something the original is kept alongside as
`declaredIdentifier`, so nothing the book said is lost.

The schema is [`epubconvert/collect/annotations.schema.json`][schema],
shipped inside the package.

#### The locator, and why the CFI is kept anyway

`locator` is a [URL text fragment][frag] — the locator the W3C work is
converging on, because it is web-native and any browser can act on it. A long
highlight is quoted as `textStart,textEnd` rather than whole: a
249-character fragment has to match a rendered DOM exactly, and will not.

Apple records an EPUB CFI, and that is kept verbatim in `cfi` rather than used
as the locator. The group has explicitly ruled CFI out — *"which will rule out
epubcfi (which, b.t.w., is bound to XHTML...)"* — but it is the only locator
that still points at the right place when the highlighted text appears more
than once in a book, which a text fragment cannot tell apart.

#### Your highlights from books you cannot convert

A DRM-protected book is skipped by the converter, because its file cannot be
opened. Its highlights still come out in full.

An annotation is not part of the publication. It is your selection and your
note, and Apple keeps it in a separate database from the book. Nothing about a
book's licensing makes the sentence you chose to mark somebody else's to
withhold. This is the case where taking them with you matters most, since the
book is the one thing that cannot come along — so `-ao` on a shelf of locked
books still gets you everything you wrote.

There is a catch worth knowing, and the tool now says so rather than leaving
you to find out. `-ae` puts a book's highlights *inside the book*, which needs
the book to be on the shelf. A book that was never converted has no archive to
put them in, so those highlights reach nothing:

```text
10 annotation(s) from 4 book(s) reached no file: Blindsight.epub, Dune.epub,
Neuromancer.epub, and 1 more. Those books are not on the shelf, so there was
nothing to embed them in -- a DRM-protected book can never be converted, and
its highlights are the only part of it you can keep. Run again with
--annotations-detached FILE to write them to a file of their own, or
--annotations-only FILE to do that without converting anything.
```

With DRM this is permanent: no rerun will ever produce an archive to embed
into. So a locked library wants `-ae -ad ~/highlights.json`, or `-ao` on its
own. The warning stays quiet when `-ad` is already in force, because then the
highlights are in a file and there is nothing to report.

A zipped book or a PDF copied through is on the shelf, but copied byte for
byte, so `-ae` and `-ae -ar` embed nothing in it either. They say so in a
warning of their own, which is quiet under `-ad` too:

```text
2 annotation(s) from 1 book(s) copied through unchanged were not embedded
(copies are byte-for-byte): Beta.epub. Use -ad FILE or -ao FILE.
```

`-ae -ar` converts nothing and cannot tell whether a copy is on the shelf, so
it says those books were "not converted by ibook2epub" instead.

Apple records some highlights against no book at all. They are embedded
nowhere, and only a detached file carries them, so `-ae` and `-ae -ar` say that
too, and are quiet under `-ad`:

```text
2 highlight(s) Apple recorded against no book were not embedded; use -ad FILE or -ao FILE.
```

**This is ahead of the specification, not conformant to it.** That draft still
has sections marked T.B.D., and its dependency on text fragments has not yet
landed in HTML. The shape here is meant to become conformant without the data
being gathered again.

#### Re-running

What a rerun does with a highlight you have deleted in Books depends on where
the highlights go, and only one destination keeps it:

| Destination | On a rerun |
|---|---|
| the `-ad` / `-ao` JSON file | merged into: a highlight deleted in Books is **kept** |
| the set embedded in each book (`-ae`, `-ar`) | replaced: mirrors what Books holds now |
| a vault note (`--annotations-format markdown`) | its highlights are replaced: mirrors what Books holds now |

So the JSON file is the one to keep if you want everything you ever
highlighted. A book that arrives carrying its own `META-INF/annotations.json`
has it replaced by this run's set, not merged with it. One exception on the
mirroring side: a book with no highlight left in Books is not touched at all,
so its embedded set and its note keep the last highlights they had, rather
than being emptied.

The JSON file merges rather than replaces. Annotations are matched on the UUID
Apple gives them, so a file you have been adding to is added to again:

```text
3 added, 1 updated, 214 unchanged, 2 kept (no longer in Books)
```

An annotation you deleted in Books is **kept**: taking them with you is the
point, and losing one because Apple lost it would defeat that. An export
written by a different version of this tool is regenerated rather than trusted,
because the locator is ahead of a moving draft and an old entry may not say
what a current one would.

A file at that path that the merge cannot account for in full is left exactly
as it is, and the run stops with exit code `5` and names it: one that is not
an export, an annotation with no id, two annotations sharing an id, or a
key the tool does not write, at the top of the file, on an annotation or on
an annotation's book. Merging any of those would drop
something without a word, so move the file aside or fix it and rerun.

[anno]: https://w3c.github.io/epub-specs/epub34/annotations/
[frag]: https://developer.mozilla.org/en-US/docs/Web/URI/Fragment/Text_fragments
[schema]: epubconvert/collect/annotations.schema.json

### Taking your library with you

Apple gives no way to get a catalogue out. `--library-export` reads the same
database the highlights come from and writes what you own: title, author,
when each book arrived, and the collections you sorted it onto. By default it
is a **Goodreads-format CSV**, which [The StoryGraph][storygraph] and most
other trackers import directly; `--library-format json` writes the canonical
record instead.

```bash
ibook2epub --library-export ~/library.csv                       # for a tracker
ibook2epub --library-export ~/library.json --library-format json
ibook2epub --library-export ~/library.csv -ao ~/highlights.json --force
ibook2epub --library-export --dry-run                           # just the estimate
```

The third one writes both files, and takes `--force` because the catalogue is
a snapshot: without it a second run stops at the file already there, and stops
before the highlights too, since both destinations are judged before either is
written. The catalogue's directory is judged as the highlights file's is: one
that is not there, that the run may not write into or search, or that is on a
read-only volume stops the run with exit code 5 before the library is read, in
a dry run too. A symlink is judged where it leads, where the catalogue is
written; so is a name too long for its filesystem, and one that is the vault
the same run makes (`-ao out/vault --library-export out`), or a directory
above it.

It converts nothing, like `-ao`, and the two compose: the reader who wants
their catalogue out is the reader who wants their highlights out. It needs no
library on disk and no output directory, only Full Disk Access on macOS.

#### It is a catalogue, not a reading history

Worth knowing before you import it. Measured against my 3,620-book library,
Apple's database holds titles, authors, collections and acquisition dates for
every book, and almost nothing else: the rating is zero on every row, the page
count on all but one, the year and the finished flag are null throughout, and
eleven books carry a finish date. The CSV promises what the database holds.
Every other column is filled when it is there and left blank when it is not.

**The exclusive shelf is evidence or nothing.** A finish date, a finished flag
or reaching the end of a book means `read`; progress short of the end means
`currently-reading`. Nothing else is claimed. The obvious rule -- a book with
no evidence is `to-read` -- would have told a tracker I intend to read 3,389
books I merely bought. If your importer insists on the column,
`--unknown-shelf to-read` fills the blanks, so the claim made on your behalf
is one you chose.

Collections become the `Bookshelves` column, minus the ones Apple fills by
itself -- Library, Downloaded, Books, PDFs, Audiobooks, My Samples -- which
say where a book is stored rather than where you shelved it. `Want to Read` is
one of yours and comes through as a shelf. The JSON carries every collection.

#### Most rows will import unmatched, and the tool says so first

A tracker matches a row on its ISBN, and most books do not have one: 1,108 of
my 2,805 epubs carry an ISBN, 1,448 carry a UUID, and the PDFs and audiobooks
carry nothing a tracker can read. So about 30% of rows import matched and the
rest need adding by hand. The run says exactly that, before writing anything:

```text
1108 of 3620 book(s) carry an ISBN a tracker can match on. The other 2512
will import unmatched and need adding by hand.
```

`--dry-run` gives you the number and writes nothing, which is where to look
before a 3,620-row import into a service where undoing one is manual. Reading
the ISBN means opening every book's package document, one read per book where
the highlights cost one per *annotated* book; `--no-isbn` skips it when speed
matters more than matching. The package document is also where the author's
sort name comes from, and under `--name-by author-title` the book's name on
the shelf, so those go too.

An ISBN is judged by its check digit, never by its prefix: 68 identifiers in
that library are ten or thirteen digits that fail their check, and a tracker
handed one of those matches the wrong book. They are left out of the ISBN
columns and out of the count.

#### What a row looks like

The header is Goodreads' own, verbatim and in its order, because that is what
the importers key on. ISBNs are written the way Goodreads writes them,
`="9781449340360"`, which is a spreadsheet's spelling of "keep this as text".
Dates are `YYYY/MM/DD`. A title that starts with `=`, `+`, `-` or `@` gains a
leading apostrophe, because a title is input and a cell starting with `=` runs
when the file is opened in Excel. Control characters are escaped, as they are
in every name this tool prints.

The JSON export is described by [`epubconvert/export/library.schema.json`][libschema]
and looks like this:

```json
{
  "title": "Leviathan Wakes",
  "author": "James S. A. Corey",
  "authorSort": "Corey, James S. A.",
  "identifier": "urn:isbn:9781449340360",
  "source": "Leviathan Wakes.epub",
  "assetId": "0A1B2C3D-4E5F-6789-ABCD-EF0123456789",
  "added": "2016-03-05T18:22:41Z",
  "lastOpened": "2019-08-14T07:03:12Z",
  "finished": "2018-12-25T22:44:28Z",
  "collections": ["Books", "Downloaded", "Library", "Space opera"],
  "shelf": "read"
}
```

`assetId` is Apple's key for the book, unique within the library and the same
one the annotation export records, so the two files join. `identifier` is the
book's own, canonicalised exactly as the annotation export canonicalises it.

#### Re-running

Unlike the annotation file, the library export is a snapshot rather than a
file that is merged into: there is nothing in a CSV to merge on, and a
Goodreads export at the same path would carry the very same header, so the
tool cannot tell its own file from yours. An existing file is left alone with
exit code `5`; pass `--force` to replace it.

[storygraph]: https://app.thestorygraph.com/import-export
[libschema]: epubconvert/export/library.schema.json

### Tracking what's been converted

The output directory is the source of truth — there is no state file. On each
run, any book already present in the output directory is skipped and logged as
`Already exported, skipping: <name>`. Re-running the command is therefore
safe: it only exports books that have not yet been converted. Where a name
alone cannot say which book a file holds, the source each archive names does
([How a book knows its own file](#how-a-book-knows-its-own-file)).

With `-p` this still holds. A book's identity is derived from its *sanitised*
filename, so the identity of completed work can be recomputed by reading the
output directory back off disk.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Every book that could be converted was. |
| `1` | At least one book failed to convert. |
| `2` | The command line was wrong: unknown, malformed or contradictory flags. |
| `3` | Another run holds the output lock. Worth retrying later. |
| `4` | The source directory does not exist, or no library was found. |
| `5` | The output directory could not be created, opened or found. |
| `6` | A required extra or external tool is not installed. |
| `7` | `--verify` found at least one damaged archive. |
| `8` | macOS refused access to the Books library; the terminal needs Full Disk Access. |
| `130` | Stopped with Ctrl-C. Finished books are intact; rerun to continue. |

Each code means exactly one thing, so a scheduled run can act on the status
without reading the message: `3` is worth retrying later, `6` needs something
installed, `4` and `5` are paths to fix, `7` means a book on the shelf is
broken, and `8` needs Full Disk Access granted to the terminal (System Settings
> Privacy & Security > Full Disk Access). `2` keeps its universal meaning of a
bad command line, and only that: an unknown or malformed flag, or flags that
contradict each other.

`4` and `8` both mean Apple's library could not be read, for different
reasons: `4` that there is none where it was looked for, `8` that macOS would
not let this run look. Every failure prints its reason on stderr as well.

`7` is for damage a reader can trip on. What the specification asks against
but readers open anyway — an extra field in the `mimetype` member's local
header, which `zip` writes unless given `-X` — is said on stderr and changes
no exit code: a book copied through keeps its bytes, so no rerun would mend it.
A member's name is the one its header holds, or the UTF-8 name a Unicode
Path extra field (0x7075) gives it, but never `mimetype` by a field alone, and
a field never renames `mimetype`. A field Python 3.14's zipfile refuses —
shorter than its version and CRC, or vouching for the name in bytes that are
not UTF-8 — is damage on every Python.

`5` also covers a report that could not be written. When `--list`, `--verify`
or a run's summary cannot be written to standard output — a full disk behind
`> report.txt`, say — the run says so on stderr and exits `5` rather than `0`,
so a script does not take a lost report for a clean run. The same goes for a
standard output closed before the run started (`>&-`), and for the document
`-ao -` or `--library-export -` writes there. A reader that closes the pipe
early, as `| head` does, has seen what it wanted and changes no exit code, and
so does a standard error that is closed. A report goes out as UTF-8 whatever
the locale or `PYTHONIOENCODING` says, as the documents do. Any other code a
run has earned leads: `--verify` still exits `7` for a damaged shelf. A
`--log-file` that cannot be opened, or stops taking writes partway, is said
once on stderr and changes no exit code: it is a copy of what the console
shows.

`1` also covers a run that could not proceed at all — for example when the
output volume is below `--min-free`. Nothing is counted as *failed* in that
case, because nothing was attempted.

A book that fails to convert is logged and the run continues with the rest.

When more than one applies, a run exits with the first of these: `130` if it
was stopped with Ctrl-C, `1` if a book failed or the run could not proceed,
then the code for wherever the highlights were to go — `5` for a destination
it could not write, for example. The summary line describes the books, so the
code that leads is the one that agrees with it; the other reason is on stderr.

Name collisions do **not** change the exit code. If you run this from a script
or a cron job and need to know that books were skipped, check the summary line
or watch for `Name collision, skipping:` in the log — a run that skips books
for that reason still exits `0`. A vault run (`--annotations-format markdown`)
has no note name for a book that lost a collision either, and names the ones
whose highlights it therefore left out: watch for `lost a name collision`.

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
