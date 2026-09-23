--------------------------- MODULE OutputProtocol ---------------------------
(*
 * The protocol ibook2epub follows in its output directory.
 *
 * The output directory is the only record of completed work (CONTRIBUTING.md):
 * a *.epub there is taken as a finished book by every later run. So it must
 * only ever hold complete books, a run must never delete another live run's
 * in-flight temporary, and temporaries a killed run leaves behind must still
 * be cleaned up.
 *
 * Modelled from:
 *   epubconvert/run/convert.py    output_lock, sweep_partials,
 *                                 STALE_PARTIAL_SECONDS
 *   epubconvert/run/run.py        `if locked and not args.dry_run:
 *                                 sweep_partials(...)`
 *   epubconvert/export/archive.py zip_package: mkstemp a temporary, write
 *                                 it, assert_is_a_book, replace; unlink it on
 *                                 any ordinary failure, Ctrl-C included
 *
 * Time is abstracted to one fact per temporary: whether it has gone
 * unmodified for STALE_PARTIAL_SECONDS. A live run touches its temporary far
 * more often than that (see the constant's comment), so only an abandoned
 * temporary goes stale -- unless LiveMayStall says otherwise.
 *
 * README.md lists the configurations and what each is expected to show.
 *)
EXTENDS Naturals

CONSTANTS
    Runs,          \* concurrent invocations: cron, launchd, a user at a shell
    Books,         \* packages in the library
    LockWorks,     \* the runs for which flock() works on the output directory
    NoRun,         \* a model value naming no run
    AgeGuard,      \* the sweep takes only stale temporaries
    LiveMayStall,  \* a live run can leave its temporary alone past the age
    MaxKills       \* SIGKILLs in one behaviour, so the state space is finite

VARIABLES
    pc,            \* each run's phase
    holder,        \* the run holding the flock, or NoRun
    locked,        \* whether a run believes it has exclusive access
    partials,      \* temporaries in the output directory
    books,         \* *.epub files in the output directory
    kills,         \* SIGKILLs so far
    lost           \* history: a sweep deleted a live run's temporary

vars == <<pc, holder, locked, partials, books, kills, lost>>

\* A temporary: its writer, its book, whether every member is written, whether
\* its writer is still running, and whether it has gone unmodified long enough
\* to look abandoned.
Partial == [run : Runs, book : Books, complete : BOOLEAN,
            live : BOOLEAN, stale : BOOLEAN]

Phases == {"idle", "sweep", "work", "writing", "done", "refused", "dead"}

TypeOK ==
    /\ pc \in [Runs -> Phases]
    /\ holder \in Runs \cup {NoRun}
    /\ locked \in [Runs -> BOOLEAN]
    /\ partials \subseteq Partial
    /\ books \subseteq [book : Books, complete : BOOLEAN]
    /\ kills \in 0..MaxKills
    /\ lost \in BOOLEAN

Init ==
    /\ pc = [r \in Runs |-> "idle"]
    /\ holder = NoRun
    /\ locked = [r \in Runs |-> FALSE]
    /\ partials = {}
    /\ books = {}
    /\ kills = 0
    /\ lost = FALSE

\* The temporary a run is writing now; a dead run's are no longer its.
Mine(r) == {p \in partials : p.run = r /\ p.live}

\* What a killed run left behind.
Abandoned == {p \in partials : ~p.live}

Release(r) ==
    /\ holder' = IF holder = r THEN NoRun ELSE holder
    /\ locked' = [locked EXCEPT ![r] = FALSE]

(* output_lock: flock(LOCK_EX | LOCK_NB). Contended: OutputLockedError and the
   run exits. An errno outside _CONTENDED (ENOTSUP, ENOLCK): warn and carry on
   unlocked, yielding False. *)
Start(r) ==
    /\ pc[r] = "idle"
    /\ IF r \in LockWorks
         THEN IF holder = NoRun
                THEN /\ holder' = r
                     /\ locked' = [locked EXCEPT ![r] = TRUE]
                     /\ pc' = [pc EXCEPT ![r] = "sweep"]
                ELSE /\ pc' = [pc EXCEPT ![r] = "refused"]
                     /\ UNCHANGED <<holder, locked>>
         ELSE /\ pc' = [pc EXCEPT ![r] = "work"]
              /\ UNCHANGED <<holder, locked>>
    /\ UNCHANGED <<partials, books, kills, lost>>

(* run.py sweeps only when locked. sweep_partials globs every temporary,
   whoever made it, and with the guard removes only the stale ones. *)
Sweep(r) ==
    LET swept == IF AgeGuard THEN {p \in partials : p.stale} ELSE partials
    IN /\ pc[r] = "sweep"
       /\ locked[r]
       /\ lost' = (lost \/ \E p \in swept : p.live)
       /\ partials' = partials \ swept
       /\ pc' = [pc EXCEPT ![r] = "work"]
       /\ UNCHANGED <<holder, locked, books, kills>>

(* plan_exports: a book whose *.epub is present is not pending.
   zip_package: mkstemp a temporary in the output directory. *)
BeginBook(r, b) ==
    /\ pc[r] = "work"
    /\ ~\E e \in books : e.book = b
    /\ partials' = partials \cup
         {[run |-> r, book |-> b, complete |-> FALSE, live |-> TRUE,
           stale |-> FALSE]}
    /\ pc' = [pc EXCEPT ![r] = "writing"]
    /\ UNCHANGED <<holder, locked, books, kills, lost>>

(* Every member written and assert_is_a_book satisfied. Writing touches it. *)
Complete(r) ==
    /\ pc[r] = "writing"
    /\ \E p \in Mine(r) :
         /\ ~p.complete
         /\ partials' = (partials \ {p}) \cup
              {[p EXCEPT !.complete = TRUE, !.stale = FALSE]}
    /\ UNCHANGED <<pc, holder, locked, books, kills, lost>>

(* partial.replace(target_archive), atomic. A swept temporary makes replace
   raise FileNotFoundError: the book fails and is pending on the next run. *)
Replace(r) ==
    /\ pc[r] = "writing"
    /\ IF \E p \in Mine(r) : p.complete
         THEN \E p \in Mine(r) :
                /\ p.complete
                /\ books' = {e \in books : e.book # p.book}
                              \cup {[book |-> p.book, complete |-> p.complete]}
                /\ partials' = partials \ {p}
         ELSE /\ Mine(r) = {}
              /\ UNCHANGED <<books, partials>>
    /\ pc' = [pc EXCEPT ![r] = "work"]
    /\ UNCHANGED <<holder, locked, kills, lost>>

(* Any ordinary exception, Ctrl-C included: `except BaseException: unlink`. *)
Fail(r) ==
    /\ pc[r] = "writing"
    /\ partials' = partials \ Mine(r)
    /\ pc' = [pc EXCEPT ![r] = "work"]
    /\ UNCHANGED <<holder, locked, books, kills, lost>>

(* SIGKILL or power loss: no handler runs, so the temporary stays behind; the
   kernel releases the flock. *)
Kill(r) ==
    /\ pc[r] \in {"sweep", "work", "writing"}
    /\ kills < MaxKills
    /\ kills' = kills + 1
    /\ pc' = [pc EXCEPT ![r] = "dead"]
    /\ partials' = {IF p.run = r /\ p.live THEN [p EXCEPT !.live = FALSE]
                                           ELSE p : p \in partials}
    /\ Release(r)
    /\ UNCHANGED <<books, lost>>

