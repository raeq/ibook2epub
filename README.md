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

```
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
