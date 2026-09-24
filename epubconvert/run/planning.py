"""
Deciding what to do with each package, before anything is written.

Planning is kept separate from exporting so that ``--list`` can render the
same decisions the exporter acts on, and so the rules that make a rerun safe
live in one place: identity is recomputed from the filenames already in the
output directory, which is why this tool needs no state file.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from zipfile import BadZipFile, ZipFile

from ..collect.identifiers import usable_identifier
from ..collect.package import ValidationError, read_package, read_package_dir
from ..collect.source import inspect_package
from ..export.naming import disambiguator, filesystem_key
from ..utils.app_logger import logger
from ..utils.display import printable, printable_json
from ..utils.opf import Package
from ..utils.policy import Assignment, NamingPolicy
from ..utils.spec import PACKAGE_SUFFIX
from .claims import MAX_SUFFIX, Claims, lost_to, marked, suffixed
from .holders import holds_another_book, identifier_on_shelf, same_identity
from .placing import Existing, Shelf, place, read_shelf

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for typing only
    from .convert import Report

#: What the planner can decide about a package. These strings are a public
#: contract, not an internal detail: ``--list`` names them all in its help text
#: and ``--list --json`` emits them verbatim. Typing them keeps a mistyped
#: comparison from passing the type checker.
Status = Literal["pending", "exported", "collision", "drm", "incomplete", "orphan"]

#: The :class:`~epubconvert.run.convert.Report` fields the outcome table may bump.
ReportField = Literal["skipped", "collisions", "drm", "incomplete"]

#: Decision statuses. Each constant's value is what a user sees.
PENDING: Status = "pending"
EXPORTED: Status = "exported"
COLLISION: Status = "collision"
DRM: Status = "drm"
INCOMPLETE: Status = "incomplete"

#: Not a decision about a book in the library at all: an archive on the shelf
#: that no book in the library claims. Reported, never acted on.
ORPHAN: Status = "orphan"

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
)


@dataclass(frozen=True)
class PlanOptions:
    """How the planner should treat the packages it is given."""

    force: bool = False
    refresh: bool = False
    check_incomplete: bool = False
    on_collision: CollisionMode = SKIP


@dataclass
class Decision:
    """What the planner decided about one package."""

    package: Path
    status: Status
    target: Path | None = None
    reason: str | None = None

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
            with ZipFile(source) as archive:
                metadata = read_package(archive)
        except (ValidationError, BadZipFile, OSError):
            # A file that cannot describe itself still gets copied; it just
            # cannot be renamed from metadata it does not have.
            metadata = None
    return policy.filename(source.name, metadata)


def assign_names(
    packages: Sequence[Path], policy: NamingPolicy, on_collision: CollisionMode
) -> list[Assignment]:
    """
    Give every package an output name, resolving collisions deterministically.

    Assignment walks the packages in sorted order rather than the order they
    were selected, so the same set of packages always produces the same names
    regardless of shuffling.

    Under :data:`SUFFIX`, a book that has to share a name is marked with a
    digest of its own ``dc:identifier`` rather than its position in the
    colliding group. A position describes the group, so adding a book that
    sorted earlier renamed every later member; a digest describes the book, so
    the marker holds still while the library changes around it. Every member of
    a crowded group is marked, not all but the first, because "all but the
    first" is itself a position.

    Two things still move a name, and both are visible. A book entering or
    leaving a collision gains or loses its marker, which is one rename rather
    than a cascade. And a book whose identifier is junk or shared -- 92 books in
    a surveyed library claim to be ``none``, and 52 more share a real value --
    keeps the old positional suffix, because a marker that pretended to be
    stable would be worse than a number that admits it is not.

    Policies that name a book after its own metadata need the package document
    read first. That read is skipped entirely for the policies that do not ask
    for it, which is what keeps a no-op rerun over thousands of books free of
    any source-side open. A package that cannot be parsed yields no metadata
    rather than an error, and the policy falls back to the directory name.

    :param packages: Packages to name.
    :param policy: Naming policy supplying filenames and identities.
    :param on_collision: :data:`SKIP` or :data:`SUFFIX`.

    :return: One :class:`Assignment` per package, in sorted order.
    """
    setup = _Naming(policy, on_collision, getattr(policy, "max_bytes", 0))
    wanted = _wanted_names(packages, policy)
    crowded = Counter(policy.identity(name) for _, name, _ in wanted)
    claims = Claims()

    return [
        _assign_one(
            package, name, metadata, setup=setup, claims=claims, crowded=crowded
        )
        for package, name, metadata in wanted
    ]


@dataclass(frozen=True)
class _Naming:
    """The naming configuration, which every step of an assignment needs."""

    policy: NamingPolicy
    on_collision: CollisionMode
    #: The policy's byte budget, or 0 for no clamping.
    budget: int


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
) -> Assignment:
    """
    Settle one package's output name against the names already taken.

    :param package: The package directory.
    :param name: The name its policy asked for.
    :param metadata: Its package document, if one was read.
    :param setup: The naming configuration.
    :param claims: Names already spoken for, updated in place.
    :param crowded: How many packages wanted each identity.

    :return: The assignment, with an empty filename if the book lost.
    """
    base = name
    stable = _stable_base(name, metadata, setup.budget)
    if setup.on_collision == SUFFIX and crowded[setup.policy.identity(name)] > 1:
        base = stable
    group = setup.policy.identity(base)

    taken = _claim(claims, base, group, setup=setup)
    if taken is None:
        # Carries its identifier though it has no name, so an archive of it
        # already on the shelf is still recognised as a live book's.
        reason = lost_to(claims.holder(group), metadata)
        return Assignment(
            package, "", group, reason, identifier=usable_identifier(metadata)
        )

    filename, key = taken
    return Assignment(
        package,
        filename,
        key,
        None,
        _named_without_author(metadata),
        _named_from_folder(metadata, setup.policy),
        usable_identifier(metadata),
        # Where it goes if its name holds another book; see placing.place.
        stable if setup.on_collision == SUFFIX and stable != name else None,
    )


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
    claims: Claims, base: str, group: str, *, setup: _Naming
) -> tuple[str, str] | None:
    """
    Take the first free candidate name for a book, or report that none is.

    :param claims: Names already spoken for, updated in place.
    :param base: The name to start from.
    :param group: The identity group this book competes in.
    :param setup: The naming configuration.

    :return: The claimed filename and its identity, or None if the group is
        exhausted.
    """
    limit = MAX_SUFFIX if setup.on_collision == SUFFIX else 1
    for position in range(claims.resume(group), limit + 1):
        candidate = suffixed(base, position, setup.budget)
        key = setup.policy.identity(candidate)
        if claims.take(group, position, key, candidate):
            return candidate, key
    claims.exhaust(group, limit)
    return None


def find_orphans(
    output_dir: Path,
    policy: NamingPolicy,
    packages: Sequence[Path],
    on_collision: CollisionMode = SKIP,
    *,
    assigned: Sequence[Assignment] | None = None,
) -> list[Path]:
    """
    Find archives on the shelf that no book in the library claims.

    The library has always been seen richly -- five statuses, reasons, tallies
    -- and the output directory not at all. An archive left behind by a book
    deleted from the library, or by adopting a naming policy that renames
    everything, sits there for ever: ``--verify`` blesses it because it is a
    sound archive, and ``--list`` only ever looked at sources.

    Asks the planner for the names rather than deriving them, so a book that
    took a ``" (2)"`` suffix is not reported as abandoning the name it holds.
    And a file is claimed only when the planner would call it that book's:
    one holding another book is the plan's collision, and counting it as
    claimed hid the archive of a book deleted from the library, which can be
    its last copy. Under ``--name-by author-title`` that reads each claimed
    archive's identifier, as planning does (:mod:`epubconvert.run.holders`),
    and a book that moved on to its marked name claims that file
    (:func:`epubconvert.run.placing.place`).

    Yet a file under a name the plan gave a book, holding another book of the
    library, is that other book's. In skip mode the Ace edition, exported
    alone and then outsorted by an added 1965 edition, loses the name and is a
    collision; its archive, the only copy, was listed here as claimed by
    nothing -- the list a person reviews before deleting. An archive under a
    name no book wants, such as one left by adopting a renaming policy, stays
    an orphan: its book is written under the new name. A book that lost its
    name claims the file under the name it wanted, when that file is of its
    identity and may be its book: under a policy that names from the folder
    there is no identifier to go by, so ``b/dune.epub``, exported alone and
    then outsorted by an added ``a/Dune.epub`` that a case-insensitive
    filesystem gives the same file, had its only archive listed here.

    Nothing is deleted, here or anywhere. The never-deletes stance is
    deliberate; the gap was that nothing would say either.

    :param output_dir: Directory holding exported files.
    :param policy: Naming policy supplying filenames and identities.
    :param packages: **Every** package in the library, not the subset this run
        is looking at -- ``--match`` narrows a run, not the shelf.
    :param on_collision: The collision mode, so suffixed names are recognised.
    :param assigned: The names already given, when the caller has them, the
        files copied through included
        (:func:`~epubconvert.run.copynames.claim_copies`): a copy claims
        the file it is placed at, as a package does.

    :return: Archives no book accounts for, sorted by path.
    """
    if assigned is None:
        assigned = assign_names(packages, policy, on_collision)
    shelf = read_shelf(output_dir, policy, assigned)
    claimed: set[str] = set()
    for item in assigned:
        clash = place(item, shelf).clash
        if clash is None and not item.filename:
            clash = _held_by_loser(item, shelf)
        if clash is not None:
            claimed.add(filesystem_key(clash.identity))

    live = {item.identifier for item in assigned if item.identifier}
    return sorted(
        found
        for found in output_dir.glob(f"*{PACKAGE_SUFFIX}")
        if found.is_file()
        and (key := filesystem_key(policy.identity(found.name))) not in claimed
        and not (live and key in shelf.spoken and identifier_on_shelf(found) in live)
    )


def _held_by_loser(item: Assignment, shelf: Shelf) -> Existing | None:
    """
    Find the archive a book that lost its name may still hold.

    :param item: The book, with no name and the identity of the one it wanted.
    :param shelf: The archives already present.

    :return: The archive under that name when it can be this book's, as the
        plan would judge a book of that name: of its identity, and not
        holding another book by identifier.
    """
    found = shelf.existing.get(filesystem_key(item.identity))
    if found is None or not same_identity(found.identity, item.identity):
        return None
    return found if holds_another_book(found.path, item.identifier) is None else None


def orphan_decisions(orphans: Sequence[Path]) -> list[Decision]:
    """
    Render orphans as decisions so one listing can carry both.

    :param orphans: Archives no book accounts for.

    :return: One decision per orphan.
    """
    return [
        Decision(path, ORPHAN, path, reason="no book in the library claims this name")
        for path in orphans
    ]


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

    if assigned is None:
        assigned = assign_names(packages, policy, settings.on_collision)
    assignments = assigned
    shelf = read_shelf(output_dir, policy, assignments)
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
    named = {item.package: item for item in assignments}
    # Naming read no package document, so no book carries an identifier to
    # compare against the archive holding its name. See _decide.
    unread = not getattr(policy, "needs_metadata", False)

    return [
        _decide(package, named[package], shelf, output_dir, settings, unread=unread)
        for package in packages
    ]


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

    unusable = _decide_before_writing(package, found, settings, unread=unread)
    if unusable is not None:
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
    package: Path, found: Path | None, settings: PlanOptions, *, unread: bool
) -> Decision | None:
    """
    Decide whether a book about to be written may be, and over what.

    :param package: The package directory.
    :param found: The archive it would be written over, if any.
    :param settings: Planning behaviour.
    :param unread: Naming read no package document, so nothing has yet
        compared this book with the archive holding its name.

    :return: The decision, or None when the book may be written.
    """
    if found is not None and unread:
        # About to write over the archive holding this name, and naming read
        # nothing that could say whose it is. Folder names are not unique: the
        # library is walked recursively, so two subfolders can each hold a
        # Dune.epub, and romanize folds Café and Cafe to one name. Once the
        # book holding the name left the library, --refresh and --force wrote
        # the other over its archive, likely the last copy. One source read,
        # paid only by a book about to replace something.
        identifier = usable_identifier(_metadata_of(package, True))
        other = _decide_against_holder(package, found, identifier)
        if other is not None:
            return other
    return _decide_against_source(package, settings)


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


@dataclass(frozen=True)
class _Outcome:
    """How one non-pending status is counted and reported."""

    #: Name of the :class:`~epubconvert.run.convert.Report` field to increment.
    #: Narrowed to a Literal because the table drives a setattr, which turned a
    #: type-checked ``report.drm += 1`` into a string the checker cannot see:
    #: renaming a Report field would have broken this at runtime with mypy,
    #: ruff and pylint all silent.
    counter: ReportField
    #: Whether the per-book line is a warning rather than information.
    warn: bool
    #: Format string for the per-book line. Every entry uses the same two
    #: named fields, ``name`` and ``reason``, so the caller never has to work
    #: out which arguments a particular message wants.
    line: str
    #: Format string for the closing tally; takes the count.
    tally: str


#: Everything the reporter needs to know about a status, in one place. The
#: alternative -- an if/elif chain over the statuses and a second block of ``if
#: report.x`` tallies below it -- stated the same mapping twice, so a new status
#: had to be added in three places and two of the branches drifted into being
#: byte-identical.
_OUTCOMES: dict[Status, _Outcome] = {
    EXPORTED: _Outcome(
        counter="skipped",
        warn=False,
        line="Already exported, skipping: %(name)s",
        tally="Skipped %d already-exported file(s).",
    ),
    COLLISION: _Outcome(
        counter="collisions",
        warn=True,
        line="Name collision, skipping: %(name)s (%(reason)s)",
        tally="%d package(s) skipped because another book claims the same output name.",
    ),
    DRM: _Outcome(
        counter="drm",
        warn=True,
        line="Skipped, %(reason)s: %(name)s",
        tally="%d package(s) skipped as DRM-protected.",
    ),
    INCOMPLETE: _Outcome(
        counter="incomplete",
        warn=True,
        line="Skipped, %(reason)s: %(name)s",
        tally="%d package(s) skipped as not downloaded.",
    ),
}


def record_decisions(decisions: Sequence[Decision], report: Report) -> None:
    """
    Fold planning decisions into the report and log them.

    Pending decisions are left alone; the exporter counts those as it writes
    them, so counting here too would double them.

    :param decisions: The planner's output.
    :param report: Report to accumulate counts into.
    """
    for decision in decisions:
        if decision.status == PENDING:
            # Counted by the exporter as it writes, so counting here doubles it.
            continue
        # Indexed, not .get(): a sixth status should raise here in the tests
        # rather than disappear silently from the report and the tallies.
        outcome = _OUTCOMES[decision.status]

        setattr(report, outcome.counter, getattr(report, outcome.counter) + 1)
        log = logger.warning if outcome.warn else logger.info
        log(
            outcome.line,
            {
                "name": printable(decision.package.name),
                "reason": printable(str(decision.reason)),
            },
        )

    # Counted from the decisions in hand rather than from the report, which
    # export_planned documents as something a caller may accumulate across
    # calls -- so a second call logged the running total as this batch's tally.
    seen = Counter(decision.status for decision in decisions)
    for status, outcome in _OUTCOMES.items():
        if seen[status]:
            log = logger.warning if outcome.warn else logger.info
            log(outcome.tally, seen[status])


def render_listing(decisions: Sequence[Decision], as_json: bool) -> str:
    """
    Render the planner's decisions for human or machine consumption.

    :param decisions: The planner's output.
    :param as_json: Emit JSON rather than a table.

    :return: The text to print.
    """
    if as_json:
        # Titles stay readable; only what printable() would escape is escaped.
        return printable_json(
            json.dumps(
                [
                    {
                        "name": decision.package.name,
                        # An orphan has no source package; the path in "target" is
                        # where the file actually is.
                        "source": (
                            None if decision.status == ORPHAN else str(decision.package)
                        ),
                        "status": decision.status,
                        "target": str(decision.target) if decision.target else None,
                        "reason": decision.reason,
                    }
                    for decision in decisions
                ],
                indent=2,
                ensure_ascii=False,
            )
        )

    if not decisions:
        return "No books found."

    width = max(len(decision.status) for decision in decisions)
    lines = [
        f"{decision.status:<{width}}  {printable(decision.display_name)}"
        + (f"  ({printable(decision.reason)})" if decision.reason else "")
        for decision in decisions
    ]
    counts = Counter(decision.status for decision in decisions)
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    lines.append("")
    lines.append(summary)
    return "\n".join(lines)
