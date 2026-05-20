# ibooks2epub

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

## Performance

There are two operations which are time consuming:

- Compressing the files from the source directory.
- Writing the files to the output directory.

The program uses a thread pool to compress the files, and a thread pool to write the files.

## Usage

### Command Line

```text
Usage: convert.py [OPTIONS]

  Convert Apple iBooks epub packages to zipped epub files.

  This function collects directory names, creates EPUB files, and handles the
  process flow. It limits the total number of files processed based on the
  MAX_EXPORT_FILES global constant.

Options:
  -m, --max-export-files INTEGER  Override the maximum number of exported
                                  files, default=5, 0=no limit.
  -o, --output-dir DIRECTORY      Path of the output directory.
  -s, --source-dir DIRECTORY      Path of the source directory.
  -d, --dry-run                   Run the program in dry-run mode.
  --help                          Show this message and exit.

```

### Examples

Convert up to 5 books from the default iCloud iBooks location to `~/Books/`:

```bash
python3 epubconvert/convert.py
```

Convert from a custom source directory to a custom output directory, capped at 100 files:

```bash
python3 epubconvert/convert.py \
    -s "$HOME/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents/" \
    -o "$HOME/Downloads/epubs/" \
    -m 100
```

Convert *every* book (no cap), to an output folder you've already created:

```bash
mkdir -p "$HOME/Downloads/epubs"
python3 epubconvert/convert.py -s "$HOME/iBooks/" -o "$HOME/Downloads/epubs/" -m 0
```

Dry run — list what *would* happen without writing any files:

```bash
python3 epubconvert/convert.py -s "$HOME/iBooks/" -o "$HOME/Downloads/epubs/" -d
```

### Tracking what's been converted

The output directory is the source of truth. On each run, any book whose
output `.epub` already exists in the output directory is skipped and logged as
`Already exported, skipping: <name>`. Re-running the command is therefore
safe — it will only export books that have not yet been converted.
