import Std

namespace Fireworks

/-- The number of length-one blocks in a list of positive run lengths. -/
def singles (xs : List Nat) : Nat := xs.countP (fun a => a == 1)

def Positive (xs : List Nat) : Prop := ∀ a ∈ xs, 0 < a

@[simp] theorem singles_nil : singles [] = 0 := rfl

@[simp] theorem singles_cons (a : Nat) (xs : List Nat) :
    singles (a :: xs) = singles xs + if a = 1 then 1 else 0 := by
  simp [singles, List.countP_cons]

@[simp] theorem singles_append (xs ys : List Nat) :
    singles (xs ++ ys) = singles xs + singles ys := by
  simp [singles]

@[simp] theorem singles_replicate (n a : Nat) :
    singles (List.replicate n a) = if a = 1 then n else 0 := by
  simp [singles, List.countP_replicate]

@[simp] theorem nat_sum_append (xs ys : List Nat) :
    (xs ++ ys).sum = xs.sum + ys.sum := by
  induction xs with
  | nil => simp
  | cons a xs ih => simp [ih, Nat.add_assoc]

theorem singles_le_length (xs : List Nat) : singles xs ≤ xs.length :=
  List.countP_le_length

theorem length_le_sum (xs : List Nat) (hp : Positive xs) : xs.length ≤ xs.sum := by
  induction xs with
  | nil => simp
  | cons a xs ih =>
      have ha : 0 < a := hp a (by simp)
      have ht : Positive xs := fun b hb => hp b (by simp [hb])
      have := ih ht
      simp only [List.length_cons, List.sum_cons]
      omega

/-- Every non-singleton positive block contributes at least two sites. -/
theorem length_twice_le_sum_add_singles (xs : List Nat) (hp : Positive xs) :
    2 * xs.length ≤ xs.sum + singles xs := by
  induction xs with
  | nil => simp
  | cons a xs ih =>
      have ha : 0 < a := hp a (by simp)
      have ht : Positive xs := fun b hb => hp b (by simp [hb])
      have := ih ht
      simp only [List.length_cons, List.sum_cons, singles_cons]
      by_cases h : a = 1 <;> simp only [h, ↓reduceIte] <;> omega

theorem sum_eq_length_of_all_singletons (xs : List Nat)
    (h : singles xs = xs.length) : xs.sum = xs.length := by
  have hones : ∀ a ∈ xs, a = 1 := by
    have hf := List.countP_eq_length.mp h
    intro a ha
    simpa using hf a ha
  induction xs with
  | nil => simp
  | cons a xs ih =>
      have ha := hones a (by simp)
      have ht : ∀ b ∈ xs, b = 1 := fun b hb => hones b (by simp [hb])
      have hs : singles xs = xs.length := List.countP_eq_length.mpr (by simpa using ht)
      have := ih hs ht
      simp [ha, this, Nat.add_comm]

theorem singles_lt_length_of_length_lt_sum (xs : List Nat)
    (h : xs.length < xs.sum) : singles xs < xs.length := by
  have hle := singles_le_length xs
  by_cases heq : singles xs = xs.length
  · have := sum_eq_length_of_all_singletons xs heq
    omega
  · omega

theorem singles_eq_length_of_sum_eq_length (xs : List Nat) (hp : Positive xs)
    (h : xs.sum = xs.length) : singles xs = xs.length := by
  have hb := length_twice_le_sum_add_singles xs hp
  have hc := singles_le_length xs
  omega

/-- An explicit positive composition with exactly `t` singleton blocks.
The remaining blocks are twos, except that the last block absorbs the excess. -/
def makeComposition (m r t : Nat) : List Nat :=
  List.replicate t 1 ++
    (List.replicate (r - t - 1) 2 ++ [m - t - 2 * (r - t - 1)])