Finish(r) ==
    /\ pc[r] = "work"
    /\ pc' = [pc EXCEPT ![r] = "done"]
    /\ Release(r)
    /\ UNCHANGED <<partials, books, kills, lost>>

\* The next invocation: cron fires again, or the user reruns.
Restart(r) ==
    /\ pc[r] \in {"done", "refused", "dead"}
    /\ pc' = [pc EXCEPT ![r] = "idle"]
    /\ UNCHANGED <<holder, locked, partials, books, kills, lost>>

(* Time passes: a temporary nobody touches goes stale. A live run's does too
   only if it can stall past STALE_PARTIAL_SECONDS. *)
Age ==
    \E p \in partials :
        /\ ~p.stale
        /\ (~p.live \/ LiveMayStall)
        /\ partials' = (partials \ {p}) \cup {[p EXCEPT !.stale = TRUE]}
        /\ UNCHANGED <<pc, holder, locked, books, kills, lost>>

Next ==
    \/ Age
    \/ \E r \in Runs :
         \/ Start(r) \/ Sweep(r) \/ Complete(r) \/ Replace(r)
         \/ Fail(r) \/ Kill(r) \/ Finish(r) \/ Restart(r)
         \/ \E b \in Books : BeginBook(r, b)

(* Runs keep being invoked and get on with it; failures and kills are the
   environment's, and carry no fairness. A run that keeps failing books still
   finishes eventually. *)
Fairness ==
    /\ WF_vars(Age)
    /\ \A r \in Runs :
         /\ WF_vars(Start(r)) /\ WF_vars(Sweep(r)) /\ WF_vars(Restart(r))
         /\ WF_vars(Complete(r)) /\ WF_vars(Replace(r))
         /\ SF_vars(Finish(r))

Spec == Init /\ [][Next]_vars /\ Fairness

-----------------------------------------------------------------------------
(* Safety *)

\* Every *.epub in the output directory is a complete book.
OnlyCompleteBooks == \A e \in books : e.complete

\* No sweep deletes a temporary its writer is still writing.
NoLiveWorkSwept == ~lost

\* At most one run believes it has exclusive access.
MutualExclusion == \A r1, r2 \in Runs : (locked[r1] /\ locked[r2]) => r1 = r2

(* Liveness *)

\* What a killed run leaves behind does not stay forever.
AbandonedCleanedUp == <>[](Abandoned = {})
=============================================================================
