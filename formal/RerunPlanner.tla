---------------------------- MODULE RerunPlanner ----------------------------
(*
 * How the planner names books, and how a run reads the shelf back, across a
 * sequence of runs while the library changes.
 *
 * There is no state file. A run recognises a book as already exported by
 * recomputing the book's name and finding a file of that name on the shelf
 * (CONTRIBUTING.md: "The output directory is the only record of completed
 * work"). This model checks what that inference gets right.
 *
 * Modelled from epubconvert/run/planning.py, with claims.py (the candidate
 * names) and placing.py (where a book is on the shelf):
 *   assign_names / _wanted_names / _assign_one / _claim   naming, in sorted
 *       order, with collisions settled by suffix or by losing
 *   _stable_base / marked       a crowded book with a usable dc:identifier is
 *                               marked with a digest of it, when naming read
 *                               the package document
 *   suffixed                    " (2)", " (3)", ...
 *   plan_exports / _decide / _decide_against_existing    EXPORTED when a
 *       file of the book's name exists, PENDING otherwise; --refresh writes
 *       over that file when the source is newer
 *   _decide_against_holder      COLLISION when that file's usable identifier
 *       and the book's differ; with either unusable, the name is trusted.
 *       A policy that names from the folder reads no package document, so
 *       _decide reads the source's identifier only for a book about to be
 *       written over an archive, and a book reported exported is not checked
 *   place                       under suffix, a book whose name holds another
 *       book moves on to the first position of its marked name that no other
 *       book of the run is named and no other book's archive holds
 * and epubconvert/run/run.py (_shared_names): every run names the whole
 * library, and one narrowed by --match plans only the books it selected. It
 * used to name the selection alone, so a matched book could get a different
 * name from the one a full run gives it.
 *
 * Files copied through -- PDFs and books that arrived already zipped -- are
 * the books in Copies, modelled from copynames.py and copying.py:
 *   claim_copies / _Claiming   after every package, each copy takes the
 *       first free position of the name it wants. With KeepOwn, a copy whose
 *       own file is under a name another book holds keeps it whatever the
 *       mode, and a copy with its own file under its name claims first. A
 *       copy that lost its name keeps a file of its own under it (_lost)
 *       either way. A copy has no digest marker: in suffix mode it moves on
 *       to its own name, numbered
 *   _identified                a package's identifier is read when a copy
 *       wants its name, whatever the policy
 *   _own                       a copy's identifier is read for a file under
 *       its name that is not its own, and with KeepOwn also where another
 *       book wants that name. A copy's own file is told exactly here: the
 *       size test, and the identifiers where the name is contested, stand in
 *       for that
 *   copy_through_all           a copy whose target exists is not copied
 * KeepOwn stands for the claim-pass fixes together: with it, _assign_one
 * also gives a package with no digest marker somewhere to move on to in
 * suffix mode, its own name numbered, as a copy has.
 *
 * find_orphans is modelled too: an archive on the shelf is an orphan when no
 * book is placed at it, no book that lost its name may hold it, and it is not
 * under a name the plan gave while holding a book whose identifier was read.
 *
 * Not modelled:
 *   - case folding and Unicode normalization. Names are strings compared
 *     exactly: PassthroughNaming.identity is the filename. So a book renamed
 *     by case (holders.foreign), and claim_order's rule that a book whose
 *     exact name is on the shelf claims first -- which reorders only books
 *     whose names differ in case, or a copy whose own file is there -- are
 *     left to tests/test_case_namesakes.py; the copies' own-file-first order
 *     is modelled.
 *   - --force, which writes as --refresh does for a newer source.
 *   - two different copies of one size: the identifier comparison that tells
 *     them apart is taken as given (tests/test_copy_shelf.py).
 * A digest marker is " [b]" for book b; a real digest is a hash of the
 * identifier, equal for equal identifiers.
 *)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
    N,             \* books; book b sorts before book b + 1
    Wanted,        \* the name each book's policy asks for, [1..N -> STRING]
    Usable,        \* books with a usable, unique dc:identifier
    OnCollision,   \* "skip" (the default) or "suffix"
    AllowMatch,    \* runs may be narrowed with --match
    AllowRefresh,  \* runs may pass --refresh
    AllowChanges,  \* books may be added to the library, which may start
                   \* with any of them
    AllowRemovals, \* with AllowChanges, books may be removed from it too
    VerifyHolder,  \* _decide_against_holder: the check that fixes the defects
    ReadsSources,  \* naming reads each package document (--name-by author-title);
                   \* otherwise the check runs only before a write
    MoveOn,        \* place: under suffix, a book whose name holds another book
                   \* moves on to its marked name
    Copies,        \* books copied through rather than converted
    KeepOwn        \* the claim-pass fixes: a copy keeps its own file whatever
                   \* the mode, and a package without a digest can move on

Books == 1..N

VARIABLES
    lib,           \* books in the library
    shelf,         \* name -> the book whose archive has that name, or 0
    misreported,   \* history: a run called a book exported by a file of another
    clobbered,     \* history: a run wrote over another book's archive
    stranded,      \* history: a suffix run left an identifiable book unexported
                   \* because another book's archive held its name
    orphaned,      \* history: a run reported as an orphan the archive of a
                   \* book in the library
    last           \* the last run: what it selected and decided, for traces

vars == <<lib, shelf, misreported, clobbered, stranded, orphaned, last>>

-----------------------------------------------------------------------------
(* Naming: assign_names, then claim_copies *)

\* MAX_SUFFIX is 99; N positions always leave one free for N books to claim,
\* and 2N one for a book moving on past every name the others hold.
Limit == IF OnCollision = "suffix" THEN N ELSE 1
MoveLimit == IF OnCollision = "suffix" THEN 2 * N ELSE 1

Suffixed(base, k) == IF k = 1 THEN base ELSE base \o " (" \o ToString(k) \o ")"
Marked(name, b) == name \o " [" \o ToString(b) \o "]"

Min(T) == CHOOSE k \in T : \A j \in T : k <= j

Packages(X) == X \ Copies
CopiesOf(X) == X \cap Copies

Crowd(S, want) == Cardinality({c \in S : Wanted[c] = want})

\* _stable_base: a digest of the identifier, which naming has only when the
\* policy read the package document.
Digested(b) == ReadsSources /\ b \in Usable

\* _assign_one's base: marked only when crowded and identifiable.
Base(S, b) ==
    IF OnCollision = "suffix" /\ Crowd(S, Wanted[b]) > 1 /\ Digested(b)
      THEN Marked(Wanted[b], b)
      ELSE Wanted[b]

RECURSIVE Sorted(_)
Sorted(S) ==
    IF S = {} THEN <<>>
    ELSE LET m == CHOOSE x \in S : \A y \in S : x <= y
         IN <<m>> \o Sorted(S \ {m})

(* _claim: the first free candidate, in sorted order of packages. "" means
   the book lost (COLLISION). *)
RECURSIVE Claim(_, _, _)
Claim(S, order, claimed) ==
    IF order = <<>> THEN [b \in {} |-> ""]
    ELSE LET b    == Head(order)
             base == Base(S, b)
             free == {k \in 1..Limit : Suffixed(base, k) \notin claimed}
             name == IF free = {} THEN "" ELSE Suffixed(base, Min(free))
             rest == Claim(S, Tail(order),
                           IF name = "" THEN claimed ELSE claimed \cup {name})
         IN [c \in {b} \cup DOMAIN rest |-> IF c = b THEN name ELSE rest[c]]

\* The positions of c's name under which the shelf holds c's own file:
\* _Claiming.kept, which looks at them all only in suffix mode.
OwnAt(c) == {k \in 1..MoveLimit : shelf[Suffixed(Wanted[c], k)] = c}

(* _Claiming.name: with KeepOwn, a copy whose own file is under its name or
   one of its numbers keeps it, whoever holds the name. Otherwise the first
   free position of its name; with none free, a file of its own under the
   name it wanted (_Claiming._lost, before KeepOwn), or nothing. *)
RECURSIVE CopyClaim(_, _)
CopyClaim(order, claimed) ==
    IF order = <<>> THEN [b \in {} |-> ""]
    ELSE LET c    == Head(order)
             want == Wanted[c]
             keep == KeepOwn /\ OwnAt(c) # {}
             free == {k \in 1..Limit : Suffixed(want, k) \notin claimed}
             name == IF keep THEN Suffixed(want, Min(OwnAt(c)))
                     ELSE IF free # {} THEN Suffixed(want, Min(free))
                     ELSE IF shelf[want] = c THEN want
                     ELSE ""
             took == IF keep THEN TRUE ELSE free # {}
             rest == CopyClaim(Tail(order),
                               IF took THEN claimed \cup {name} ELSE claimed)
         IN [x \in {c} \cup DOMAIN rest |-> IF x = c THEN name ELSE rest[x]]

\* With KeepOwn, a copy whose own file is under its name or one of its
\* numbers claims first, then one whose name holds a file (claim_order), then
\* the rest, each sorted.
CopyOrder(C) ==
    IF KeepOwn
      THEN Sorted({c \in C : OwnAt(c) # {}})
           \o Sorted({c \in C : OwnAt(c) = {} /\ shelf[Wanted[c]] # 0})
           \o Sorted({c \in C : OwnAt(c) = {} /\ shelf[Wanted[c]] = 0})
      ELSE Sorted(C)

\* Every book's name: the packages', then the copies' claimed after them.
Assigned(L) ==
    LET pkgs   == Claim(Packages(L), Sorted(Packages(L)), {})
        copies == CopyClaim(CopyOrder(CopiesOf(L)),
                            {pkgs[b] : b \in Packages(L)} \ {""})
    IN [b \in L |-> IF b \in Copies THEN copies[b] ELSE pkgs[b]]

\* The archive named n holds another book: its identifier and b's are both
\* usable and differ (holds_another_book).
Holds(b, n) == /\ VerifyHolder
               /\ b \in Usable
               /\ shelf[n] \in Usable
               /\ shelf[n] # b

(* Whose identifier the plan has read, given every book's name asg. A
   package's when naming read it, or a copy wants its name (_identified). A
   copy's where a file under a name it wants is not its own, or with KeepOwn
   where another book wants that name too (_Claiming._own). *)
Contested(L, asg, c) ==
    \E d \in L \ {c} : IF d \in Copies THEN Wanted[d] = Wanted[c]
                                       ELSE asg[d] = Wanted[c]
Known(L, asg) ==
    {b \in Packages(L) :
        ReadsSources \/ \E c \in CopiesOf(L) : Wanted[c] = asg[b]}
    \cup
    {c \in CopiesOf(L) :
        \E n \in {Wanted[c], asg[c]} \ {""} :
            /\ shelf[n] # 0
            /\ shelf[n] # c \/ (KeepOwn /\ Contested(L, asg, c))}

\* Where a book whose name holds another book moves on to: its digest-marked
\* name; with KeepOwn and no digest, its own name, numbered. A copy's is
\* always its own name, numbered.
Target(b) ==
    IF b \in Copies THEN Wanted[b]
    ELSE IF Digested(b) THEN Marked(Wanted[b], b)
    ELSE IF KeepOwn THEN Wanted[b]
    ELSE ""

(* place, over books in order: a book stays at its name unless the file
   there holds another book whose identifier the plan read; then, under
   suffix, it takes the first position of its target that no book is named
   and no other book's archive holds. Each name moved on to is spoken for. *)
RECURSIVE Place(_, _, _, _)
Place(order, start, spoken, known) ==
    IF order = <<>> THEN [b \in {} |-> ""]
    ELSE LET b        == Head(order)
             Other(n) == shelf[n] # 0 /\ b \in known /\ Holds(b, n)
             open     == IF Target(b) = "" THEN {}
                         ELSE {k \in 1..MoveLimit :
                                 LET n == Suffixed(Target(b), k)
                                 IN n \notin spoken /\ ~Other(n)}
             final    == IF start[b] = "" THEN ""
                         ELSE IF ~Other(start[b]) THEN start[b]
                         ELSE IF MoveOn /\ OnCollision = "suffix" /\ open # {}
                           THEN Suffixed(Target(b), Min(open))
                         ELSE ""
             rest     == Place(Tail(order), start,
                               IF final \notin {"", start[b]}
                                 THEN spoken \cup {final} ELSE spoken,
                               known)
         IN [x \in {b} \cup DOMAIN rest |-> IF x = b THEN final ELSE rest[x]]

\* Every name any selection could produce: the shelf's domain.
Names ==
    {Suffixed(base, k) :
        base \in {Wanted[b] : b \in Books} \cup {Marked(Wanted[b], b) : b \in Books},
        k \in 1..MoveLimit}

-----------------------------------------------------------------------------

Init ==
    \* With changes, any library: the one before a book was added. The same
    \* states as removing books before the first run, when removals are on.
    /\ lib \in IF AllowChanges THEN SUBSET Books ELSE {Books}
    /\ shelf = [n \in Names |-> 0]
    /\ misreported = FALSE
    /\ clobbered = FALSE
    /\ stranded = FALSE
    /\ orphaned = FALSE
    /\ last = <<>>

AddBook(b) ==
    /\ AllowChanges
    /\ b \notin lib
    /\ lib' = lib \cup {b}
    /\ last' = <<"added", b>>
    /\ UNCHANGED <<shelf, misreported, clobbered, stranded, orphaned>>

RemoveBook(b) ==
    /\ AllowChanges
    /\ AllowRemovals
    /\ b \in lib
    /\ lib' = lib \ {b}
    /\ last' = <<"removed", b>>
    /\ UNCHANGED <<shelf, misreported, clobbered, stranded, orphaned>>

(* One run over the selection S. Every decision is made before anything is
   written (plan_exports, then the export). `newer` is the packages whose
   source is newer than their archive, for --refresh. `done` is the writes
   that finish: -m caps a run, and a run can be stopped.

   Each computed value is bound with \E x \in {e} rather than LET because
   TLC re-evaluates a LET definition at every reference: naming, once per
   lookup of a name. ChangingSuffix took 2m08s that way and takes 1s.

   The names are placed three times, as the code places them: every book,
   packages then copies, to settle the copies (_shared_names); the packages
   the run selected, against every name so settled (plan_exports); and every
   book again for the orphan check (find_orphans). *)
Run(S, refresh, newer, done) ==
    \E asg \in {Assigned(lib)} :
    \E known \in {Known(lib, asg)} :
    \E all \in {Place(Sorted(Packages(lib)) \o Sorted(CopiesOf(lib)), asg,
                      {asg[b] : b \in lib} \ {""}, known)} :
    \E spoken \in {({asg[b] : b \in Packages(lib)}
                     \cup {all[c] : c \in CopiesOf(lib)}) \ {""}} :
    \E planned \in {Place(Sorted(Packages(S)), asg, spoken, known)} :
    \E again \in {Place(Sorted(Packages(lib)) \o Sorted(CopiesOf(lib)),
                        [b \in lib |-> IF b \in Copies THEN all[b] ELSE asg[b]],
                        spoken, known)} :
    LET
        \* Whose archive is compared before a write: a book whose identifier
        \* the plan read, and one --refresh would write.
        Checked(b) == b \in known \/ (refresh /\ b \in newer)
        name     == [b \in S |-> IF b \in Copies THEN all[b] ELSE planned[b]]
        present  == {b \in Packages(S) : name[b] # "" /\ shelf[name[b]] # 0}
        foreign  == {b \in present : Checked(b) /\ Holds(b, name[b])}
        collided == {b \in S : name[b] = ""} \cup foreign
        ours     == present \ foreign
        rewrite  == IF refresh THEN ours \cap newer ELSE {}
        exported == ours \ rewrite
        \* copy_through_all: a copy whose target exists is taken as its own.
        kept     == {c \in CopiesOf(S) : name[c] # "" /\ shelf[name[c]] # 0}
        pending  == {b \in S : name[b] # "" /\ shelf[name[b]] = 0} \cup rewrite
        written  == pending \cap done
        \* find_orphans: the archive each book is placed at, the one a book
        \* that lost its name may hold (_held_by_loser), and one under a name
        \* the plan gave that holds a book whose identifier was read.
        Wants(b) == IF b \in Copies THEN Wanted[b] ELSE Base(Packages(lib), b)
        losers   == {b \in lib : /\ again[b] = ""
                                 /\ shelf[Wants(b)] # 0
                                 /\ ~(b \in known /\ Holds(b, Wants(b)))}
        claimed  == {again[b] : b \in lib} \cup {Wants(b) : b \in losers}
        orphans  == {n \in Names : /\ shelf[n] # 0
                                   /\ n \notin claimed
                                   /\ ~(n \in spoken /\ shelf[n] \in known \cap Usable)}
    IN /\ misreported' = (misreported \/
                           \E b \in exported \cup kept : shelf[name[b]] # b)
       /\ clobbered' = (clobbered \/
                         \E b \in written : shelf[name[b]] \notin {0, b})
       /\ stranded' = (stranded \/
                        (OnCollision = "suffix" /\ collided \cap Usable # {}))
       /\ orphaned' = (orphaned \/ \E n \in orphans : shelf[n] \in lib)
       /\ shelf' = [n \in Names |->
                      IF \E b \in written : name[b] = n
                        THEN CHOOSE b \in written : name[b] = n
                        ELSE shelf[n]]
       /\ last' = <<"ran", IF S = lib THEN "all" ELSE "--match", S,
                    IF refresh THEN "--refresh" ELSE "",
                    [b \in S |-> IF b \in collided THEN "collision"
                                ELSE IF b \in exported THEN <<"exported", name[b]>>
                                ELSE IF b \in kept THEN <<"copied", name[b]>>
                                ELSE IF b \in written THEN <<"wrote", name[b]>>
                                ELSE <<"pending", name[b]>>],
                    <<"orphans", orphans>>>>
       /\ UNCHANGED lib

Next ==
    \/ \E b \in Books : AddBook(b) \/ RemoveBook(b)
    \* Only a run's own books matter to `newer` and `done`, and `newer` only
    \* under --refresh: the same successors as ranging over every set of
    \* books, enumerated far fewer times over.
    \/ \E S \in SUBSET lib, refresh \in BOOLEAN :
         /\ S # {}
         /\ AllowMatch \/ S = lib
         /\ AllowRefresh \/ ~refresh
         /\ \E newer \in (IF refresh THEN SUBSET Packages(S) ELSE {{}}),
               done \in SUBSET S :
              Run(S, refresh, newer, done)

Spec == Init /\ [][Next]_vars

\* What makes two states the same, for TLC's VIEW: everything but `last`,
\* which is there for reading counterexamples and would otherwise multiply
\* the states checked by every way of reaching each one.
View == <<lib, shelf, misreported, clobbered, stranded, orphaned>>

-----------------------------------------------------------------------------
(* Properties *)

\* A book the planner calls exported is the book in the file it points at,
\* and a copy taken as already on the shelf is in the file it would have
\* been copied to. Otherwise that book is silently never exported, and the
\* file that really holds it can be reported as an orphan no book claims.
ExportedMeansTheBooksOwnFile == ~misreported

\* No run replaces the archive of another book. Nothing in the tool deletes a
\* book from the shelf (find_orphans: "Nothing is deleted, here or anywhere").
NeverWritesOverAnotherBook == ~clobbered

\* Suffix mode exists to keep both books. One with a usable identifier is
\* never left unexported because another book's archive holds its name.
SuffixKeepsEveryIdentifiableBook == ~stranded

\* An archive whose book is in the library is never reported as an orphan:
\* the orphans are the list a person reviews before deleting.
NoArchiveOfTheLibraryIsAnOrphan == ~orphaned

TypeOK ==
    /\ lib \subseteq Books
    /\ Copies \subseteq Books
    /\ shelf \in [Names -> 0..N]
    /\ misreported \in BOOLEAN
    /\ clobbered \in BOOLEAN
    /\ stranded \in BOOLEAN
    /\ orphaned \in BOOLEAN

-----------------------------------------------------------------------------
(* Libraries used by the configurations *)

\* Three editions of one title: every book competes for one name.
OneTitle == [b \in Books |-> "Dune"]

\* Two editions of one title, and a book whose title is what the second
\* edition's suffix produces.
SuffixLookalike == [b \in Books |-> IF b = 3 THEN "Dune (2)" ELSE "Dune"]
=============================================================================
