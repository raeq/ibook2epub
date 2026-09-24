# Formal models

TLA+ models of the parts of ibook2epub whose correctness depends on how
steps interleave: concurrent runs, killed runs, and one run's output being
the next run's input. Tests exercise particular schedules. TLC, the TLA+
model checker, explores every schedule a model allows, up to the model's
bounds.

A model is a separate description of the code, not the code. Each one names
the functions it abstracts, and each action carries a comment tying it to
the line it stands for. When you change one of those functions, change the
model with it.

## Running

TLC needs Java 11 or later and `tla2tools.jar`. CI pins
[v1.7.4](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4) by
SHA-256 (see `.github/workflows/ci.yml`).

```bash
curl -sSfLO https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
TLA2TOOLS_JAR=$PWD/tla2tools.jar pytest tests/test_formal.py
```

`tests/test_formal.py` runs every configuration and holds each to the
outcome in its `EXPECTED` table. Without `TLA2TOOLS_JAR` it only checks that
every `*.cfg` here has an entry in that table.

To run one configuration by hand and see a counterexample trace:

```bash
cd formal
java -cp tla2tools.jar tlc2.TLC -deadlock \
    -config OutputProtocol.MixedLockingUnguarded.cfg OutputProtocol.tla
```

`-deadlock` is needed because a state where every run has finished has no
successor, and TLC would otherwise report that as a fault.

## OutputProtocol

This models the output lock, `sweep_partials`, and `zip_package`'s
write-then-rename, with runs that fail, get Ctrl-C'd, are killed outright,
and are started again. It checks four properties:

- **OnlyCompleteBooks:** every `*.epub` in the output directory is a
  complete book.
- **NoLiveWorkSwept:** no sweep deletes a temporary file that another run is
  still writing.
- **MutualExclusion:** at most one run believes it has exclusive access.
- **AbandonedCleanedUp:** a temporary file left by a killed run is
  eventually removed.

The model has 3 runs, 2 books and up to 2 kills. The table shows what
TLC 1.7.4 reports for each configuration:

| Configuration | flock works for | Age guard | Outcome | Distinct states |
|---|---|---|---|---|
| `Locking` | every run | on | all hold | 249,180 |
| `LockingUnguarded` | every run | off | all hold | 8,484 |
| `MixedLocking` | r1 only | on | all hold | 600,912 |
| `MixedLockingUnguarded` | r1 only | off | **NoLiveWorkSwept violated** | — |
| `MixedLockingStall` | r1 only | on, but a live run can stall | **NoLiveWorkSwept violated** | — |
| `NoLocking` | no run | on | **AbandonedCleanedUp violated** | — |

What the configurations that fail show:

- **MixedLockingUnguarded** is the defect that `STALE_PARTIAL_SECONDS`
  fixes. A run that could not take the lock (on NFS, `ENOLCK`, transiently)
  carries on unlocked. A later run gets the lock, and its sweep deletes the
  first run's in-flight temporary file.
- **MixedLockingStall** is the assumption the fix depends on: a live run
  never leaves its temporary untouched for longer than the threshold. The
  comment on `STALE_PARTIAL_SECONDS` records the measurements behind that.
- **NoLocking** is accepted behaviour. On a share with no advisory locking,
  nothing is ever swept, because no run can tell an abandoned temporary
  from another run's live one.

## RerunPlanner

This models how the planner names books (`assign_names`) and how a run reads
the shelf back (`plan_exports`). There is no state file, so a book whose
name is on the shelf looks exported. The model checks what that inference
gets right over a sequence of runs while the library changes. It checks
four properties:

- **ExportedMeansTheBooksOwnFile:** a book the planner reports as exported is
  the book held in the file it points at.
- **NeverWritesOverAnotherBook:** no run replaces another book's archive.
- **SuffixKeepsEveryIdentifiableBook:** under `--on-collision suffix`, no
  run leaves a book with a usable identifier unexported because another
  book's archive holds its name. Checked in the suffix configurations only;
  skip mode reports that as a collision by design.
