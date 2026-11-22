import Fireworks.Composition

namespace Fireworks

/-- A mismatch contributes one bond defect. -/
def mismatch (a b : Bool) : Nat := if a = b then 0 else 1

/-- Read a word, comparing each new symbol with its predecessor. -/
def scan1 : Bool → List Bool → Nat
  | _, [] => 0
  | p, x :: xs => mismatch p x + scan1 x xs

/-- Read a word, comparing each new symbol with the symbol two positions before it. -/
def scan2 : Bool → Bool → List Bool → Nat
  | _, _, [] => 0
  | p, q, x :: xs => mismatch p x + scan2 q x xs

/-- The two most recently read symbols, oldest first. -/
def end2 : Bool → Bool → List Bool → Bool × Bool
  | p, q, [] => (p, q)
  | _, q, x :: xs => end2 q x xs

/-- Count unequal adjacent symbols around a periodic binary word. -/
def cyclicD1 (xs : List Bool) : Nat := scan1 (xs.getLastD false) xs

/-- Count unequal symbols at distance two around a periodic binary word.
Reading two copies supplies the correct cyclic history even for a singleton. -/
def cyclicD2 (xs : List Bool) : Nat :=
  let pq := end2 false false (xs ++ xs)
  scan2 pq.1 pq.2 xs

/-- Expand alternating false and true runs. Main theorems require equal-length
positive block lists; unmatched input tails are deliberately ignored. -/
def expand : List Nat → List Nat → List Bool
  | a :: as, b :: bs =>
      List.replicate a false ++ (List.replicate b true ++ expand as bs)
  | _, _ => []


@[simp] theorem mismatch_self (p : Bool) : mismatch p p = 0 := by simp [mismatch]

/-- A convenient numerical encoding of a Boolean state. -/
def bit (p : Bool) : Nat := if p then 1 else 0

@[simp] theorem end2_append (p q : Bool) (xs ys : List Bool) :
    end2 p q (xs ++ ys) = end2 (end2 p q xs).1 (end2 p q xs).2 ys := by
  induction xs generalizing p q with
  | nil => rfl
  | cons x xs ih => simpa only [List.cons_append, end2] using ih q x

@[simp] theorem scan2_append (p q : Bool) (xs ys : List Bool) :
    scan2 p q (xs ++ ys) = scan2 p q xs +
      scan2 (end2 p q xs).1 (end2 p q xs).2 ys := by
  induction xs generalizing p q with
  | nil => simp [scan2, end2]
  | cons x xs ih => simp only [List.cons_append, scan2, end2, ih, Nat.add_assoc]

@[simp] theorem end2_replicate_same (p : Bool) (n : Nat) :
    end2 p p (List.replicate n p) = (p, p) := by
  induction n with
  | zero => rfl
  | succ n ih => simpa only [List.replicate_succ, end2] using ih

@[simp] theorem scan2_replicate_same (p : Bool) (n : Nat) (xs : List Bool) :
    scan2 p p (List.replicate n p ++ xs) = scan2 p p xs := by
  induction n with
  | zero => rfl
  | succ n ih => simpa only [List.replicate_succ, List.cons_append, scan2,
      mismatch_self, Nat.zero_add] using ih

@[simp] theorem end2_replicate_two (p q c : Bool) (n : Nat) :
    end2 p q (List.replicate (n + 2) c) = (c, c) := by
  simp [show n + 2 = (n + 1) + 1 by omega, List.replicate_succ, end2]

@[simp] theorem scan2_replicate_two (p q c : Bool) (n : Nat) (xs : List Bool) :
    scan2 p q (List.replicate (n + 2) c ++ xs) =
      mismatch p c + mismatch q c + scan2 c c xs := by
  change mismatch p c + (mismatch q c +
    scan2 c c (List.replicate n c ++ xs)) = _
  rw [scan2_replicate_same]
  omega

