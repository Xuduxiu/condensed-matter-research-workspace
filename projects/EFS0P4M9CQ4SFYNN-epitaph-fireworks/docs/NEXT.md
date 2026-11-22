# Continuation after the certified theorem

These are separate research targets. None is counted as a completed result of the first theorem.

## A. Extend the same classification

An immediate technical extension is to unequal populations: `a` zeros and `b` ones. At a prescribed number `r` of runs of each colour, the possible long-run counts of each colour are already characterized by `composition_longRuns_spectrum`. Combining the two intervals should give a complete `(D1,D2)` classification without the balance assumption.

A second extension is to prescribed three-step correlation `D3`. Here long-run counts alone no longer determine the statistic; adjacency between short runs matters. Start by finding counterexamples to any proposed sufficient statistic. Only then formulate and prove a complete statement.

These extensions may yield useful formalized lemmas, but they do not by themselves establish mathematical novelty. Search the existing run/autocorrelation literature before making such a claim.

## B. A genuinely open-problem candidate

Grytczuk, Pawlik and Ruciński ask in §6.3 of *Shuffle squares and ordered nest-free graphs* whether every binary word in which each letter occurs an even number of times can be cut at most twice and have the resulting pieces rearranged into a shuffle square.

A shuffle square is a word whose positions can be partitioned into two subsequences that read as the same word. Reordering pieces preserves the internal order of each piece. This is a different notion from simply having equal numbers of zeros and ones.

Primary source: <https://arxiv.org/html/2503.22043v2>, §6.3.

The related project <https://vamshij.com/research/shuffle-anti-squares/> (v3 dated 2026-11-19, checked 2026-11-22) describes the two-cut assertion as a conjecture. That project already establishes the existence of words of every even length at least 24 for which no cyclic rotation is a shuffle square. Do not relabel that existing result as a new target.

If pursuing the two-cut question, first formalize the intended cutting and shuffle relations, reproduce the published small examples, and identify a restricted family not already covered by the literature. Finding a counterexample is a valid outcome. A failed search or a finite bound is not a proof of the universal conjecture.

The first project does not promise a solution to this open problem and does not claim that work on it has been executed.