- **NoArchiveOfTheLibraryIsAnOrphan:** the orphan check (`find_orphans`)
  never lists the archive of a book that is still in the library: the list
  a person reviews before deleting. Checked in `Changing` and the copies
  and removal configurations. Suffix mode breaks it by design when a book
  enters a collision and takes its marker: the old file stays, and is
  reported as an orphan. A book whose crowd leaves keeps its marked or
  numbered file where anything can say it is its own
  (`claims.kept_numbers`); the configurations below say where nothing can.

Names are strings, so a title that looks like a suffix (`Dune (2)`) collides
exactly as it does on disk. The model has 3 editions sharing one title, as
they do under `--name-by author-title`, and runs that can be stopped
partway. `VerifyHolder` switches on `_decide_against_holder`, which reads
the identifier of the archive already on the shelf and reports a collision
when it and the book's own identifier are both usable and differ.
`ReadsSources` says whether naming read each book's package document, as
`--name-by author-title` does; without it the book's identifier is read
only when the book is about to be written over an archive. `MoveOn`
switches on `placing.place`: under suffix mode, a book whose name holds another
book moves on to the first free position of its marked name.

`Copies` are the books copied through rather than converted: PDFs and books
that arrived already zipped. They take their names after every package
(`copynames.claim_copies`), with no digest marker, and a package a copy wants
the name of has its identifier read whatever the policy. `KeepOwn` switches
on the claim-pass fixes: a copy whose own file is on the shelf under its name
or one of its numbers keeps it in either mode and claims before the others,
and a package with no digest marker moves on to its own name, numbered.
`AllowChanges` lets the library start with any of its books and have books
added; `AllowRemovals` lets books be removed as well. `TakesArchive` removes
a book's archive from the shelf with the book, as a person deleting both
does, and with it a library that starts whole can lose books without gaining
any.

`SharedId` is the books that declare one identifier between them, so anything
that compares identifiers takes each for the others. A copy's own file is its
own bytes, which the claim pass tells by size and modification time; where the
copy and the file both declare an identifier, a file of the copy's identifier
is its own too, the copy made before Apple rewrote the book. `KeepOne` switches
on the round-5 claim pass: each file on the shelf is kept by one copy at most,
a copy whose own bytes are there keeps them before a copy that goes by
identifier, and a file under a name a package was given is that package's
unless the identifiers say otherwise. `KeepNumbered` switches on
`claims.kept_numbers`: in suffix mode a package keeps the numbered file of its
name that declares its identifier, which is read for this whatever the
policy (unless `--skip-incomplete` leaves an evicted book unopened, which the
model does not describe; the orphan check then lists no numbered or marked
file of its name), or the file of its marked name once its crowd has left it; with no
usable identifier, the one numbered file when no other package wants the name
and nothing holds the plain name, and with `AskNumbered` only when that file
declares no usable identifier either. `KeepShared` asks the same where two
packages want one name and a file of it is on the shelf, numbered or not:
the one whose identifier the plain file declares keeps it. A copy that keeps that file sends the
package back to claim a name (`_Claiming.reclaim`). `ReclaimOwn` widens that
to any package given the name of a file the claim pass kept as a copy's own
bytes: in suffix mode it claims its marked or numbered name, and in skip mode
it loses the name. With `KeepOwn`, a copy whose claimed name holds a file
that is not its own is placed at no file on the shelf (`not_own`), since its
size says so where no identifier can.