@[simp] theorem scan1_replicate_same (p : Bool) (n : Nat) (xs : List Bool) :
    scan1 p (List.replicate n p ++ xs) = scan1 p xs := by
  induction n with
  | zero => rfl
  | succ n ih => simpa only [List.replicate_succ, List.cons_append, scan1,
      mismatch_self, Nat.zero_add] using ih

 theorem scan1_replicate_positive (p c : Bool) (n : Nat) (hn : 0 < n)
    (xs : List Bool) :
    scan1 p (List.replicate n c ++ xs) = mismatch p c + scan1 c xs := by
  cases n with
  | zero => omega
  | succ n => simp [List.replicate_succ, scan1]

 theorem getLastD_append (xs ys : List Bool) (d : Bool) :
    (xs ++ ys).getLastD d = ys.getLastD (xs.getLastD d) := by
  induction xs generalizing d with
  | nil => rfl
  | cons x xs ih => simpa only [List.cons_append, List.getLastD_cons] using ih x

 theorem getLastD_replicate_positive (n : Nat) (hn : 0 < n) (c d : Bool) :
    (List.replicate n c).getLastD d = c := by
  cases n with
  | zero => omega
  | succ n =>
    induction n with
    | zero => rfl
    | succ n ih => simpa only [List.replicate_succ, List.getLastD_cons] using ih (by omega)

 theorem expand_length (a b : List Nat) (hl : a.length = b.length) :
    (expand a b).length = a.sum + b.sum := by
  induction a generalizing b with
  | nil => have : b = [] := List.length_eq_zero_iff.mp hl.symm; simp [this, expand]
  | cons x xs ih =>
    cases b with
    | nil => simp at hl
    | cons y ys =>
      have ht : xs.length = ys.length := by simpa using hl
      simp only [expand, List.length_append, List.length_replicate,
        List.sum_cons, ih ys ht]
      omega

 theorem expand_count_false (a b : List Nat) (hl : a.length = b.length) :
    (expand a b).count false = a.sum := by
  induction a generalizing b with
  | nil => simp [expand]
  | cons x xs ih =>
    cases b with
    | nil => simp at hl
    | cons y ys =>
      have ht : xs.length = ys.length := by simpa using hl
      simp [expand, List.count_replicate, ih ys ht]

 theorem expand_count_true (a b : List Nat) (hl : a.length = b.length) :
    (expand a b).count true = b.sum := by
  induction a generalizing b with
  | nil => have : b = [] := List.length_eq_zero_iff.mp hl.symm; simp [this, expand]
  | cons x xs ih =>
    cases b with
    | nil => simp at hl
    | cons y ys =>
      have ht : xs.length = ys.length := by simpa using hl
      simp [expand, List.count_replicate, ih ys ht]

 theorem scan1_expand (a b : List Nat) (hl : a.length = b.length)
    (ha : Positive a) (hb : Positive b) :
    scan1 true (expand a b) = 2 * a.length := by
  induction a generalizing b with
  | nil => simp [expand, scan1]
  | cons x xs ih =>
    cases b with
    | nil => simp at hl
    | cons y ys =>
      have ht : xs.length = ys.length := by simpa using hl
      have hx : 0 < x := ha x (by simp)
      have hy : 0 < y := hb y (by simp)
      have hxs : Positive xs := fun z hz => ha z (by simp [hz])
      have hys : Positive ys := fun z hz => hb z (by simp [hz])
      simp only [expand, scan1_replicate_positive true false x hx,
        scan1_replicate_positive false true y hy, ih ys ht hxs hys,
        List.length_cons]
      simp [mismatch]
      omega

 theorem expand_last_true (a b : List Nat) (hl : a.length = b.length)
    (ha : Positive a) (hb : Positive b) :
    (expand a b).getLastD true = true := by
  induction a generalizing b with
  | nil => rfl
  | cons x xs ih =>
    cases b with
    | nil => simp at hl
    | cons y ys =>
      have ht : xs.length = ys.length := by simpa using hl
      have hy : 0 < y := hb y (by simp)
      have hxs : Positive xs := fun z hz => ha z (by simp [hz])
      have hys : Positive ys := fun z hz => hb z (by simp [hz])
      simp only [expand, getLastD_append,
        getLastD_replicate_positive y hy]
      exact ih ys ht hxs hys

 theorem expand_last (a b : List Nat) (hl : a.length = b.length)
    (ha : Positive a) (hb : Positive b) (hn : a ≠ []) (d : Bool) :
    (expand a b).getLastD d = true := by
  cases a with
  | nil => contradiction
  | cons x xs =>
    cases b with
    | nil => simp at hl
    | cons y ys =>
      have ht : xs.length = ys.length := by simpa using hl
      have hy : 0 < y := hb y (by simp)
      have hxs : Positive xs := fun z hz => ha z (by simp [hz])
      have hys : Positive ys := fun z hz => hb z (by simp [hz])
      simp only [expand, getLastD_append,
        getLastD_replicate_positive y hy]
      exact expand_last_true xs ys ht hxs hys

 theorem expand_d1 (a b : List Nat) (hl : a.length = b.length)
    (ha : Positive a) (hb : Positive b) :
    cyclicD1 (expand a b) = 2 * a.length := by
  by_cases hn : a = []
  · simp [hn, cyclicD1, expand, scan1]
  · rw [cyclicD1, expand_last a b hl ha hb hn false]
    exact scan1_expand a b hl ha hb


 theorem end2_snd (p q : Bool) (xs : List Bool) :
    (end2 p q xs).2 = xs.getLastD q := by
  induction xs generalizing p q with
  | nil => rfl
  | cons x xs ih => simpa only [end2, List.getLastD_cons] using ih q x

 theorem end2_independent (xs : List Bool) (h : 2 ≤ xs.length)
    (p q p' q' : Bool) : end2 p q xs = end2 p' q' xs := by
  cases xs with
  | nil => simp at h
  | cons x xs =>
    cases xs with
    | nil => simp at h
    | cons y ys => rfl

 theorem end2_pair (p q : Bool) (a b : Nat) (ha : 0 < a) (hb : 0 < b) :
    end2 p q (List.replicate a false ++ List.replicate b true) =
      (decide (2 ≤ b), true) := by
  cases a with
  | zero => omega
  | succ a =>
    cases b with
    | zero => omega
    | succ b =>
      cases b with
      | zero =>
        rw [end2_append]
        change ((end2 p q (List.replicate (a + 1) false)).2, true) = (false, true)
        rw [end2_snd, getLastD_replicate_positive (a + 1) (by omega) false q]
      | succ b =>
        rw [end2_append]
        simp only [show decide (2 ≤ b + 1 + 1) = true by simp]
        exact end2_replicate_two
          (end2 p q (List.replicate (a + 1) false)).1
          (end2 p q (List.replicate (a + 1) false)).2 true b

 theorem scan2_pair (p : Bool) (a b : Nat) (ha : 0 < a) (hb : 0 < b)
    (xs : List Bool) :
    scan2 p true (List.replicate a false ++ (List.replicate b true ++ xs)) =
      bit p + 2 * (if 2 ≤ a then 1 else 0) + (if 2 ≤ b then 1 else 0) +
        scan2 (decide (2 ≤ b)) true xs := by
  cases a with
  | zero => omega
  | succ a =>
    cases b with
    | zero => omega
    | succ b =>
      cases a <;> cases b <;> cases p <;>
        simp only [List.replicate_succ, List.replicate_zero, List.nil_append,
          List.cons_append, scan2, scan2_replicate_same] <;>
        simp [bit, mismatch, Nat.add_assoc] <;> omega

 theorem scan2_expand_balance (a b : List Nat) (hl : a.length = b.length)
    (ha : Positive a) (hb : Positive b) (p : Bool) :
    scan2 p true (expand a b) + bit (end2 p true (expand a b)).1 =
      bit p + 2 * (longRuns a + longRuns b) := by
  induction a generalizing b p with
  | nil =>
    have he : b = [] := List.length_eq_zero_iff.mp hl.symm
    simp [he, expand, scan2, end2]
  | cons x xs ih =>
    cases b with
    | nil => simp at hl
    | cons y ys =>
      have ht : xs.length = ys.length := by simpa using hl
      have hx : 0 < x := ha x (by simp)
      have hy : 0 < y := hb y (by simp)
      have hxs : Positive xs := fun z hz => ha z (by simp [hz])
      have hys : Positive ys := fun z hz => hb z (by simp [hz])
      have hi := ih ys ht hxs hys (decide (2 ≤ y))
      have he : end2 p true (expand (x :: xs) (y :: ys)) =
          end2 (decide (2 ≤ y)) true (expand xs ys) := by
        simp only [expand, ← List.append_assoc]
        rw [end2_append, end2_pair p true x y hx hy]
      rw [he]
      simp only [expand, scan2_pair p x y hx hy, longRuns_cons]
      by_cases hy2 : 2 ≤ y <;> by_cases hx2 : 2 ≤ x <;>
        simp only [hy2, hx2, ↓reduceIte, decide_true, decide_false, bit,
          Bool.false_eq_true, Bool.true_eq_false, Bool.false_eq_true,
          Bool.true_or, Bool.false_or] at * <;>
        simp at hi ⊢ <;> omega


