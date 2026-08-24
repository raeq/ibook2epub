# Code tours

Six guided walks through this codebase, for the [CodeTour][ct] extension in VS Code.

Install the extension, then open the **CodeTour** panel in the Explorer sidebar. Each
tour appears there; click one to start, and use the arrows in the notification to move
between steps. Nothing here changes the code — a tour is a list of file positions with
commentary attached.

| Tour | What it covers |
|---|---|
| 1. The Round Trip | One book from `python -m epubconvert` to an archive on disk. Start here. |
| 2. Why a Rerun Is Safe | The no-state-file invariant, and every rule that keeps it true. |
| 3. Trust Nothing You Read | DRM detection, path containment, and what a book's own manifest is allowed to make the tool do. |
| 4. Names, Identity and Collisions | Filename sanitisation, the three naming policies, collision suffixes. |
| 5. Threads, Ctrl-C and the Log | The thread pool, interrupt handling, and logging setup. |
| 6. The Test Suite and the Toolchain | Where each behaviour is tested, and what CI enforces. |

They chain: finishing one offers the next.

## Keeping them working

Every step carries both a `line` and a `pattern`. CodeTour prefers the pattern, so a step
follows its anchor when surrounding code moves; the line number is the fallback. If you
move or rename an anchor, update the step that points at it. The script that checks all
101 anchors resolve to their declared lines is short enough to rewrite in a minute:
read each `.tour` file, `re.search` the pattern against the target file, compare the
resulting line number to the declared one.

[ct]: https://marketplace.visualstudio.com/items?itemName=vsls-contrib.codetour
