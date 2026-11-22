import Fireworks.Spectrum
import Fireworks.Rotation
import Fireworks.Decomposition

namespace Fireworks

/-- A word has exactly `m` occurrences of each of the two Boolean colours. -/
def Balanced (w : List Bool) (m : Nat) : Prop :=
  w.count false = m ∧ w.count true = m

/-- Realization is stated in terms of the actual Boolean word and its two
directly evaluated cyclic mismatch counts. -/
def Realizes (m h k : Nat) : Prop :=
  ∃ w : List Bool, Balanced w m ∧ cyclicD1 w = h ∧ cyclicD2 w = k

theorem bool_count_add (w : List Bool) :
    w.count false + w.count true = w.length := by
  induction w with
  | nil => simp
  | cons b bs ih => cases b <;> simp_all <;> omega

theorem balanced_length {w : List Bool} {m : Nat} (hb : Balanced w m) :
    w.length = 2 * m := by
  have hc := bool_count_add w
  rcases hb with ⟨hf, ht⟩
  omega

theorem balanced_append_comm {a b : List Bool} {m : Nat} :
    Balanced (a ++ b) m ↔ Balanced (b ++ a) m := by
  simp [Balanced, List.count_append, Nat.add_comm]

/-- Every nonempty balanced word has positive alternating run data with exactly
the same two directly evaluated cyclic observables. -/
theorem balanced_run_representation {w : List Bool} {m : Nat}
    (hm : 0 < m) (hb : Balanced w m) :
    ∃ a b : List Nat,
      0 < a.length ∧ a.length = b.length ∧ a.sum = m ∧ b.sum = m ∧
      Positive a ∧ Positive b ∧
      cyclicD1 w = cyclicD1 (expand a b) ∧ cyclicD2 w = cyclicD2 (expand a b) := by
  obtain ⟨u, v, a, b, hw, he, ha, hb', hl, has, hbs, hn⟩ :=
    balanced_word_decomposition w m hm hb.1 hb.2
  refine ⟨a, b, hn, hl, has, hbs, ha, hb', ?_, ?_⟩
  · rw [hw, cyclicD1_append_comm, he]
  · rw [hw, cyclicD2_append_comm, he]

/-- The complete distance spectrum of expanded positive runs, with the
observables computed from the resulting Boolean word. -/
theorem expanded_word_spectrum {m h k : Nat} :
    (∃ a b : List Nat,
      0 < a.length ∧ a.length = b.length ∧ a.sum = m ∧ b.sum = m ∧
      Positive a ∧ Positive b ∧
      cyclicD1 (expand a b) = h ∧ cyclicD2 (expand a b) = k) ↔
    BalancedSpectrum m h k := by
  rw [balancedSpectrum_iff_compositions]
  constructor
  · rintro ⟨a, b, hn, hl, has, hbs, ha, hb, hd1, hd2⟩
    refine ⟨a, b, hn, hl, has, hbs, ha, hb, ?_, ?_⟩
    · rw [← hd1, expand_d1 a b hl ha hb]
    · rw [← hd2, expand_d2 a b hl ha hb]
  · rintro ⟨a, b, hn, hl, has, hbs, ha, hb, hd1, hd2⟩
    refine ⟨a, b, hn, hl, has, hbs, ha, hb, ?_, ?_⟩
    · rw [expand_d1 a b hl ha hb, hd1]
    · rw [expand_d2 a b hl ha hb, hd2]

/-- Complete classification for actual balanced binary words of every positive
even length. The forward direction covers every word; the reverse direction
constructs a word for every admissible pair. -/
theorem balanced_binary_ring_spectrum {m h k : Nat} (hm : 0 < m) :
    Realizes m h k ↔ BalancedSpectrum m h k := by
  constructor
  · rintro ⟨w, hb, hd1, hd2⟩
    obtain ⟨a, b, hn, hl, has, hbs, ha, hb', he1, he2⟩ :=
      balanced_run_representation hm hb
    exact expanded_word_spectrum.mp
      ⟨a, b, hn, hl, has, hbs, ha, hb', by omega, by omega⟩
  · intro hs
    obtain ⟨a, b, hn, hl, has, hbs, ha, hb, hd1, hd2⟩ :=
      expanded_word_spectrum.mpr hs
    refine ⟨expand a b, ⟨?_, ?_⟩, hd1, hd2⟩
    · rw [expand_count_false a b hl, has]
    · rw [expand_count_true a b hl, hbs]

/-- Readable run-count form: for `h = 2r`, every even second-neighbour distance
from four through `4 min(r,m-r)` occurs, except that `r=m` gives zero. -/
theorem balanced_binary_ring_spectrum_twice {m r k : Nat} (hm : 0 < m) :
    Realizes m (2 * r) k ↔
    0 < r ∧ r ≤ m ∧
      ((r = m ∧ k = 0) ∨
        (r < m ∧ k % 2 = 0 ∧ 4 ≤ k ∧ k ≤ 4 * min r (m - r))) := by
  rw [balanced_binary_ring_spectrum hm, balancedSpectrum_twice_iff]
  omega

/-- No balanced nonempty binary ring has second-neighbour distance two. -/
theorem next_neighbour_distance_ne_two {m : Nat} {w : List Bool}
    (hm : 0 < m) (hb : Balanced w m) : cyclicD2 w ≠ 2 := by
  intro hk
  have hs := (balanced_binary_ring_spectrum hm).mp
    (show Realizes m (cyclicD1 w) 2 from ⟨w, hb, rfl, hk⟩)
  unfold BalancedSpectrum at hs
  omega

/-- A genuinely forbidden pair at eight sites. -/
theorem eight_site_forbidden_pair : ¬ Realizes 4 4 2 := by
  intro hr
  have hs := (balanced_binary_ring_spectrum (by decide : 0 < 4)).mp hr
  unfold BalancedSpectrum at hs
  omega

/-- A checkable witness with an interior, rather than extremal, distance pair. -/
theorem eight_site_interior_witness : Realizes 4 4 6 := by
  refine ⟨[false, true, true, false, false, false, true, true], ?_, by decide, by decide⟩
  constructor <;> decide

/-- The perfectly alternating six-site ring. -/
theorem six_site_alternating_witness : Realizes 3 6 0 := by
  refine ⟨[false, true, false, true, false, true], ?_, by decide, by decide⟩
  constructor <;> decide

/-- Two colour domains give the minimum positive second-neighbour distance. -/
theorem six_site_two_domain_witness : Realizes 3 2 4 := by
  refine ⟨[false, false, false, true, true, true], ?_, by decide, by decide⟩
  constructor <;> decide

end Fireworks
