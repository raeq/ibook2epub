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
gets right over a sequence of runs while the library changes. It checks two
properties:

- **ExportedMeansTheBooksOwnFile:** a book the planner reports as exported is
  the book held in the file it points at.
- **NeverWritesOverAnotherBook:** no run replaces another book's archive.

Names are strings, so a title that looks like a suffix (`Dune (2)`) collides
exactly as it does on disk. The model has 3 editions sharing one title, as
they do under `--name-by author-title`, and runs that can be stopped
partway. `VerifyHolder` switches on `_decide_against_holder`, which reads
the identifier of the archive already on the shelf and reports a collision
when it and the book's own identifier are both usable and differ.

| Configuration | Runs | Library | Identifiers | Check | Outcome |
|---|---|---|---|---|---|
| `Stable` | whole library, `--refresh` | fixed | none | on | both hold |
| `StableSuffix` | the same, suffix mode, look-alike title | fixed | two | on | both hold |
| `Changing` | `--match`, `--refresh` | books added and removed | all | on | both hold |
| `ChangingSuffix` | the same, suffix mode | books added and removed | all | on | both hold |
| `MatchUnverified` | `--match` | fixed | all | off | **ExportedMeansTheBooksOwnFile violated** |
| `ChangesUnverified` | whole library | books added and removed | all | off | **ExportedMeansTheBooksOwnFile violated** |
| `RefreshUnverified` | whole library, `--refresh` | books added and removed | all | off | **NeverWritesOverAnotherBook violated** |
| `Unidentifiable` | `--match`, `--refresh` | books added and removed | book 1 only | on | **ExportedMeansTheBooksOwnFile violated** |

What the configurations that fail show:

- **The `Unverified` rows** are the three defects the check fixes. Each one
  reproduces with the code before the check. The setup is three packages
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

The check runs only under a naming policy that already reads each source's
package document (`--name-by author-title`), so it adds no reads on the
source side. Under the default policy, names come from the package folder
names, which are unique within a library.
