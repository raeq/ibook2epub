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
 * Modelled from epubconvert/run/planning.py:
 *   assign_names / _wanted_names / _assign_one / _claim   naming, in sorted
 *       order, with collisions settled by suffix or by losing
 *   _stable_base / marked       a crowded book with a usable dc:identifier is
 *                               marked with a digest of it
 *   suffixed                    " (2)", " (3)", ...
 *   plan_exports / _decide / _decide_against_existing    EXPORTED when a
 *       file of the book's name exists, PENDING otherwise; --refresh writes
 *       over that file when the source is newer
 *   _decide_against_holder      COLLISION when that file's usable identifier
 *       and the book's differ; with either unusable, the name is trusted.
 *       A policy that names from the folder reads no package document, so
 *       _decide reads the source's identifier only for a book about to be
 *       written over an archive, and a book reported exported is not checked
 *   _place                      under suffix, a book whose name holds another
 *       book moves on to the first position of its marked name that no other
 *       book of the run is named and no other book's archive holds
 * and epubconvert/run/run.py (_shared_names): every run names the whole
 * library, and one narrowed by --match plans only the books it selected. It
 * used to name the selection alone, so a matched book could get a different
 * name from the one a full run gives it.
 *
 * Names are strings, compared exactly: PassthroughNaming.identity is the
 * filename, and case folding is left out. A digest marker is " [b]" for book
 * b; a real digest is a hash of the identifier, equal for equal identifiers.
 *)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
    N,             \* books; book b sorts before book b + 1
    Wanted,        \* the name each book's policy asks for, [1..N -> STRING]
    Usable,        \* books with a usable, unique dc:identifier
    OnCollision,   \* "skip" (the default) or "suffix"
    AllowMatch,    \* runs may be narrowed with --match
    AllowRefresh,  \* runs may pass --refresh
    AllowChanges,  \* books may be added to and removed from the library
    VerifyHolder,  \* _decide_against_holder: the check that fixes the defects
    ReadsSources,  \* naming reads each package document (--name-by author-title);
                   \* otherwise the check runs only before a write
    MoveOn         \* _place: under suffix, a book whose name holds another book
                   \* moves on to its marked name

Books == 1..N

VARIABLES
    lib,           \* books in the library
    shelf,         \* name -> the book whose archive has that name, or 0
    misreported,   \* history: a run called a book exported by a file of another
    clobbered,     \* history: a run wrote over another book's archive
    stranded,      \* history: a suffix run left an identifiable book unexported
                   \* because another book's archive held its name
    last           \* the last run: what it selected and decided, for traces

vars == <<lib, shelf, misreported, clobbered, stranded, last>>

-----------------------------------------------------------------------------
(* Naming: assign_names *)

\* MAX_SUFFIX is 99; N positions always leave one free for N books.
Limit == IF OnCollision = "suffix" THEN N ELSE 1

Suffixed(base, k) == IF k = 1 THEN base ELSE base \o " (" \o ToString(k) \o ")"
Marked(name, b) == name \o " [" \o ToString(b) \o "]"

Crowd(S, want) == Cardinality({c \in S : Wanted[c] = want})

\* _assign_one's base: marked only when crowded and identifiable.
Base(S, b) ==
    IF OnCollision = "suffix" /\ Crowd(S, Wanted[b]) > 1 /\ b \in Usable
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
             name == IF free = {}
                       THEN ""
                       ELSE Suffixed(base, CHOOSE k \in free :
                                              \A j \in free : k <= j)
             rest == Claim(S, Tail(order),
                           IF name = "" THEN claimed ELSE claimed \cup {name})
         IN [c \in {b} \cup DOMAIN rest |-> IF c = b THEN name ELSE rest[c]]

Assign(S) == Claim(S, Sorted(S), {})

\* Every name any selection could produce: the shelf's domain.
Names ==
    {Suffixed(base, k) :
        base \in {Wanted[b] : b \in Books} \cup {Marked(Wanted[b], b) : b \in Books},
        k \in 1..Limit}

-----------------------------------------------------------------------------

Init ==
    /\ lib = Books
    /\ shelf = [n \in Names |-> 0]
    /\ misreported = FALSE
    /\ clobbered = FALSE
    /\ stranded = FALSE
    /\ last = <<>>

AddBook(b) ==
    /\ AllowChanges
    /\ b \notin lib
    /\ lib' = lib \cup {b}
    /\ last' = <<"added", b>>
    /\ UNCHANGED <<shelf, misreported, clobbered, stranded>>

RemoveBook(b) ==
    /\ AllowChanges
    /\ b \in lib
    /\ lib' = lib \ {b}
    /\ last' = <<"removed", b>>
    /\ UNCHANGED <<shelf, misreported, clobbered, stranded>>

(* One run over the selection S. Every decision is made before anything is
   written (plan_exports, then the export). `newer` is the books whose source
   is newer than their archive, for --refresh. `done` is the writes that
   finish: -m caps a run, and a run can be stopped.

   `first` and `name` are bound with \E x \in {e} rather than LET because
   TLC re-evaluates a LET definition at every reference: naming, once per
   lookup of a name. ChangingSuffix took 2m08s that way and takes 1s. *)
