"""
Deciding what to do with each package, before anything is written.

Planning is kept separate from exporting so that ``--list`` can render the
same decisions the exporter acts on, and so the rules that make a rerun safe
live in one place: identity is recomputed from the filenames already in the
output directory, which is why this tool needs no state file.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Container, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..collect.identifiers import usable_identifier
from ..collect.package import ValidationError, read_archive_package, read_package_dir
from ..collect.source import inspect_package
from ..export.naming import disambiguator, filesystem_key
from ..export.provenance import source_of
from ..utils.app_logger import logger
from ..utils.display import printable
from ..utils.opf import Package
from ..utils.policy import Assignment, NamingPolicy
from ..utils.spec import PACKAGE_SUFFIX
from .claims import (
    MAX_SUFFIX,
    Claims,
    Keeping,
    Wanting,
    claim_order,
    kept_numbers,
    lost_to,
    marked,
    shelf_names,
    suffixed,
)
from .holders import Unopened, declares_one, holds_another_book, written_for
from .placing import Shelf, place, read_shelf
from .telling import tell_apart

#: What the planner can decide about a package. These strings are a public
#: contract, not an internal detail: ``--list`` names them all in its help text
#: and ``--list --json`` emits them verbatim. Typing them keeps a mistyped
#: comparison from passing the type checker.
Status = Literal[
    "pending", "exported", "collision", "drm", "incomplete", "orphan", "copy", "copied"
]

#: Decision statuses. Each constant's value is what a user sees.
PENDING: Status = "pending"
EXPORTED: Status = "exported"
COLLISION: Status = "collision"
DRM: Status = "drm"
INCOMPLETE: Status = "incomplete"

#: Not a decision about a book in the library at all: an archive on the shelf
#: that no book in the library claims. Reported, never acted on.
ORPHAN: Status = "orphan"

#: What ``--list`` says of a PDF or an already-zipped book the run takes along
#: unchanged: to be copied, or already on the shelf.
COPY: Status = "copy"
COPIED: Status = "copied"

#: Why a book iCloud has evicted is skipped.
NOT_DOWNLOADED = "not downloaded from iCloud"

#: What a run says of the books it left unnamed rather than download them.
NOT_NAMED = (
    "%d book(s) not downloaded from iCloud could not be named without "
    "downloading them; a copy of one already on the shelf is counted as an "
    "orphan."
)

#: How ``--on-collision`` may be set.
CollisionMode = Literal["skip", "suffix"]
SKIP: CollisionMode = "skip"
SUFFIX: CollisionMode = "suffix"
COLLISION_MODES = (SKIP, SUFFIX)

#: Every status, in the order --list documents them. Exported so the CLI help
#: can name them without restating the set: Status's own docstring calls that
#: help part of the contract, and a sixth status would otherwise leave it wrong
#: with nothing to catch it.
STATUSES: tuple[Status, ...] = (
    PENDING,
    EXPORTED,
    COLLISION,
    DRM,
    INCOMPLETE,
    ORPHAN,
    COPY,
    COPIED,
)


@dataclass(frozen=True)
class PlanOptions:
    """How the planner should treat the packages it is given."""

    force: bool = False
    refresh: bool = False
    check_incomplete: bool = False
    on_collision: CollisionMode = SKIP
    #: The library's root, which each archive's provenance marker is
    #: relative to (:func:`~epubconvert.export.provenance.source_of`). None
    #: writes and reads no marker.
    library: Path | None = None


@dataclass
class Decision:
    """What the planner decided about one package."""

    package: Path
    status: Status
    target: Path | None = None
    reason: str | None = None
    #: Where the plan placed a book it then found it could not write, such
    #: as a DRM-protected one: a vault note is named after it, as ``-ao``
    #: names it.
    placed: Path | None = None

    @property
    def display_name(self) -> str:
        """
        The name to show a person, which is the name that will be written.

        Two fields hold a name and the type did not say which one a reader
        wants, so the listing printed the source package and showed nothing
        about a policy that renames. Answered here rather than at each
        renderer, because there is more than one renderer and they disagreed.

        A book that lost a collision has no target; there the source name is
        both the only thing available and the right thing to show, since the
        question is which book lost.
        """
        return (self.target or self.package).name


def _metadata_of(package: Path, wanted: bool) -> Package | None:
    """
    Read a package document, but only for a policy that asked for it.

    :param package: The package directory.
    :param wanted: Whether the naming policy needs the metadata at all.

    :return: The parsed package document, or None if it was not wanted or
        could not be read.
    """
    if not wanted:
        return None
    try:
        return read_package_dir(package)
    except (ValidationError, OSError):
        # One package in a surveyed 2,805-book library has no container.xml.
        # A book that cannot describe itself still deserves a name.
        return None


def copy_name_opens_file(source: Path, policy: NamingPolicy) -> bool:
    """
    Report whether naming *source* for the shelf means opening it.

    Only an already-zipped epub under a metadata policy is named from its own
    contents. Asked on its own so ``--skip-incomplete`` can leave an evicted
    file unopened: opening it is the download the flag exists to avoid.

    :param source: The file to be copied.
    :param policy: The naming policy this run is using.

    :return: True if :func:`copy_target_name` would read the file.
    """
    return bool(getattr(policy, "needs_metadata", False)) and (
        source.suffix.lower() == PACKAGE_SUFFIX
    )


def copy_target_name(source: Path, policy: NamingPolicy) -> str:
    """
    Name a file that is copied rather than converted.

    Copying used to write ``output_dir / source.name``, so a copied file never
    met the naming layer: under ``-p`` a colon reached a shelf bound for a
    Kindle, which is the one thing ``-p`` exists to prevent, and under
    ``--name-by author-title`` half the shelf kept its old names.

    An already-zipped epub carries the same ``dc:title`` and ``dc:creator`` a
    package directory does, so a metadata policy can name it from its own
    contents. A PDF carries neither and keeps its filename, cleaned.

    Lives here rather than beside the copy, because three callers need the
    answer -- the copy itself and both orphan checks -- and computing it in
    three places is what let the copy and the orphan report disagree.

    :param source: The file to be copied.
    :param policy: The naming policy this run is using.

    :return: The filename to write it under.
    """
    metadata = None
    if copy_name_opens_file(source, policy):
        try:
            metadata = read_archive_package(source)
        except ValidationError:
            # A file that cannot describe itself still gets copied; it just
            # cannot be renamed from metadata it does not have.
            metadata = None
    return policy.filename(source.name, metadata)


def assign_names(
    packages: Sequence[Path],
    policy: NamingPolicy,
    on_collision: CollisionMode,
    *,
    shelf: Collection[str] = frozenset(),
    unopened: Container[Path] = frozenset(),
    library: Path | None = None,
) -> list[Assignment]:
    """
    Give every package an output name, resolving collisions deterministically.

    Assignment walks the packages in sorted order rather than the order they
    were selected, so the same set of packages always produces the same names
    regardless of shuffling -- except that a book whose name is already a
    file on the shelf claims it first (:func:`~epubconvert.run.claims.claim_order`).

    Under :data:`SUFFIX`, a book that has to share a name is marked with a
    digest of its own ``dc:identifier`` rather than its position in the
    colliding group. A position describes the group, so adding a book that
    sorted earlier renamed every later member; a digest describes the book, so
    the marker holds still while the library changes around it. Every member of
    a crowded group is marked, not all but the first, because "all but the
    first" is itself a position.

    Two things still move a name, and both are visible. A book entering a
    collision gains its marker, which is one rename rather than a cascade. And
    a book whose identifier is junk or shared -- 92 books in a surveyed
    library claim to be ``none``, and 52 more share a real value -- keeps the
    old positional suffix, because a marker that pretended to be stable would
    be worse than a number that admits it is not. A book whose own file is on
    the shelf under a number of its name, or under its marked name, keeps it
    when the books before it leave, or its crowd does
    (:func:`~epubconvert.run.claims.kept_numbers`).

    Policies that name a book after its own metadata need the package document
    read first. That read is skipped entirely for the policies that do not ask
    for it, which is what keeps a no-op rerun over thousands of books free of
    any source-side open. A package that cannot be parsed yields no metadata
    rather than an error, and the policy falls back to the directory name.
    Under ``--skip-incomplete`` a package iCloud has evicted is not read: it
    is left unnamed, and reported not downloaded, as an evicted zipped book
    is.

    :param packages: Packages to name.
    :param policy: Naming policy supplying filenames and identities.
    :param on_collision: :data:`SKIP` or :data:`SUFFIX`.
    :param shelf: The names of the files on the shelf, from
        :func:`~epubconvert.run.claims.shelf_names`. Every caller that names
        the library for a run passes the same shelf, so every route agrees.
    :param unopened: The books not to open, because opening them downloads
        them: under ``--skip-incomplete``, a package iCloud has evicted
        (:class:`~epubconvert.run.holders.Unopened`).
    :param library: The library's root, which each book's source is named
        relative to (:attr:`Assignment.source`); None names none.

    :return: One :class:`Assignment` per package, in sorted order.
    """
    setup = _Naming(
        policy,
        on_collision,
        getattr(policy, "max_bytes", 0),
        unopened=unopened,
        library=library,
    )
    unnamed = _left_unnamed(packages, policy, unopened)
    return sorted(
        [
            *_assign_all([p for p in packages if p not in unnamed], setup, shelf),
            *(
                Assignment(
                    package,
                    "",
                    "",
                    NOT_DOWNLOADED,
                    unnamed=True,
                    source=source_of(package, library),
                )
                for package in sorted(unnamed)
            ),
        ],
        key=lambda item: item.package,
    )


def _assign_all(
    packages: Sequence[Path], setup: _Naming, shelf: Collection[str]
) -> list[Assignment]:
    """
    Name every package to be named, as :func:`assign_names` describes.

    :param packages: Packages to name.
    :param setup: The naming configuration.
    :param shelf: The names of the files on the shelf.

    :return: One :class:`Assignment` per package, in sorted order.
    """
    policy = setup.policy
    wanted = _wanted_names(packages, policy)
    crowded = Counter(policy.identity(name) for _, name, _ in wanted)
    claims = Claims()

    bases = [_bases(name, metadata, setup, crowded) for _, name, metadata in wanted]
    keeping = _kept_on_shelf(
        _wanting(wanted, bases, crowded, setup),
        [_shown(package, setup.library) for package, _, _ in wanted],
        setup,
        (shelf, claims),
    )
    kept = [index for index, found in keeping.items() if found.file]
    named: dict[int, Assignment] = {}
    for index in [
        *kept,
        *(i for i in claim_order([b for b, _ in bases], shelf) if i not in kept),
    ]:
        named[index] = _assign_one(
            *wanted[index],
            setup=setup,
            claims=claims,
            crowded=crowded,
            keeping=keeping.get(index, Keeping()),
        )
    return [named[index] for index in range(len(wanted))]


def _left_unnamed(
    packages: Sequence[Path], policy: NamingPolicy, unopened: Container[Path]
) -> frozenset[Path]:
    """
    Find the packages that naming them would download.

    Under a policy that names a book from its package document, reading it
    downloads a package iCloud has evicted; ``--skip-incomplete`` exists to
    leave such a book where it is, and every one was read to be named.

    :param packages: Packages to name.
    :param policy: The naming policy.
    :param unopened: The books not to open.

    :return: The packages to leave unnamed, as not downloaded.
    """
    if not getattr(policy, "needs_metadata", False):
        return frozenset()
    return frozenset(package for package in packages if package in unopened)


def _wanting(
    wanted: Sequence[tuple[Path, str, Package | None]],
    bases: Sequence[tuple[str, str]],
    crowded: Counter[str],
    setup: _Naming,
) -> list[Wanting]:
    """
    Say what the shelf is asked about each package.

    :param wanted: Package, wanted name and metadata, in sorted order.
    :param bases: Each book's base and stable name, in the same order.
    :param crowded: How many books want each identity.
    :param setup: The naming configuration.

    :return: One per package, in the same order.
    """
    policy = setup.policy
    # A policy that reads no package document leaves the identifier to be
    # read where the shelf is asked, and only for a name with numbered files
    # on the shelf, or one two books want with a file.
    unread = not getattr(policy, "needs_metadata", False)
    return [
        Wanting(
            base,
            stable,
            usable_identifier(metadata),
            crowded[policy.identity(name)] == 1,
            package if unread else None,
            source_of(package, setup.library),
        )
        for (base, stable), (package, name, metadata) in zip(bases, wanted, strict=True)
    ]


def _shown(package: Path, library: Path | None) -> str:
    """A package as a person knows it: its path in the library, if there is one."""
    if library is not None and package.is_relative_to(library):
        return package.relative_to(library).as_posix()
    return str(package)


def _kept_on_shelf(
    wanting: Sequence[Wanting],
    shown: Sequence[str],
    setup: _Naming,
    claiming: tuple[Collection[str], Claims],
) -> dict[int, Keeping]:
    """
    Find the books that keep a file of theirs on the shelf.

    Under :data:`SUFFIX` a numbered or marked one; see
    :func:`~epubconvert.run.claims.kept_numbers`. In skip mode, the file of a
    name two books want that says it is one's
    (:func:`~epubconvert.run.telling.tell_apart`). A file of such a name that
    none of them may have is spoken for, so none claims it.

    :param wanting: What the shelf is asked about each package.
    :param shown: Each package as a person knows it.
    :param setup: The naming configuration.
    :param claiming: The names of the files on the shelf, and the names
        spoken for, updated in place.

    :return: What was found of each book whose files were looked at, by its
        index, with why it was refused a file where it cannot be told from
        another book.
    """
    shelf, claims = claiming
    told = tell_apart(
        wanting,
        shown,
        shelf,
        setup.policy,
        suffix=setup.on_collision == SUFFIX,
        unopened=setup.unopened,
    )
    for name, reason in told.refused.items():
        claims.refuse(setup.policy.identity(name), name, reason)
    found = (
        kept_numbers(wanting, shelf, setup.policy, setup.unopened, told.refused)
        if setup.on_collision == SUFFIX
        else {index: Keeping(name) for index, name in told.keeps.items()}
    )
    for index, identifier in told.identifiers.items():
        # Read to tell it from its namesakes, and carried as kept_numbers
        # carries it, so placing compares it with the file it is given.
        keeping = found.get(index, Keeping())
        if keeping.identifier is None:
            found[index] = keeping._replace(identifier=identifier)
    for index, reason in told.untold.items():
        keeping = found.get(index, Keeping())
        if not keeping.file:
            found[index] = keeping._replace(untold=reason)
    return found


@dataclass(frozen=True)
class _Naming:
    """The naming configuration, which every step of an assignment needs."""

    policy: NamingPolicy
    on_collision: CollisionMode
    #: The policy's byte budget, or 0 for no clamping.
    budget: int
    #: The books not to open, because opening them downloads them.
    unopened: Container[Path] = frozenset()
    #: The library's root, which each book's source is named relative to.
    library: Path | None = None


def _wanted_names(
    packages: Sequence[Path], policy: NamingPolicy
) -> list[tuple[Path, str, Package | None]]:
    """
    Name every package once, before any collision is resolved.

    Naming is two passes because a book cannot know it is in a crowd until
    every other book has been named. A single streaming pass could only mark
    the second and later arrivals, which is the positional rule again.

    :param packages: Packages to name.
    :param policy: The naming policy.

    :return: Package, wanted name and metadata, in sorted order.
    """
    wants_metadata = getattr(policy, "needs_metadata", False)
    wanted = []
    for package in sorted(packages):
        metadata = _metadata_of(package, wants_metadata)
        wanted.append((package, policy.filename(package.name, metadata), metadata))
    return wanted


def _assign_one(
    package: Path,
    name: str,
    metadata: Package | None,
    *,
    setup: _Naming,
    claims: Claims,
    crowded: Counter[str],
    keeping: Keeping,
) -> Assignment:
    """
    Settle one package's output name against the names already taken.

    :param package: The package directory.
    :param name: The name its policy asked for.
    :param metadata: Its package document, if one was read.
    :param setup: The naming configuration.
    :param claims: Names already spoken for, updated in place.
    :param crowded: How many packages wanted each identity.
    :param keeping: What :func:`~epubconvert.run.claims.kept_numbers` found
        of its files on the shelf: the numbered one it keeps, if any; whether
        the file of its plain name is another book's, when, with no digest
        of its identifier to move on to, it claims from its first number;
        and its identifier, read to find them, which it carries so placing
        can tell another book's file from its own.

    :return: The assignment, with an empty filename if the book lost.
    """
    base, stable = _bases(name, metadata, setup, crowded)
    group = setup.policy.identity(base)
    identifier = usable_identifier(metadata) or keeping.identifier

    # Refused, with a digest to go by, placing moves the book on to its
    # marked name (placing.place), which holds still; without one its plain
    # name is not claimed at all, and stays free for the file's own book,
    # which may be one nothing read.
    taken = (
        (keeping.file, setup.policy.identity(keeping.file))
        if keeping.file
        and claims.keep(group, setup.policy.identity(keeping.file), keeping.file)
        else _claim(
            claims,
            base,
            group,
            setup=setup,
            first=2 if keeping.refused and stable == base else 1,
        )
    )
    if taken is None:
        # Carries its identifier though it has no name, so an archive of it
        # already on the shelf is still recognised as a live book's.
        reason = (
            keeping.untold
            or claims.refused.get(filesystem_key(base))
            or lost_to(claims.holder(group, base), metadata)
        )
        return Assignment(
            package,
            "",
            group,
            reason,
            identifier=identifier,
            source=source_of(package, setup.library),
            untold=keeping.untold,
        )

    filename, key = taken
    return Assignment(
        package,
        filename,
        key,
        None,
        _named_without_author(metadata),
        _named_from_folder(metadata, setup.policy),
        identifier,
        # Where it goes if its name holds another book; see placing.place.
        # Its own name, numbered, when it has no digest: a copy already on
        # the shelf keeps its file (copynames.claim_copies), and a package
        # with nowhere to go was a collision on every run in suffix mode.
        stable if setup.on_collision == SUFFIX else None,
        kept_number=filename == keeping.file,
        source=source_of(package, setup.library),
        untold=keeping.untold,
    )


def _bases(
    name: str, metadata: Package | None, setup: _Naming, crowded: Counter[str]
) -> tuple[str, str]:
    """
    Find the name a book claims first, and its digest-marked name.

    :param name: The name its policy asked for.
    :param metadata: Its package document, if one was read.
    :param setup: The naming configuration.
    :param crowded: How many packages wanted each identity.

    :return: The marked name when the book is in a crowd in suffix mode,
        otherwise *name*; and the marked name either way.
    """
    stable = _stable_base(name, metadata, setup.budget)
    if setup.on_collision == SUFFIX and crowded[setup.policy.identity(name)] > 1:
        return stable, stable
    return name, stable


def _stable_base(name: str, metadata: Package | None, budget: int) -> str:
    """
    Mark a crowded name with a digest of the book's own identifier.

    :param name: The name the book wants and cannot have alone.
    :param metadata: The parsed package document, if it was read.
    :param budget: The policy's byte budget, or 0 for no clamping.

    :return: The marked name, or *name* unchanged when nothing usable
        identifies the book and the positional suffix has to do the job.
    """
    identifier = usable_identifier(metadata)
    if identifier is None:
        return name
    return marked(name, f" [{disambiguator(identifier)}]", budget)


def _named_without_author(metadata: Package | None) -> bool:
    """Whether this book was named from a document declaring no creator."""
    return bool(metadata and metadata.title and not metadata.creator)


def _named_from_folder(metadata: Package | None, policy: NamingPolicy) -> bool:
    """
    Whether a metadata policy had to fall back to the directory name.

    Only true for a policy that asked for metadata: every other policy names
    from the directory by design, and reporting that would be noise.

    :param metadata: The parsed package document, if one was read.
    :param policy: The naming policy.

    :return: True if the book gave the policy nothing to name it by.
    """
    if not getattr(policy, "needs_metadata", False):
        return False
    return metadata is None or not metadata.title


def _claim(
    claims: Claims, base: str, group: str, *, setup: _Naming, first: int = 1
) -> tuple[str, str] | None:
    """
    Take the first free candidate name for a book, or report that none is.

    :param claims: Names already spoken for, updated in place.
    :param base: The name to start from.
    :param group: The identity group this book competes in.
    :param setup: The naming configuration.
    :param first: The first position this book may take.

    :return: The claimed filename and its identity, or None if the group is
        exhausted.
    """
    limit = MAX_SUFFIX if setup.on_collision == SUFFIX else 1
    for position in range(max(claims.resume(group), first), limit + 1):
        candidate = suffixed(base, position, setup.budget)
        key = setup.policy.identity(candidate)
        if claims.take(group, position, key, candidate):
            return candidate, key
    claims.exhaust(group, limit)
    return None


def plan_exports(
    packages: Sequence[Path],
    output_dir: Path,
    policy: NamingPolicy,
    options: PlanOptions | None = None,
    assigned: Sequence[Assignment] | None = None,
) -> list[Decision]:
    """
    Decide what to do with every package, without doing any of it.

    Books already present are recognised by recomputing their identity from
    the filenames on disk, so the output directory stays the sole record of
    completed work and no state file is needed.

    :param packages: Package directories to consider.
    :param output_dir: Directory the epub files are written into.
    :param policy: Naming policy supplying filenames and identities.
    :param options: Planning behaviour; defaults are conservative.

    :return: One decision per package, in the order given.
    """
    settings = options if options is not None else PlanOptions()
    # Deduplicated, order preserved: the name map is keyed by path, so a
    # repeated package collapsed there while the decision list still emitted
    # one per element -- two workers writing the same target.
    packages = list(dict.fromkeys(packages))

    unopened = Unopened(packages=settings.check_incomplete)
    if assigned is None:
        assigned = assign_names(
            packages,
            policy,
            settings.on_collision,
            shelf=shelf_names(output_dir),
            unopened=unopened,
            library=settings.library,
        )
    assignments = assigned
    shelf = read_shelf(output_dir, policy, assignments, unopened=unopened)
    # Neither is a failure, and both change what the shelf looks like. A run
    # that says nothing leaves the only way to notice as looking afterwards
    # and wondering.
    authorless = sum(1 for item in assignments if item.authorless)
    if authorless:
        logger.warning("%d book(s) named without an author: none declared.", authorless)
    from_folder = sum(1 for item in assignments if item.from_folder)
    if from_folder:
        logger.warning(
            "%d book(s) kept their folder name: no title in the package document.",
            from_folder,
        )
    unnamed = sum(1 for item in assignments if item.unnamed)
    if unnamed:
        logger.warning(NOT_NAMED, unnamed)
    _say_untold(assignments)
    named = {item.package: item for item in assignments}
    # Naming read no package document, so no book carries an identifier to
    # compare against the archive holding its name. See _decide.
    unread = not getattr(policy, "needs_metadata", False)

    return [
        _decide(package, named[package], shelf, output_dir, settings, unread=unread)
        for package in packages
    ]


def _say_untold(assignments: Sequence[Assignment]) -> None:
    """
    Say which books were refused a file because nothing tells them apart.

    Once per crowd, whatever the mode: in skip mode each is a collision, and
    the collision's reason alone did not say that the file may be either
    one's only archive; in suffix mode each is written under a name of its
    own, a rewrite nothing else would explain.

    :param assignments: Every book's name.
    """
    untold: dict[str, list[Assignment]] = {}
    for item in assignments:
        if item.untold:
            untold.setdefault(item.untold, []).append(item)
    for reason, crowd in untold.items():
        logger.warning(
            "%s; %s.",
            printable(reason),
            (
                "each was given a name of its own"
                if all(item.filename for item in crowd)
                else "none was written"
            ),
        )


def _decide(
    package: Path,
    assignment: Assignment,
    shelf: Shelf,
    output_dir: Path,
    settings: PlanOptions,
    *,
    unread: bool = False,
) -> Decision:
    """
    Decide what to do with a single package.

    :param package: The package directory.
    :param assignment: Its assigned name, identity and collision reason.
    :param shelf: The archives already present, and the plan's names.
    :param output_dir: Directory the epub files are written into.
    :param settings: Planning behaviour.
    :param unread: Naming read no package document, so the assignment
        carries no identifier whatever the book declares.

    :return: The decision for this package.
    """
    # Neither "write it", which would replace another book's archive, nor
    # "exported", which would silently drop this one.
    filename, clash, reason = place(assignment, shelf)
    if assignment.unnamed:
        return Decision(package, INCOMPLETE, reason=reason)
    if not filename:
        return Decision(package, COLLISION, reason=reason)
    found = clash.path if clash is not None else None
    # --force still has to pass inspection. Settling it here, before the walk
    # below, let a DRM-protected or half-downloaded source overwrite a good
    # archive -- the one path a user reaches for when something already looks
    # wrong. Only the "already done" answer may be given without inspecting.
    forced = found is not None and settings.force
    if not forced:
        settled = _decide_against_existing(package, found, settings)
        if settled is not None:
            return settled
    refreshing = found is not None and not forced

    unusable = _decide_before_writing(
        package,
        found,
        settings,
        unread=unread,
        # About to write over an archive: its marker is read, whatever the
        # policy read before, one short read paid only by a book about to
        # replace something.
        own=(
            written_for(found, assignment.source, shelf.sources)
            if found is not None
            else None
        ),
    )
    if unusable is not None:
        if unusable.status != COLLISION:
            unusable.placed = found or output_dir / filename
        return unusable

    if found is not None and (forced or refreshing):
        # Written over the archive that is really there, not over a freshly
        # computed name: under a policy whose identity is looser than its
        # filename the two differ, and the stale file would keep satisfying
        # the identity check for ever.
        return Decision(
            package, PENDING, found, reason="forced" if forced else "source is newer"
        )

    return Decision(package, PENDING, output_dir / filename)


def _decide_before_writing(
    package: Path,
    found: Path | None,
    settings: PlanOptions,
    *,
    unread: bool,
    own: bool | None = None,
) -> Decision | None:
    """
    Decide whether a book about to be written may be, and over what.

    :param package: The package directory.
    :param found: The archive it would be written over, if any.
    :param settings: Planning behaviour.
    :param unread: Naming read no package document, so nothing has yet
        compared this book with the archive holding its name.
    :param own: Whether the archive's marker names this book's source,
        which says more than the identifiers can; None when it names none,
        or the plan names no sources.

    :return: The decision, or None when the book may be written.
    """
    # Under --skip-incomplete the source is inspected first: the read below
    # downloaded the package document of a book the inspection then called
    # not downloaded.
    inspected = settings.check_incomplete
    if inspected and (unusable := _decide_against_source(package, settings)):
        return unusable
    if found is not None and own is False:
        # Written from another book's source: that book's, maybe its last
        # one, whatever the name or the identifiers say.
        return Decision(
            package, COLLISION, reason=f"{found.name} was written for another book"
        )
    if found is not None and unread:
        # About to write over the archive holding this name, and naming read
        # nothing that could say whose it is. Folder names are not unique: the
        # library is walked recursively, so two subfolders can each hold a
        # Dune.epub, and romanize folds Café and Cafe to one name. Once the
        # book holding the name left the library, --refresh and --force wrote
        # the other over its archive, likely the last copy. One source read,
        # paid only by a book about to replace something.
        # A marker naming this book does not excuse the comparison: a book
        # deleted and another added at its path, whose identifiers differ, or
        # where it declares none and the file one.
        identifier = usable_identifier(_metadata_of(package, True))
        other = _decide_against_holder(package, found, identifier)
        if other is not None:
            return other
        if identifier is None and settings.library is not None:
            declared = declares_one(found)
            if declared is not None:
                return Decision(package, COLLISION, reason=declared)
    return None if inspected else _decide_against_source(package, settings)


def _decide_against_holder(
    package: Path, found: Path, identifier: str | None
) -> Decision | None:
    """
    Decide whether the archive holding this book's name holds another book.

    :mod:`epubconvert.run.holders` says why a name is not a book, and what
    reading the archive's identifier costs.

    :param package: The package directory.
    :param found: The archive on the shelf under this book's name.
    :param identifier: This book's usable identifier, or None.

    :return: A collision decision, or None when the archive may be this book.
    """
    reason = holds_another_book(found, identifier)
    return None if reason is None else Decision(package, COLLISION, reason=reason)


def _decide_against_existing(
    package: Path, found: Path | None, settings: PlanOptions
) -> Decision | None:
    """
    Decide what an archive already in the output directory settles, if anything.

    Answered before the package is inspected: a book that is already exported
    needs no source walk, and ``--skip-incomplete`` would otherwise pay for one
    on every book of the library on every rerun.

    Only ever answers "already exported" or "needs looking at". The paths that
    write are settled by the caller *after* inspection, because a book that is
    about to be overwritten still has to be a book worth writing.

    :param package: The package directory.
    :param found: The archive already present under this identity, if any.
    :param settings: Planning behaviour.

    :return: The decision, or None when the package still needs inspecting.
    """
    if found is None:
        return None
    if settings.refresh and _source_is_newer(package, found):
        return None
    return Decision(package, EXPORTED, found)


def _decide_against_source(package: Path, settings: PlanOptions) -> Decision | None:
    """
    Decide what inspecting the source settles, if anything.

    Runs for every book that is going to be written, ``--force`` included: a
    book about to overwrite a good archive still has to be a book worth
    writing.

    :param package: The package directory.
    :param settings: Planning behaviour.

    :return: The decision, or None when the package is usable.
    """
    status = inspect_package(package, check_incomplete=settings.check_incomplete)
    if status.drm:
        return Decision(package, DRM, reason=status.reason)
    if status.incomplete:
        return Decision(package, INCOMPLETE, reason=status.reason)
    return None


def _source_is_newer(package: Path, exported: Path) -> bool:
    """
    Report whether a package looks newer than its exported archive.

    Uses the package directory's own timestamp, which macOS updates when
    entries are added or removed. A book re-downloaded in place with identical
    entry names will not be noticed; use --force for that.

    :param package: The source package directory.
    :param exported: The archive already in the output directory.

    :return: True if the source appears to have changed since the export.
    """
    try:
        return package.stat().st_mtime > exported.stat().st_mtime
    except OSError:  # pragma: no cover - racing removal
        return False
