# ibook2epub

Use this program to convert iBooks epub directories to epub format.

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

Apple's own bookkeeping is left out of the exported archive: any `*.plist`
file (including `iTunesMetadata.plist`), files whose names begin with
`bookmarks`, and `.DS_Store`.

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

## Safety

Each archive is built under a temporary `.part` name and moved into place with
`os.replace()` only once it is complete. An interrupted run therefore never
leaves a truncated `.epub` in the output directory — which matters, because the
output directory is what the program uses to decide what has already been done.

## Usage

### Command Line

```text
usage: ibook2epub [-h] [-m N] [-o OUTPUT_DIR] [-s SOURCE_DIR] [-d] [-p]
                  [--no-shuffle] [-v] [-q] [--log-file PATH]

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
                        packages.
  -d, --dry-run         Report what would be exported without writing
                        anything.
  -p, --portable-names  Rewrite output names so they survive a copy to
                        Windows, exFAT or a Kindle, and treat case/accent
                        variants of a title as the same book. Romanizes non-
                        Latin titles, and renames files an earlier run already
                        exported. Needs the 'disarm' extra.
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

When `--max-export-files` applies, the capped subset is chosen at *random* by
default, so repeated runs work through the library a batch at a time. Pass
`--no-shuffle` to take books in sorted order instead, which makes a run
reproducible.

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

`-p` / `--portable-names` rewrites output names so they survive that copy, and
treats case and accent variants of a title as the same book:

```bash
pip install "ibook2epub[portable]"
ibook2epub -s "$HOME/iBooks/" -o /Volumes/KINDLE/documents/ -m 0 -p
```

```text
Sapiens: A Brief History.epub  ->  Sapiens A Brief History.epub
The Hobbit.epub  +  THE HOBBIT.epub  ->  one export, not two
```

Spaces are preserved — they are legal everywhere, so replacing them would be
gratuitous churn. Three things to know before turning this on:

- **Non-Latin titles are romanized.** `こころ.epub` becomes `kokoro.epub`. This
  is lossy, and it is why the flag is opt-in rather than the default.
- **It renames books an earlier run already exported.** A book whose name
  changes under sanitisation will be exported again under its new name, and
  the old file is left behind. Start with `-d` to preview.
- **Distinct books can want the same name.** `Vol 1:2` and `Vol 1?2` both
  become `Vol 1 2`. The first wins; the rest are reported as name collisions
  and skipped rather than silently overwritten.

Without `-p`, names are used exactly as they appear in the source directory and
no third-party package is involved.

### Tracking what's been converted

The output directory is the source of truth — there is no state file. On each
run, any book already present in the output directory is skipped and logged as
`Already exported, skipping: <name>`. Re-running the command is therefore
safe: it only exports books that have not yet been converted.

With `-p` this still holds. A book's identity is derived from its *sanitised*
filename, so the identity of completed work can be recomputed by reading the
output directory back off disk.

### Exit codes

`0` on success, `1` if any book failed to convert. A book that fails is logged
and the rest of the run continues.

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
