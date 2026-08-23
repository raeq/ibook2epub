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
the standard library.

## Installation

```bash
pip install -e .
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
usage: ibook2epub [-h] [-m N] [-o OUTPUT_DIR] [-s SOURCE_DIR] [-d]
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

### Tracking what's been converted

The output directory is the source of truth. On each run, any book whose
output `.epub` already exists in the output directory is skipped and logged as
`Already exported, skipping: <name>`. Re-running the command is therefore
safe — it will only export books that have not yet been converted.

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
