"""
Convert Apple iBooks epub packages to zipped epub files

This module is designed to convert epub packages located in a specific directory
to epub files and exports them to another directory. It leverages `pathlib` and
`zipfile` libraries for handling directory navigation and archive processing.
The main steps include collecting directory names, creating the epub files, and
function orchestration through the main() function.
"""

import os
import pathlib
import platform
from pathlib import Path
from random import shuffle
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

import click

import app_logger


# Get the current username and directories
USER = os.getenv("USER")
PATH_INPUT = (
    f"/Users/{USER}/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents/"
)
PATH_OUTPUT = rf"/Users/{USER}/Books/"
MAX_EXPORT_FILES = 5


def create_zip_file_from_dir(source_dir: str, target_archive: str) -> int:
    """
    Create a ZIP file from the provided source directory.

    This function takes a source directory, its target archive path, and
    optionally, a list of exclusions. It returns the count of processed EPUB
    items. The ZIP file created is compliant with the EPUB specifications.

    :param source_dir: The source directory containing the epub package files.
    :param target_archive: The output path for the resulting epub file.

    :return: The count of processed EPUB items.
    """

    source_dir = pathlib.Path(source_dir)
    epub_processed_count = 0

    # Create a ZipFile object
    with ZipFile(target_archive, 'w', ZIP_DEFLATED) as zf:
        # First, add the mimetype
        zf.writestr(zinfo_or_arcname="mimetype", compress_type=ZIP_STORED, data="application/epub+zip")

        for root, _, files in os.walk(source_dir):
            for filename in files:
                if any(s in filename for s in ["mimetype", ".plist", "bookmarks"]):
                    app_logger.logger.trace(f"Skipped oobject: <{filename}>")
                    continue

                file_path = os.path.join(root, filename)
                name_in_archive = os.path.relpath(file_path, source_dir)

                zf.write(file_path, name_in_archive, compresslevel=9)
                app_logger.logger.trace(f"Adding object to archive: <{name_in_archive}>")
                epub_processed_count += 1

    return epub_processed_count


def collect_directory_names() -> list:
    """
    Collect all directory names containing ".epub/" and return them in a list.

    This function iterates through the input directory, filtering out directories
    containing ".epub/" and returns them in a sorted list.

    :return: A sorted list of directory names.
    """
    fn = []

    try:
        for root, dirs, files in os.walk(PATH_INPUT):
            for d in dirs:
                if d.endswith(".epub"):
                    fn.append(d)
    except Exception as e:
        app_logger.logger.error(e)
    else:
        app_logger.logger.debug(
            f"Will process the following folders: {list(enumerate(fn))}"
        )

    return sorted(fn)


#   Function to generate new epub files
def create_epub(filenames: list = None) -> int:
    """
    Create EPUB files from the provided filenames.

    This function takes a list of directory names as input and processes each
    one to create an EPUB file. It returns the count of successfully exported
    EPUB files.

    :param filenames: A list of directory names to be processed into EPUB files.

    :return: The count of successfully exported EPUB files.
    """

    exported: int = 0

    for i, filename in enumerate(filenames):
        output_zip_file = Path(f"{PATH_OUTPUT}{filename.lstrip().rstrip()}").as_posix()
        folder_to_zip = f"{PATH_INPUT}{filename}/"

        app_logger.logger.debug(f"Processing folder: {folder_to_zip}")

        try:
            create_zip_file_from_dir(folder_to_zip, output_zip_file)
        except Exception as e:
            app_logger.logger.warning(
                f"File <{filename}> #{i + 1} of {len(filenames)} was not processed correctly."
            )
            app_logger.logger.exception(e)
        else:
            app_logger.logger.info(
                f"File <{filename}> #{i + 1} of {len(filenames)} has been processed successfully."
            )
            exported += 1

    return exported


def ensure_directory_exists(source_dir, target_dir) -> bool:
    """
    Ensure the output directory exists, and if not, create it.

    :param source_dir: The source directory containing the epub package files.
    :param target_dir: The output path for the resulting epub file.

    :return: True if the directory exists or was created successfully.
    """

    source_dir_exists: bool = False
    target_dir_exists: bool = False

    # Firstly; ensure that the source directory exists,
    # otherwise return False
    try:
        if source_dir_exists := os.path.exists(source_dir):
            pass
        else:
            app_logger.logger.critical(f"Source directory does not exist: {source_dir}")
    except Exception as e:
        app_logger.logger.exception(e)
        raise RuntimeError(e) from e

    # Secondly; ensure that the target directory exists,
    # otherwise create it
    try:
        if not os.path.exists(target_dir) and source_dir_exists:
            os.makedirs(target_dir)
            app_logger.logger.info(f"Created output directory: {target_dir}")
    except Exception as e:
        app_logger.logger.exception(e)
        raise RuntimeError(e) from e
    else:
        target_dir_exists = os.path.exists(target_dir)

    # only return True if both source _and_ target directories exist
    return target_dir_exists and source_dir_exists


@click.command()
@click.option(
    "-m",
    "--max-export-files",
    default=5,
    type=int,
    help="Override the maximum number of exported files.",
)
@click.option(
    "-o",
    "--output-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Path of the output directory.",
)
def main(max_export_files: int, output_dir: str):
    """
    Convert Apple iBooks epub packages to zipped epub files.

    This function collects directory names, creates EPUB files, and handles
    the process flow. It limits the total number of files processed based on
    the MAX_EXPORT_FILES global constant.
    """

    global MAX_EXPORT_FILES, PATH_OUTPUT

    # Set up logging configuration
    app_logger.logger.info(f"Starting the convert application, examining: {PATH_INPUT}")
    # Ensure program is running on a Mac
    if platform.system() != "Darwin":
        raise RuntimeError("This program will only work on mcOS")

    # Override the MAX_EXPORT_FILES if needed
    if max_export_files is not None:
        MAX_EXPORT_FILES = max_export_files

    # Update the output directory if the user provides a value
    if output_dir is not None:
        PATH_OUTPUT = output_dir

    if not ensure_directory_exists(PATH_INPUT, PATH_OUTPUT):
        raise FileNotFoundError(f"The input directory does not exist. <{PATH_INPUT}>")

    files = collect_directory_names()

    if MAX_EXPORT_FILES:
        shuffle(files)
        files = files[:MAX_EXPORT_FILES]
        app_logger.logger.info(
            f"Limiting activity to a maximum of {MAX_EXPORT_FILES} epub files."
        )
    else:
        app_logger.logger.info(
            f"All epub files up to a maximum of {len(files)+1} will be processed."
        )

    count = create_epub(files)
    print(f"Exported {count} epub files to {PATH_OUTPUT}")
    app_logger.logger.debug("Ending the convert application")


if __name__ == "__main__":
    main()
