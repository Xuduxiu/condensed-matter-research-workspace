# The mathematical argument

## Problem and conventions

Let `m` be a positive integer. A balanced binary ring is a word of length `2m` with `m` zeros and `m` ones, with indices taken modulo `2m`. Define

\[
D_j(x)=\#\{i\in\{0,\ldots,2m-1\}:x_i\ne x_{i+j}\},\qquad j=1,2.
\]

Each starting index is counted once. In particular, when the ring has two sites, `D1 = 2` and `D2 = 0`. These are oriented positional counts, not the number of distinct undirected edges in a simple graph.

The exact attainable pairs are

\[
(D_1,D_2)=(2m,0)
\]

and

\[
(D_1,D_2)=(2r,2u),\quad
1\le r<m,\quad 2\le u\le2\min(r,m-r).
\]

Equivalently, for fixed `D1 = 2r` and `r < m`, the possible values of `D2` are precisely the even integers from `4` to `4 min(r,m-r)`, inclusive. The statement is both necessary and sufficient.

## 1. Write a ring in runs

Because both colours occur, choose the origin immediately after a `1 -> 0` boundary. The word then has the form

\[
0^{a_1}1^{b_1}0^{a_2}1^{b_2}\cdots0^{a_r}1^{b_r},
\]

where every block length is positive and

\[
\sum_{i=1}^{r}a_i=\sum_{i=1}^{r}b_i=m.
\]

There are exactly `2r` boundaries, so `D1 = 2r`. Changing the origin does not change either disagreement count. The formal proof must justify this change of representation as well as the count on an expanded word.

## 2. Count next-nearest disagreements

Call a run *long* if its length is at least two. Let `q0` and `q1` count the long zero-runs and long one-runs.

A useful intermediate word is the boundary word `d_i = x_i xor x_(i+1)`. The identity

\[
x_i\mathbin{\mathrm{xor}}x_{i+2}
=d_i\mathbin{\mathrm{xor}}d_{i+1}
\]

shows that `D2(x)` is the nearest-neighbour disagreement count of `d`. Each original long run contributes one nonempty zero-block in `d`. Such a block has two ends. Singleton runs contribute no zero-block. Hence

\[
D_2(x)=2(q_0+q_1).
\]

This includes the all-alternating ring: there are no long runs, the boundary word is all ones, and `D2 = 0`.

An equivalent expression uses the total number `t` of singleton runs: `D2 = 4r - 2t`.

## 3. Exact spectrum for one colour

Consider a positive composition of `m` into `r` parts and let `q` count its long parts.

If `m = r`, every part equals one and `q = 0`. If `r < m`, a long part must exist and

\[
1\le q\le r,\qquad r+q\le m.
\]

The second inequality counts one site per part plus one extra site per long part. These conditions are sufficient: take `r-q` ones, `q-1` twos, and one final part

\[
m-r-q+2\ge2.
\]

This has exactly the required length, sum, positivity, and long-part count. Thus every integer `q` between `1` and `min(r,m-r)` occurs.

The Lean development first proves the equivalent singleton formulation, then derives the long-run formulation. Both directions include the empty-composition boundary cases explicitly.

## 4. Combine the two colours

When `r < m`, set `L = min(r,m-r)`. Both `q0` and `q1` may independently be any integer from `1` through `L`. Their sum therefore may be any integer from `2` through `2L`.

For a prescribed admissible sum `u`, one explicit choice is

\[
q_0=\lfloor u/2\rfloor,\qquad q_1=u-\lfloor u/2\rfloor.
\]

Use the construction from step 3 for each colour and alternate the resulting blocks. This realizes every admitted pair. Conversely, any actual ring yields two such compositions, so no other pair is possible.

If `r=m`, both compositions consist entirely of ones and the pair is `(2m,0)`.

## Example: twelve sites

| `D1` | All possible `D2` values |
|---:|---|
| 2 | 4 |
| 4 | 4, 6, 8 |
| 6 | 4, 6, 8, 10, 12 |
| 8 | 4, 6, 8 |
| 10 | 4 |
| 12 | 0 |

`000000111111` has pair `(2,4)`, `001100110011` has `(6,12)`, and `010101010101` has `(12,0)`.

## What is and is not claimed

This project records a self-contained derivation and machine-checked formalization. The derivation is elementary combinatorics. A verified universal theorem is stronger than testing any finite set of examples, but verification alone does not establish originality or publication significance.

No priority claim is made for the mathematical result or its formalization. Binary-run statistics and autocorrelation have extensive prior literature. The proof here needs no imported theorem about Ising models or a particular material, and it does not predict a real material's preferred ordering without an energy model.

## Related primary literature

- W. Moser, “Cyclic Binary Strings without Long Runs of Like (Alternating) Bits,” *Fibonacci Quarterly* **31**(1), 2–6 (1993). <https://www.fq.math.ca/Scanned/31-1/moser.pdf>. Includes the cyclic binary derivative and its even-parity image.
- Kai Cai, “Autocorrelation-Run Formula for Binary Sequences,” arXiv:0909.4592. <https://arxiv.org/abs/0909.4592>.
- Jürgen Willms, “Autocorrelations of Binary Sequences and Run Structure,” *IEEE Transactions on Information Theory* **59**(8), 4985–4993 (2013). <https://arxiv.org/abs/1206.4572>; DOI: <https://doi.org/10.1109/TIT.2013.2259293>.

These references establish the related mathematical context. This limited search does not establish that the exact classification above is absent from all earlier literature or code.