| Configuration | Runs | Library | Identifiers | Check | Outcome |
|---|---|---|---|---|---|
| `Stable` | whole library, `--refresh` | fixed | none | on | both hold |
| `StableSuffix` | the same, suffix mode, look-alike title | fixed | two | on | all three hold |
| `Changing` | `--match`, `--refresh` | books added and removed | all | on | the two, and NoArchiveOfTheLibraryIsAnOrphan, hold |
| `ChangingSuffix` | the same, suffix mode | books added and removed | all | on | all three hold |
| `ChangingSuffixStuck` | the same, without `MoveOn` | books added and removed | all | on | **SuffixKeepsEveryIdentifiableBook violated** |
| `MatchUnverified` | `--match` | fixed | all | off | both hold |
| `ChangesUnverified` | whole library | books added and removed | all | off | **ExportedMeansTheBooksOwnFile violated** |
| `RefreshUnverified` | whole library, `--refresh` | books added and removed | all | off | **NeverWritesOverAnotherBook violated** |
| `Unidentifiable` | `--match`, `--refresh` | books added and removed | book 1 only | on | **ExportedMeansTheBooksOwnFile violated** |
| `FolderNamedWrites` | `--match`, `--refresh`, named from the folder | books added and removed | all | before a write | NeverWritesOverAnotherBook holds |
| `FolderNamedReports` | the same | books added and removed | all | before a write | **ExportedMeansTheBooksOwnFile violated** |
| `CopiesSuffix` | `--match`, `--refresh`, suffix mode, named from the folder, two copies and a package | books added | all | before a write, and where a copy wants the name | all four hold |
| `CopiesSuffixStuck` | the same, without `KeepOwn` | books added | all | the same | **NoArchiveOfTheLibraryIsAnOrphan violated** |
| `CopiesSharedId` | as `CopiesSuffix`, the two copies of one identifier | books added | all | the same | all four hold |
| `CopiesSharedIdLoose` | the same, without `KeepOne` | books added | all | the same | **ExportedMeansTheBooksOwnFile violated** |
| `CopiesOneIdentifier` | as `CopiesSuffix`, all three of one identifier | books added | all | the same | SuffixKeepsEveryIdentifiableBook and NoArchiveOfTheLibraryIsAnOrphan hold |
| `CopiesOneIdentifierLoose` | the same, without `KeepOne` | books added | all | the same | **NoArchiveOfTheLibraryIsAnOrphan violated** |
| `CopiesRemovals` | as `CopiesSuffix` | books added and removed | all | the same | NoArchiveOfTheLibraryIsAnOrphan holds |
| `CopiesRemovalsStuck` | the same, without `KeepNumbered` | books added and removed | all | the same | **NoArchiveOfTheLibraryIsAnOrphan violated** |
| `CopiesRemovalsRead` | the same, every identifier read | books added and removed | all | on | all four hold |
| `CopiesRemovalsDeleted` | the same, each book removed with its archive | books added and removed | all | on | all four hold |
| `CopiesUnidentified` | as `CopiesRemovalsDeleted` | books added and removed | the package and one copy | on | all four hold |
| `CopiesUnidentifiedSkip` | the same, skip mode | books added and removed | the package and one copy | on | all four hold |
| `CopiesUnidentifiedLoose` | `CopiesUnidentified` without `ReclaimOwn` | books added and removed | the package and one copy | on | **ExportedMeansTheBooksOwnFile violated** |
| `NumberedRemovals` | `--match`, `--refresh`, suffix mode, named from the folder, two packages | removed with their archives | none | before a write | NoArchiveOfTheLibraryIsAnOrphan holds |
| `NumberedRemovalsStuck` | the same, without `KeepNumbered` | removed with their archives | none | the same | **NoArchiveOfTheLibraryIsAnOrphan violated** |
| `NumberedRemovalsCrowd` | `NumberedRemovals` with three packages | removed with their archives | none | the same | **NoArchiveOfTheLibraryIsAnOrphan violated** |
| `NumberedLeftBehind` | `--match`, `--refresh`, suffix mode, named from the folder, a package titled like the other's name numbered | books added and removed, archives left | the look-alike only | before a write | ExportedMeansTheBooksOwnFile and NoArchiveOfTheLibraryIsAnOrphan hold |
| `NumberedLeftBehindLoose` | the same, without `AskNumbered` | books added and removed, archives left | the look-alike only | the same | **ExportedMeansTheBooksOwnFile violated** |
| `NamesakeAdded` | `--match`, `--refresh`, suffix mode, named from the folder, two packages of one name | books added | all | before a write | all four hold |
| `NamesakeAddedLoose` | the same, without `KeepShared` | books added | all | the same | **ExportedMeansTheBooksOwnFile violated** |

