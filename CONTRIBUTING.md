# Contributing

## Running the checks

CI runs five steps. Run all five before pushing; the first four are fast.

```bash
ruff check epubconvert tests
ruff format --check epubconvert tests
mypy
pylint epubconvert tests
pytest
```

`pylint` has no score threshold. It exits non-zero on any message, which is
deliberate: it used to run with `--fail-under=8.0` against a tree scoring
10.00, so a regression to 8.1 merged green.

## Two invariants worth knowing before you change anything

### The output directory is the only record of completed work

There is no state file. Whether a book has been converted is decided by
globbing `*.epub` in the output directory and recomputing each name's identity.
Everything follows from that:

- A name the glob cannot match means the book is re-converted on every run,
  for ever. A title of `?` once produced a file called `epub`.
- Anything that lands in the output directory is recorded as finished and no
  rerun retries it. A truncated archive is permanent.

`archive.assert_is_a_book` is the choke point. It runs before every
`partial.replace()` and refuses an archive that lacks `META-INF/container.xml`
or holds no members. Every silent-success defect this project has had ended
there: an unreadable subdirectory that contributed nothing, a package deleted
between planning and writing, a symlinked directory holding somebody else's
files. In each case the run wrote a valid zip, reported an export, and recorded
a book that was wrong or missing. `--validate` is the thorough check and it is
off by default, so the cheap unconditional assertion is what actually holds the
line.

If you add a path that writes into the output directory, it goes through that
function.

### A name from a book is input, not fact

`container.xml` names the package document. The package document names every
manifest item. The package directory names its own entries. All of those are
chosen by whoever produced or sideloaded the book, and all of them get joined
onto a real directory.

`epubconvert/contained.py` holds the rule, and a test asserts nothing
reimplements it. This is not a style preference. The rule was written three
times: a traversal through a manifest href was fixed at the one call site that
had it, the archive writer later grew a weaker version of its own, and the
readers of `container.xml`, the package document and `encryption.xml` grew
none — so a book could still point its own package document at any file the
user could read, months after the "same" bug was closed.

Names shown to a user go through `epubconvert/display.py`, which escapes
control characters. A package name carrying `ESC[2K` can otherwise erase the
line reporting it.

### Exit codes are a contract

`epubconvert/exits.py` holds every code with its meaning, and the README table
is generated from `MEANINGS`, so the two cannot drift. A new failure mode gets
a new code rather than reusing a near-enough one: five conditions once shared
`2`, and a scheduled run could not tell a typo from a missing dependency.

Environment checks — does this directory exist, is that tool installed — go in
`run.main`, not in `parse_args`. `parser.error` always exits 2, so validating
the environment there is what collapsed them in the first place.

## Measure before tuning

Two of this project's performance assumptions were wrong, and both had been
sitting in the code unquestioned:

- `COMPRESS_LEVEL = 9` had never been applied. `ZipFile(compresslevel=)` is
  consulted only when `open()` builds its own `ZipInfo`, and this code hands it
  a prebuilt one, so every member had always deflated at zlib's default.
  Applying the constant would have cost 3.2× the CPU for 0.6% less size.
- `--validate` was assumed to double the read work, because it re-inflates
  every member. It costs about 7%: the archive is still in the page cache, and
  inflating is far cheaper than deflating.

Neither was discoverable by reading. If you change a tuning constant, a worker
count, or a compression choice, record the measurement in a comment next to it
and say what you measured on. The constants in `archive.py` and
`convert.default_workers` carry theirs.

## Style

Docstrings explain *why*, and often cite the defect that motivated a decision.
That is intentional and worth keeping. When you move code, re-read the
docstrings that moved with it — four of them were left pointing at functions
that had relocated.