theorem makeComposition_spec {m r t : Nat}
    (ht : t < r) (hb : 2 * r ≤ m + t) :
    (makeComposition m r t).length = r ∧
    (makeComposition m r t).sum = m ∧
    Positive (makeComposition m r t) ∧
    singles (makeComposition m r t) = t := by
  have hlast : 2 ≤ m - t - 2 * (r - t - 1) := by omega
  have hlastne : m - t - 2 * (r - t - 1) ≠ 1 := by omega
  refine ⟨?_, ?_, ?_, ?_⟩
  · simp [makeComposition]
    omega
  · simp [makeComposition, Nat.mul_comm]
    omega
  · intro a ha
    simp only [makeComposition, List.mem_append, List.mem_replicate,
      List.mem_cons, List.not_mem_nil, or_false] at ha
    rcases ha with h | h | h
    · omega
    · omega
    · omega
  · simp [makeComposition, hlastne]

/-- Exact singleton-count spectrum for positive compositions of `m` into `r` parts. -/
theorem composition_spectrum {m r t : Nat} :
    (∃ xs : List Nat, xs.length = r ∧ xs.sum = m ∧ Positive xs ∧ singles xs = t) ↔
    (m = r ∧ t = r) ∨ (r < m ∧ t < r ∧ 2 * r ≤ m + t) := by
  constructor
  · rintro ⟨xs, hl, hs, hp, ht⟩
    have hsum := length_le_sum xs hp
    have hbound := length_twice_le_sum_add_singles xs hp
    by_cases heq : m = r
    · left
      have he : xs.sum = xs.length := by omega
      have hx := singles_eq_length_of_sum_eq_length xs hp he
      omega
    · right
      have hlt : xs.length < xs.sum := by omega
      have hx := singles_lt_length_of_length_lt_sum xs hlt
      omega
  · rintro (⟨hm, ht⟩ | ⟨hm, ht, hb⟩)
    · refine ⟨List.replicate r 1, by simp, ?_, ?_, ?_⟩
      · simp [hm]
      · intro a ha
        simp only [List.mem_replicate] at ha
        omega
      · simp [ht]
    · exact ⟨makeComposition m r t, makeComposition_spec ht hb⟩

/-- Number of blocks containing at least two sites. -/
def longRuns (xs : List Nat) : Nat := xs.countP (fun a => decide (2 ≤ a))

@[simp] theorem longRuns_nil : longRuns [] = 0 := rfl

@[simp] theorem longRuns_cons (a : Nat) (xs : List Nat) :
    longRuns (a :: xs) = longRuns xs + if 2 ≤ a then 1 else 0 := by
  simp [longRuns, List.countP_cons]

@[simp] theorem longRuns_append (xs ys : List Nat) :
    longRuns (xs ++ ys) = longRuns xs + longRuns ys := by
  simp [longRuns]

@[simp] theorem longRuns_replicate (n a : Nat) :
    longRuns (List.replicate n a) = if 2 ≤ a then n else 0 := by
  simp [longRuns, List.countP_replicate]

/-- Every positive block is either a singleton or a long block. -/
theorem singles_add_longRuns (xs : List Nat) (hp : Positive xs) :
    singles xs + longRuns xs = xs.length := by
  induction xs with
  | nil => simp
  | cons a xs ih =>
      have ha : 0 < a := hp a (by simp)
      have ht : Positive xs := fun b hb => hp b (by simp [hb])
      have := ih ht
      simp only [singles_cons, longRuns_cons, List.length_cons]
      by_cases h : a = 1
      · simp only [h, ↓reduceIte, Nat.reduceLeDiff]
        omega
      · have ha2 : 2 ≤ a := by omega
        simp only [h, ha2, ↓reduceIte]
        omega