What the configurations that fail show:

- **The `Unverified` rows** are the three defects the check fixes. Each one
  reproduced with the code before the check; `MatchUnverified` no longer
  does, because every run now names the whole library, so a run narrowed by
  `--match` gives each book the name a full run gives it and the first case
  below cannot arise even without the check. It stays as the configuration
  that shows it. The setup is three packages
  titled *Dune* by Frank Herbert, each with its own identifier, run with
  `--name-by author-title`:
  - `--match 1965` writes `Frank Herbert - Dune.epub`. A later
    `--match Ace` names the Ace edition on its own, so it wants the same
    name, and `--list` reports it as `exported` from the 1965 edition's
    file.
  - After the 1965 edition is deleted from the library, the next run
    reports the Ace edition as `exported` from that same file.
  - With `--refresh` and a newer source, the Ace edition is written over the
    1965 edition's archive. That was likely the last copy of a book deleted
    from Apple Books.

  `tests/test_planning.py::TestANameOnTheShelfIsNotProofOfTheBook` replays
  each of these against the CLI.
- **`Unidentifiable`** is the limit the fix does not remove. When one of the
  two books has no usable identifier (none at all, or a placeholder such as
  `none`), nothing tells them apart, and the name decides as it did before.
  Two books that share a genuine identifier, such as a converter's template
  UUID, cannot be told apart either.
- **`ChangingSuffixStuck`** is what the check cost suffix mode before
  `placing.place`. With the 1965 edition's archive under the plain name, a run
  that names the Ace edition alone -- any run after the 1965 edition is
  deleted -- gives it that name, finds it held by another
  book, and reports a collision, on every run for ever: the mode that exists
  to keep both kept one. Now the Ace edition moves on to its marked name,
  `Frank Herbert - Dune [<digest>].epub`, which the next run finds again
  because the digest is of its own identifier.
  `tests/test_planning.py::TestSuffixModeMovesOffAnotherBooksName` replays
  it against the CLI.
- **`FolderNamedReports`** is the other limit. The default policy,
  `strip` and `romanize` name a book from its package folder and read no
  package document, so a planner that trusted the name there wrote over
  the other book's archive, as `RefreshUnverified` does. Folder names are
  not unique: the library is walked recursively, so `a/Dune.epub` and
  `b/Dune.epub` both exist; `strip` folds case and replaces characters
  other filesystems reject, and `romanize` folds accents and
  transliterates, so `Café.epub` and `Cafe.epub` want one name. Before
  `--refresh` or `--force` writes over an archive, `_decide_before_writing`
  now reads that one book's identifier and runs the check
  (`FolderNamedWrites`). Such a book does not move on under suffix mode:
  the next run, which reads nothing, would not find it there. A book
  reported `exported` is still not checked: that would read every source
  and every archive on every rerun, which is the cost these policies exist
  to avoid. So when the book holding a folder name leaves the library, or a
  `--match` run names only its namesake, the namesake is listed as
  `exported` from the other book's file and its archive is not reported as
  an orphan. It is never written over. In suffix mode a namesake added
  beside it no longer is, where both declare a usable identifier
  (`NamesakeAdded`).
  `tests/test_planning.py::TestAFolderNameIsNotProofOfTheBook` replays the
  writes against the CLI.

