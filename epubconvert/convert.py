"""
Convert Apple iBooks epub packages to zipped epub files

This module is designed to convert epub packages located in a specific directory
to epub files and exports them to another directory. It leverages `pathlib` and
`zipfile` libraries for handling directory navigation and archive processing.
The main steps include collecting directory names, creating the epub files, and
function orchestration through the main() function.
"""

import asyncio
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor
from functools import partial
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
DRY_RUN = False


async def create_zip_file_from_dir(source_dir: str, target_archive: str, task_id: int) -> int:
    """
    Create a ZIP file from the provided source directory.

    This function takes a source directory, its target archive path, and
    optionally, a list of exclusions. It returns the count of processed EPUB
    items. The ZIP file created is compliant with the EPUB specifications.

    :param task_id: The task ID for the current task.
    :param source_dir: The source directory containing the epub package files.
    :param target_archive: The output path for the resulting epub file.

    :return: The count of processed EPUB items.
    """
    if DRY_RUN:
        return 0

    source_dir = pathlib.Path(source_dir)
    epub_processed_count = 0

    # Use "run_in_executor" to run the ZIP compression in a separate thread
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        with ZipFile(target_archive, "w", ZIP_DEFLATED) as zf:
            future_writestr = loop.run_in_executor(
                executor,
                partial(
                    zf.writestr,
                    zinfo_or_arcname="mimetype",
                    compress_type=ZIP_STORED,
                    data="application/epub+zip",
                ),
            )
            await future_writestr

            for root, _, files in os.walk(source_dir):
                for filename in files:
                    if any(s in filename for s in ["mimetype", ".plist", "bookmarks"]):
                        app_logger.logger.trace(f"Skipped object: <{filename}>")
                        continue
                    file_path = os.path.join(root, filename)
                    name_in_archive = os.path.relpath(file_path, source_dir)
                    future_write = loop.run_in_executor(
                        executor,
                        partial(
                            zf.write,
                            file_path,
                            name_in_archive,
                            compresslevel=9,
                        )
                    )
                    await future_write
            await asyncio.sleep(0)
            app_logger.logger.info(f"Completed task {task_id} for: {target_archive}")
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
async def create_epub(filenames: list = None) -> int:
    """
    Create EPUB files from the provided filenames.

    This function takes a list of directory names as input and processes each
    one to create an EPUB file. It returns the count of successfully exported
    EPUB files.

    :param filenames: A list of directory names to be processed into EPUB files.

    :return: The count of successfully exported EPUB files.
    """

    tasks = []

    for i, filename in enumerate(filenames):
        output_zip_file = Path(f"{PATH_OUTPUT}{filename.lstrip().rstrip()}").as_posix()
        folder_to_zip = f"{PATH_INPUT}{filename}/"

        app_logger.logger.debug(f"Processing folder: {folder_to_zip}")

        task = asyncio.create_task(create_zip_file_from_dir(folder_to_zip, output_zip_file, i), name=f"Task {i}")
        tasks.append(task)
        app_logger.logger.debug(f"Created task {i} for: {folder_to_zip}")

    epub_processed_count = await asyncio.gather(*tasks)

    return len(epub_processed_count)


def ensure_directory_exists(source_dir, target_dir) -> bool:
    """
    Ensure the output directory exists, and if not, create it.

    :param source_dir: The source directory containing the epub package files.
    :param target_dir: The output path for the resulting epub file.

    :return: True if the directory exists or was created successfully.
    """

    # Firstly; ensure that the source directory exists,
    # otherwise return False
    try:
        if Path(source_dir).exists():
            pass
        else:
            app_logger.logger.critical(f"Source directory does not exist: {source_dir}")
    except Exception as e:
        app_logger.logger.exception(e)
        raise RuntimeError(e) from e

    # Secondly; ensure that the target directory exists,
    # otherwise create it
    try:
        if not Path(target_dir).exists():
            if not DRY_RUN:
                os.makedirs(target_dir)
                app_logger.logger.info(f"Created output directory: {target_dir}")
    except Exception as e:
        app_logger.logger.exception(e)
        raise RuntimeError(e) from e

    # only return True if both source _and_ target directories exist
    return Path(source_dir).exists() and Path(target_dir).exists()


@click.command()
@click.option(
    "-m",
    "--max-export-files",
    default=5,
    type=int,
    help="Override the maximum number of exported files, default=5.",
)
@click.option(
    "-o",
    "--output-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Path of the output directory.",
)
@click.option(
    "-s",
    "--source-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Path of the source directory.",
)
@click.option("--dry-run", "-d", is_flag=True, help="Run the program in dry-run mode.")
def main(
        max_export_files: int, output_dir: str, source_dir: str, dry_run: bool = False
) -> None:
    """
    Convert Apple iBooks epub packages to zipped epub files.

    This function collects directory names, creates EPUB files, and handles
    the process flow. It limits the total number of files processed based on
    the MAX_EXPORT_FILES global constant.
    """

    global MAX_EXPORT_FILES, PATH_OUTPUT, PATH_INPUT, DRY_RUN
    ctx = click.get_current_context()

    # Set the Dry Run flag
    app_logger.logger.info(f"Starting the convert application {ctx.params}")
    if dry_run:
        DRY_RUN = dry_run
        app_logger.logger.info(
            "User has chosen to run the program in dry-run mode. "
            "No file system modifications will be performed."
        )
    app_logger.logger.info(f"Examining: {PATH_INPUT}")

    # Override the MAX_EXPORT_FILES if needed
    if max_export_files is not None:
        MAX_EXPORT_FILES = max_export_files

    # Update the output directory if the user provides a value
    if output_dir is not None:
        PATH_OUTPUT = output_dir

    # Update the source directory if the user provides a value
    if source_dir is not None:
        PATH_INPUT = source_dir

    try:
        if not ensure_directory_exists(PATH_INPUT, PATH_OUTPUT):
            raise FileNotFoundError(
                "Error ensuring directories exist."
            )
    except FileNotFoundError as e:
        app_logger.logger.exception(e)
        raise RuntimeError(ctx.params) from e

    files = collect_directory_names()

    if MAX_EXPORT_FILES:
        shuffle(files)
        files = files[:MAX_EXPORT_FILES]
        app_logger.logger.info(
            f"Limiting activity to a maximum of {MAX_EXPORT_FILES} epub files."
        )
    else:
        app_logger.logger.info(
            f"All epub files up to a maximum of {len(files)} will be processed."
        )

    count = asyncio.run(create_epub(files))
    print(f"Exported {count} epub files to {PATH_OUTPUT}")
    app_logger.logger.debug("Ending the convert application.")


if __name__ == "__main__":
    main()