/-- The same complete spectrum stated using blocks of length at least two. -/
theorem composition_longRuns_spectrum {m r q : Nat} :
    (∃ xs : List Nat, xs.length = r ∧ xs.sum = m ∧ Positive xs ∧ longRuns xs = q) ↔
    (m = r ∧ q = 0) ∨ (r < m ∧ 1 ≤ q ∧ q ≤ r ∧ r + q ≤ m) := by
  constructor
  · rintro ⟨xs, hl, hs, hp, hq⟩
    have hpq := singles_add_longRuns xs hp
    have hspectrum := composition_spectrum.mp ⟨xs, hl, hs, hp, rfl⟩
    rcases hspectrum with ⟨hm, ht⟩ | ⟨hm, ht, hb⟩
    · left
      omega
    · right
      omega
  · rintro (⟨hm, hq⟩ | ⟨hm, hq, hqr, hqm⟩)
    · refine ⟨List.replicate r 1, by simp, ?_, ?_, ?_⟩
      · simp [hm]
      · intro a ha
        simp only [List.mem_replicate] at ha
        omega
      · simp [hq]
    · have ht : r - q < r := by omega
      have hb : 2 * r ≤ m + (r - q) := by omega
      obtain ⟨hl, hs, hp, htcount⟩ := makeComposition_spec ht hb
      refine ⟨makeComposition m r (r - q), hl, hs, hp, ?_⟩
      have hpartition := singles_add_longRuns (makeComposition m r (r - q)) hp
      omega

/-- Split an admissible total number of long blocks into admissible counts for two colours. -/
theorem split_longRuns {m r u : Nat} (hu : 2 ≤ u)
    (hur : u ≤ 2 * r) (hum : u + 2 * r ≤ 2 * m) :
    ∃ a b : Nat, a + b = u ∧ 1 ≤ a ∧ a ≤ r ∧ r + a ≤ m ∧
      1 ≤ b ∧ b ≤ r ∧ r + b ≤ m := by
  refine ⟨u / 2, u - u / 2, ?_, ?_, ?_, ?_, ?_, ?_, ?_⟩ <;> omega

/-- Exact two-colour spectrum, before expanding runs into an actual cyclic word. -/
theorem two_compositions_spectrum {m r u : Nat} :
    (∃ xs ys : List Nat,
      xs.length = r ∧ ys.length = r ∧ xs.sum = m ∧ ys.sum = m ∧
      Positive xs ∧ Positive ys ∧ longRuns xs + longRuns ys = u) ↔
    (m = r ∧ u = 0) ∨
      (r < m ∧ 2 ≤ u ∧ u ≤ 2 * r ∧ u + 2 * r ≤ 2 * m) := by
  constructor
  · rintro ⟨xs, ys, hxl, hyl, hxs, hys, hxp, hyp, hu⟩
    have hx := composition_longRuns_spectrum.mp ⟨xs, hxl, hxs, hxp, rfl⟩
    have hy := composition_longRuns_spectrum.mp ⟨ys, hyl, hys, hyp, rfl⟩
    rcases hx with ⟨hxm, hxq⟩ | ⟨hxm, hx1, hxr, hxm'⟩
    · left
      rcases hy with ⟨hym, hyq⟩ | ⟨hym, hy1, hyr, hym'⟩ <;> omega
    · right
      rcases hy with ⟨hym, hyq⟩ | ⟨hym, hy1, hyr, hym'⟩ <;> omega
  · rintro (⟨hm, hu⟩ | ⟨hm, hu, hur, hum⟩)
    · obtain ⟨xs, hl, hs, hp, hq⟩ :=
        composition_longRuns_spectrum.mpr (Or.inl ⟨hm, rfl⟩)
      exact ⟨xs, xs, hl, hl, hs, hs, hp, hp, by omega⟩
    · obtain ⟨a, b, hab, ha, har, ham, hb, hbr, hbm⟩ := split_longRuns hu hur hum
      obtain ⟨xs, hxl, hxs, hxp, hxq⟩ :=
        composition_longRuns_spectrum.mpr (Or.inr ⟨hm, ha, har, ham⟩)
      obtain ⟨ys, hyl, hys, hyp, hyq⟩ :=
        composition_longRuns_spectrum.mpr (Or.inr ⟨hm, hb, hbr, hbm⟩)
      exact ⟨xs, ys, hxl, hyl, hxs, hys, hxp, hyp, by omega⟩

end Fireworks