- **`CopiesSuffixStuck`** is the claim pass before a copy kept its own
  file in suffix mode. The rule that a copy already on the shelf keeps its
  file was applied only to a copy that found no free name, and under
  `--on-collision suffix` a free ` (n)` always exists. So a zipped
  `b/Book.epub` copied before a package `pkg/Book.epub`, or another zipped
  `a/Book.epub`, of its name arrived was copied again as `Book (2).epub`, and
  its first file was listed as an orphan while the package was a collision
  on every run. `CopiesSuffix` holds with the fix, over additions to the
  library: `--match` runs, `--refresh`, and runs cut short.
  `tests/test_copy_shelf.py` replays it against the CLI, and the model
  found a case of it the tests had not: a copy numbered ` (2)` by a run
  narrowed with `--match` took ` (3)` once a package arrived, and was copied
  again, until a copy kept its own file under any of its numbers.

  With removals too, `CopiesRemovals` holds NoArchiveOfTheLibraryIsAnOrphan
  named from the folder, and `CopiesRemovalsRead` and
  `CopiesRemovalsDeleted` all four where naming reads every identifier,
  whether a book's archive stays or goes with it. Named from the folder,
  ExportedMeansTheBooksOwnFile still fails as in `FolderNamedReports`.

- **`CopiesSharedIdLoose` and `CopiesOneIdentifierLoose`** are the claim
  pass before `KeepOne`. It asked of each copy only whether a file under its
  name, or one of its numbers, was its own, and two copies of one book, or a
  copy and a package of one, declare one identifier: the second copy took
  the first's file, or the package's archive, for its own. In skip mode the
  collision the first run reported was gone from the next, listed as copied
  (`CopiesSharedIdLoose`); in suffix mode the second copy's own file was
  listed as an orphan (`CopiesOneIdentifierLoose`).
  `tests/test_copy_keeping.py` replays both against the CLI. Where all
  three share one identifier, ExportedMeansTheBooksOwnFile and
  NeverWritesOverAnotherBook still do not hold: the `Unidentifiable` limit.

- **`CopiesRemovalsStuck` and `NumberedRemovalsStuck`** are suffix mode
  before `claims.kept_numbers`. A book with no digest marker is numbered by
  its place, or moved on past another book's file to the first free
  number, and when that book left the library it took the name it had
  given up: reported exported from the other book's file where that
  stayed, written again where it went with its book, and its own archive
  listed as an orphan either way. A package's identifier is now read for a
  name with numbered files on the shelf, and it keeps the one declaring
  it, or, once its crowd has left it, the file of its marked name. That
  was a rename by design, and `CopiesRemovalsDeleted` found the case of it
  that left an orphan: a book moved on to its marked name past another
  book's archive took its plain name back once that archive was deleted.
  `NumberedRemovals` is the rule for books with no usable identifier: two
  packages of one name, the numbered one alone once the other leaves with
  its archive. `tests/test_numbered_names.py` replays these against the CLI.

  The model found a case the rule had made worse, now closed: a package
  alone among the packages kept a numbered file that was a copy's own
  bytes, moved on past it, and left the plain name to another copy, and a
  later run listed its archive as an orphan (`_Claiming.reclaim`).

- **`CopiesUnidentifiedLoose`** is the claim pass before `ReclaimOwn`. A
  zipped book declaring no usable identifier was copied, and a package of
  its name added: the claim pass kept the file as the copy's own bytes, by
  its size and modification time, and only a package that had kept a
  numbered file was sent on. Placing asked the identifiers, one said
  nothing, and the name was trusted: the package was reported exported from
  the copy's file and never exported, `--force` and `--refresh` wrote it
  over the copy, and `-ae -ar` wrote its highlights into the copy's
  archive. The same where the package is the one with no identifier. Now
  the package claims its marked or numbered name, or in skip mode is a
  collision. `CopiesUnidentified` removes each book with its archive: with
  the copy's archive left behind once the copy leaves the library, the
  package takes the name and is reported exported from it, the
  `Unidentifiable` limit. `tests/test_copy_keeping.py` replays it against
  the CLI.

- **`NumberedRemovalsCrowd`** is the limit of that rule: with three
  packages and no usable identifier, once the first leaves, two books still
  want the plain name, and nothing says which numbered file is whose. The
  last takes the second's number, and its own archive is an orphan.

