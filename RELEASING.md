# Releasing

Publishing to PyPI cannot be undone. A version number is spent the moment it
uploads: 2.0.4 can never be re-uploaded, even to fix a file that was wrong. So
most of what follows is checking, and the irreversible step is one command near
the end.

## What a release touches

| Where | What changes |
|---|---|
| `pyproject.toml` | `version` |
| `epubconvert/__init__.py` | `__version__` |
| `CHANGELOG.md` | `## [Unreleased]` becomes `## [x.y.z] - DATE`, and the compare link is repointed |
| git | an annotated tag `vx.y.z` |
| GitHub | a Release created from that tag |
| PyPI | the sdist and wheel, uploaded by `publish.yml` |
| Homebrew | the tap's formula, updated by its own scheduled job |

Nothing else. There is no version string in the README, no separate lockfile to
regenerate, and the exit-code table is generated from `exits.MEANINGS`.

## Before you start

The working tree must be clean and `main` must be pushed and green. A release
is cut from a tag, and a tag can be cut from anything — including a commit
whose CI never ran.

Run the five gates the way CI runs them, and judge each by its **exit code**.
Reading the output has produced a false pass here before: `pylint` printed
"10.00/10" while exiting non-zero.

```bash
ruff check epubconvert tests
ruff format --check epubconvert tests
mypy
pylint epubconvert tests
pytest
pytest --cov          # the coverage gate, currently 97%
```

`mypy` takes no argument on purpose. It is configured to check `epubconvert`
**and** `tests` together; narrowing it to the package hides errors in the tests
and has pushed a red `main`.

## Choosing the number

Patch for fixes, minor for additions, major for a break — with one wrinkle
worth naming. A fix that refuses input the previous version accepted is still
a patch **if nothing real is refused**, and that is a claim to check rather than
assume. When 2.0.3 began rejecting XML entity declarations, the check was to
parse all 2,804 package documents in a real library and confirm none declared
one.

## Cutting it

**1. Bump both version files.** They must agree; `publish.yml` refuses a tag
that does not match `pyproject.toml`, and a test asserts `__version__` equals
the installed distribution's metadata.

That test fails locally until the editable install is refreshed, which catches
people out:

```bash
pip install -e . --no-deps
```

**2. Promote the changelog.** Rename `## [Unreleased]` to `## [x.y.z] - DATE`
and repoint the link at the bottom to `compare/vPREV...vx.y.z`. If there is no
`## [Unreleased]` section, the work was not written down when it was done — go
and write it before releasing.

**3. Run the gates again**, then commit and push:

```bash
git commit -F /tmp/commit-msg.txt      # a file, not a HEREDOC
git push origin main
```

**4. Wait for CI to say `completed`.** Not `queued`, not `in_progress`.
Reporting a green build that was still queued has happened here, and `main` was
red for two pushes afterwards.

```bash
gh run list --branch main --limit 1 \
  --json status,conclusion,headSha \
  -q '.[] | "\(.status) \(.conclusion // "-") \(.headSha[0:7])"'
```

**5. Tag it.** Annotated, with a message that says what changed and why — the
tag is what someone lands on from a compare view.

```bash
git tag -a vx.y.z -F /tmp/tag-msg.txt
git push origin vx.y.z
```

Pushing a tag does **not** publish anything. `publish.yml` fires on
`release: published`, so there is still time to check.

## Pre-flight

Everything here is cheap, and each of these has caught something real.

```bash
# The three refs agree
git rev-parse main; git rev-parse vx.y.z^{}; git ls-remote origin main

# The tagged tree carries the number you think it does
git show vx.y.z:pyproject.toml | grep '^version'

# PyPI does not already have it
curl -s https://pypi.org/pypi/ibook2epub/json \
  | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)["releases"]))'

# Both artefacts build and pass
python3 -m build && python3 -m twine check dist/*
```

Then install the wheel into a throwaway virtualenv and **exercise the thing the
release is for**, from outside the repository. Inside it, Python resolves
`epubconvert` from the working tree and the test proves nothing about the
artefact:

```bash
python3 -m venv /tmp/check
/tmp/check/bin/pip install dist/ibook2epub-x.y.z-py3-none-any.whl
cd /tmp && /tmp/check/bin/ibook2epub --version
```

## The irreversible step

Creating the Release triggers `publish.yml`.

```bash
gh release create vx.y.z \
  --title "ibook2epub x.y.z" \
  --notes-file /tmp/notes.md \
  --verify-tag
```

`--verify-tag` refuses to invent a tag that does not exist.

The workflow then does three things in order. It re-checks the tag against
`pyproject.toml`; it **runs the whole suite again** against the tagged tree,
because a release can be cut from a commit CI never saw; and it builds and
uploads.

The upload job waits on the `pypi` environment, which is configured with a
**15-minute timer** and a branch policy of `tag: v*`. `waiting` is that timer,
not a fault. Publishing uses trusted publishing over OIDC, so no API token
exists anywhere.

## Afterwards

Verify against PyPI rather than the green check, by upgrading the way a user
would:

```bash
python3 -m venv /tmp/after
/tmp/after/bin/pip install "ibook2epub==PREV"
/tmp/after/bin/pip install --upgrade ibook2epub
/tmp/after/bin/ibook2epub --version
```

The JSON API lags the index by a few minutes after every upload. If
`pypi.org/pypi/ibook2epub/json` still reports the previous version while `pip`
installs the new one, that is the cache, not a failure. The Simple index is
what `pip` resolves against:

```bash
curl -s -H "Accept: application/vnd.pypi.simple.v1+json" \
  https://pypi.org/simple/ibook2epub/ \
  | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)["versions"]))'
```

## Homebrew

Nothing to do. The formula lives in [raeq/homebrew-tap][tap], not in
homebrew-core, and that repository keeps itself current: a scheduled job polls
PyPI daily, rewrites `url` and `sha256`, and audits, builds and runs the
formula's test block **before** committing.

Verifying before the commit rather than after is deliberate. A push made by a
workflow does not trigger another workflow, so the tap's CI would never see a
bump commit — checking first is the only point at which a bad bump can be
stopped before it reaches a `brew update`.

To publish sooner than the next poll, run it by hand:

```bash
gh workflow run bump.yml --repo raeq/homebrew-tap
```

The formula's test block converts a real package directory and runs `--verify`
over the result, so a release that cannot convert fails in the tap rather than
on somebody's shelf. If that job goes red, the release is still on PyPI and the
formula simply stays a version behind until it is fixed — which is the right
way round.

## If something is wrong after publishing

You cannot replace the files. The options are to yank the release on PyPI,
which hides it from new installs without removing it, or to ship the next patch
version. Shipping forwards is almost always better: a yanked version still
resolves for anyone who pinned it, and the fix is what people actually need.

Do not move a tag that has been released. Moving one before release is fine and
has been done here; moving one afterwards leaves the artefact on PyPI pointing
at a commit that no longer exists.

[tap]: https://github.com/raeq/homebrew-tap