/-- Each run of length at least two contributes exactly two distance-two
mismatches, including when its neighbours are singleton runs. -/
theorem expand_d2 (a b : List Nat) (hl : a.length = b.length)
    (ha : Positive a) (hb : Positive b) :
    cyclicD2 (expand a b) = 2 * (longRuns a + longRuns b) := by
  by_cases hn : a = []
  · have hbn : b = [] := by
      apply List.length_eq_zero_iff.mp
      simpa [hn] using hl.symm
    simp [hn, hbn, expand, cyclicD2, end2, scan2, longRuns]
  · have hla : 0 < a.length := by
      cases a with
      | nil => contradiction
      | cons x xs => exact Nat.zero_lt_succ xs.length
    have hsa := length_le_sum a ha
    have hsb := length_le_sum b hb
    have hw : 2 ≤ (expand a b).length := by
      rw [expand_length a b hl]
      omega
    have hreset (p q : Bool) :
        end2 p q (expand a b) = end2 false false (expand a b) :=
      end2_independent (expand a b) hw p q false false
    have hpq : end2 false false (expand a b ++ expand a b) =
        end2 false false (expand a b) := by
      rw [end2_append, hreset]
    have hlast : (end2 false false (expand a b)).2 = true := by
      rw [end2_snd, expand_last a b hl ha hb hn false]
    have hi := scan2_expand_balance a b hl ha hb (end2 false false (expand a b)).1
    have hf := hreset (end2 false false (expand a b)).1 true
    rw [hf] at hi
    simp only [cyclicD2, hpq, hlast]
    omega

end Fireworks