- **`NumberedLeftBehindLoose`** is that rule before `AskNumbered`. It asked
  nothing of the file: a book `Dune (1965)` with a real identifier was
  deleted from the library and its archive `Dune (1965).epub` stayed, and a
  book `Dune` declaring no usable identifier, added since, kept that file as
  its own number. It was reported exported from it and never written, and
  the deleted book's archive, maybe its last copy, was not listed as an
  orphan. Now a book with no usable identifier keeps the one numbered file
  only when the file declares none either; a file that declares one is
  another book's. `NumberedLeftBehind` holds with it, and
  `tests/test_numbered_names.py::TestABookWithNoIdentifier` replays it
  against the CLI under the default policy and `--name-by author-title`.

- **`NamesakeAddedLoose`** is that rule before `KeepShared`. Identifiers
  were read only for a name with numbered files on the shelf, so when
  `b/Dune.epub`, exported alone, was joined by `a/Dune.epub`, which sorts
  first, nothing was read: the newcomer took the plain name, was reported
  exported from the other book's archive and never written, and the other
  was written again under a number. Named from the folder in suffix mode,
  that was the `FolderNamedReports` limit. With a rename by case, which the
  model does not describe, it did not settle either: `b/Cafe.epub` renamed to
  `b/cAFE.epub` and `a/Cafe.epub` added under the old spelling, the renamed
  book was written again as `cAFE (2).epub`, and the next run, which found
  that number and read the identifiers, wrote the newcomer as
  `Cafe (3).epub` and left the second copy an orphan. Now two packages whose
  names are one file on the shelf read their identifiers, and the one the
  file declares keeps it. `NamesakeAdded` holds with it;
  `tests/test_numbered_names.py` and `tests/test_case_namesakes.py` replay
  both against the CLI. Skip mode is unchanged: the `FolderNamedReports`
  limit.

  Under `--skip-incomplete` the renamed book, evicted by iCloud, is left
  unopened and keeps nothing, and the newcomer, whose identifier and the
  file's had been read and differ, was reported exported from the renamed
  book's archive; with `--refresh`, a newcomer declaring no identifier wrote
  over it, the evicted book's only one. Now a book whose plain file declares
  a usable identifier it does not have, its own read, is refused that file:
  with no digest to move on to it claims its name numbered, and the renamed
  book, which nothing read, is trusted with the file.
  `tests/test_case_namesakes.py` replays it against the CLI.

Under `--name-by author-title` the check adds no reads on the source side,
because naming already read every package document.

What the model does not describe, and why:

- **Case folding and Unicode normalization.** Names are strings compared
  exactly, as `PassthroughNaming.identity` compares them, while the shelf is
  looked up by `filesystem_key`, which folds both. Modelling the fold means
  two name spaces and a map between them, for a state space that already
  takes minutes; so the rule that exists only because of it -- a book renamed
  by case finds its own archive (`holders.foreign`) -- is replayed against
  the CLI in `tests/test_case_namesakes.py`.
- **`--force`**, which writes over a book's own archive as `--refresh` does
  for a newer source, and is checked before the write the same way.
- **Two different files of one size and one modification time.** A copy's
  own bytes are told exactly here; the code tells them by size and
  modification time, which a copy keeps from its source
  (`tests/test_copy_keeping.py`). A copy made before copies kept the time
  is newer than its source, and is told by the identifiers where both
  declare one, and by its size where not.
- **`claim_order` for packages.** It puts every package whose first name is a
  file on the shelf ahead of the rest -- whatever put the file there: a
  namesake by case, the book's own archive or another's, or a title that
  looks like a number -- and the model's packages claim in sorted order. The
  copies claim in its order. A book titled like a number and renamed by
  case, `c/Dune (2).epub` to `c/dune (2).epub`, no longer claims first, and
  the second of two books `Dune` added since took its file as a number;
  `claims.kept_numbers` now reads the identifiers where a book's own name
  looks like a number of a name another book wants, and the book the file
  declares keeps it (`tests/test_numbered_names.py`).