Run(S, refresh, newer, done) ==
    \E whole \in {Assign(lib)} :
    \E first \in {[b \in S |-> whole[b]]} :
    LET
        \* Whose archive is compared: every book on the shelf when naming read
        \* the sources, otherwise only a book --refresh would write.
        Checked(b) == ReadsSources \/ (refresh /\ b \in newer)
        \* The archive named n holds another book: its identifier and b's are
        \* both usable and differ.
        Foreign(b, n) == /\ VerifyHolder
                         /\ Checked(b)
                         /\ b \in Usable
                         /\ shelf[n] \in Usable
                         /\ shelf[n] # b
        \* Under suffix, a book whose name holds another book moves on, when
        \* naming read the identifier its marked name is a digest of.
        moves    == {b \in S : /\ MoveOn
                               /\ OnCollision = "suffix"
                               /\ ReadsSources
                               /\ first[b] # ""
                               /\ Foreign(b, first[b])}
        taken    == {whole[c] : c \in lib}
        \* _place: the first position of b's marked name that no book of the
        \* library is named and no other book's archive holds. first[b] is one of
        \* the names taken, and holds another book besides.
        moved    == [b \in moves |->
                       LET open == {k \in 1..Limit :
                                      LET n == Suffixed(Marked(Wanted[b], b), k)
                                      IN n \notin taken /\ ~Foreign(b, n)}
                       IN IF open = {} THEN ""
                          ELSE Suffixed(Marked(Wanted[b], b),
                                        CHOOSE k \in open : \A j \in open : k <= j)]
        named    == [b \in S |-> IF b \in moves THEN moved[b] ELSE first[b]]
    IN \E name \in {named} :
    LET present  == {b \in S : name[b] # "" /\ shelf[name[b]] # 0}
        foreign  == {b \in present : Foreign(b, name[b])}
        collided == {b \in S : name[b] = ""} \cup foreign
        ours     == present \ foreign
        rewrite  == IF refresh THEN ours \cap newer ELSE {}
        exported == ours \ rewrite
        pending  == {b \in S : name[b] # "" /\ shelf[name[b]] = 0} \cup rewrite
        written  == pending \cap done
    IN /\ misreported' = (misreported \/
                           \E b \in exported : shelf[name[b]] # b)
       /\ clobbered' = (clobbered \/
                         \E b \in written : shelf[name[b]] \notin {0, b})
       /\ stranded' = (stranded \/
                        (OnCollision = "suffix" /\ collided \cap Usable # {}))
       /\ shelf' = [n \in Names |->
                      IF \E b \in written : name[b] = n
                        THEN CHOOSE b \in written : name[b] = n
                        ELSE shelf[n]]
       /\ last' = <<"ran", IF S = lib THEN "all" ELSE "--match", S,
                    IF refresh THEN "--refresh" ELSE "",
                    [b \in S |-> IF b \in collided THEN "collision"
                                ELSE IF b \in exported THEN <<"exported", name[b]>>
                                ELSE IF b \in written THEN <<"wrote", name[b]>>
                                ELSE <<"pending", name[b]>>]>>
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
         /\ \E newer \in (IF refresh THEN SUBSET S ELSE {{}}),
               done \in SUBSET S :
              Run(S, refresh, newer, done)

Spec == Init /\ [][Next]_vars

\* What makes two states the same, for TLC's VIEW: everything but `last`,
\* which is there for reading counterexamples and would otherwise multiply
\* the states checked by every way of reaching each one.
View == <<lib, shelf, misreported, clobbered, stranded>>

-----------------------------------------------------------------------------
(* Properties *)

\* A book the planner calls exported is the book in the file it points at.
\* Otherwise that book is silently never exported, and the file that really
\* holds it can be reported as an orphan no book claims.
ExportedMeansTheBooksOwnFile == ~misreported

\* No run replaces the archive of another book. Nothing in the tool deletes a
\* book from the shelf (find_orphans: "Nothing is deleted, here or anywhere").
NeverWritesOverAnotherBook == ~clobbered

\* Suffix mode exists to keep both books. One with a usable identifier is
\* never left unexported because another book's archive holds its name.
SuffixKeepsEveryIdentifiableBook == ~stranded

TypeOK ==
    /\ lib \subseteq Books
    /\ shelf \in [Names -> 0..N]
    /\ misreported \in BOOLEAN
    /\ clobbered \in BOOLEAN
    /\ stranded \in BOOLEAN

-----------------------------------------------------------------------------
(* Libraries used by the configurations *)

\* Three editions of one title: every book competes for one name.
OneTitle == [b \in Books |-> "Dune"]

\* Two editions of one title, and a book whose title is what the second
\* edition's suffix produces.
SuffixLookalike == [b \in Books |-> IF b = 3 THEN "Dune (2)" ELSE "Dune"]
=============================================================================
