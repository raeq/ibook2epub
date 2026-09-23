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
