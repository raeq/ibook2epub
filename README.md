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
[disarm](https://pypi.org/project/disarm/) for `--portable-names`.

## Installation

```bash
pip install -e .              # standard library only
pip install -e ".[portable]"  # adds --portable-names support
```

This installs the `ibook2epub` command. You can also run the package directly
with `python3 -m epubconvert`, without installing it.

## Performance

The time-consuming part of a conversion is compressing a book's files. Each
book is compressed in its own worker thread, so books are converted in parallel;
`zlib` releases the GIL while compressing, so this is genuine parallelism rather
than interleaving. The asyncio layer on top gathers the results and keeps one
book's failure from taking down the rest of the run.

On a cloud-backed library the bottleneck is usually **not** compression. A
measured run of 50 books took 80 seconds at 8% CPU — almost all of it waiting
on iCloud. Raising `--workers` above the CPU-derived default helps there, since
the extra threads spend their time blocked rather than competing.

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
usage: ibook2epub [-h] [-m N] [-o OUTPUT_DIR] [-s SOURCE_DIR] [-d] [--version]
                  [-f] [--match PATTERN] [-w N] [-p [MODE]] [--no-shuffle]
                  [-v] [-q] [--log-file PATH]

Convert Apple iBooks epub packages to zipped epub files.

options:
  -h, --help            show this help message and exit
  -m, --max-export-files N
                        Maximum number of epub files to export, default=5,
                        0=no limit.
  -o, --output-dir OUTPUT_DIR
                        Path of the output directory; created if it does not
                        exist.
  -s, --source-dir SOURCE_DIR
                        Path of the source directory containing *.epub/
                        packages. Defaults to whichever known iBooks location
                        holds books.
  -d, --dry-run         Report what would be exported without writing
                        anything.
  --version             show program's version number and exit
  -f, --force           Re-export books even if they are already in the output
                        directory.
  --match PATTERN       Only convert books whose name matches PATTERN. A
                        pattern without wildcards matches anywhere in the
                        name, so --match hobbit finds 'The Hobbit.epub';
                        otherwise it is a glob. Case-insensitive.
  -w, --workers N       Number of compression threads. The work is I/O bound
                        on a cloud library, so a value above the CPU count
                        often helps.
  -p, --portable-names [MODE]
                        Rewrite output names so they survive a copy to
                        Windows, exFAT or a Kindle. 'strip' (the default when
                        -p is given alone) removes the characters those
                        filesystems reject and needs no extra packages.
                        'romanize' also transliterates non-Latin titles and
                        folds accents when deciding whether a book is already
                        exported, and needs the 'disarm' extra. Either mode
                        renames books an earlier run already exported.
  --no-shuffle          Take the first N packages in sorted order instead of a
                        random selection when --max-export-files applies.
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
  and skipped rather than silently overwritten.

Without `-p`, names are used exactly as they appear in the source directory.

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
| `0`  | Success. Books skipped as already-exported, or skipped for a name collision, still count as success. |
| `1`  | At least one book failed to convert, or the output directory could not be created. |
| `2`  | `--portable-names` was requested but the `portable` extra is not installed. |
| `3`  | Another `ibook2epub` run already holds the output directory lock. |
| `130` | Interrupted with Ctrl-C. Finished books are intact; rerun to continue. |

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
pylint --fail-under=8.0 $(git ls-files '*.py')
```

CI runs all of these: lint and type checks once on Python 3.14, and the test
suite across Python 3.10 through 3.14.
