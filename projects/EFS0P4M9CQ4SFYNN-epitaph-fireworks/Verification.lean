import Fireworks

namespace Fireworks.Verification

/-- A separately written check of the full mathematical statement. The population
constraints and attainable-pair formula are expanded rather than hidden behind
the project's `Balanced`, `Realizes`, or `BalancedSpectrum` predicates. -/
theorem full_statement (m h k : Nat) (hm : 0 < m) :
    (∃ w : List Bool,
      w.count false = m ∧ w.count true = m ∧ cyclicD1 w = h ∧ cyclicD2 w = k) ↔
    0 < h ∧ h % 2 = 0 ∧
      ((h = 2 * m ∧ k = 0) ∨
        (h < 2 * m ∧ k % 2 = 0 ∧ 4 ≤ k ∧ k ≤ 2 * h ∧
          k + 2 * h ≤ 4 * m)) := by
  simpa only [Realizes, Balanced, BalancedSpectrum, and_assoc] using
    (balanced_binary_ring_spectrum (h := h) (k := k) hm)

/-- The compact formula stated in the mathematical notes, checked against the
same theorem about arbitrary actual Boolean words. -/
theorem readable_statement (m r k : Nat) (hm : 0 < m) :
    (∃ w : List Bool,
      w.count false = m ∧ w.count true = m ∧
      cyclicD1 w = 2 * r ∧ cyclicD2 w = k) ↔
    0 < r ∧ r ≤ m ∧
      ((r = m ∧ k = 0) ∨
        (r < m ∧ k % 2 = 0 ∧ 4 ≤ k ∧ k ≤ 4 * min r (m - r))) := by
  simpa only [Realizes, Balanced, and_assoc] using
    (balanced_binary_ring_spectrum_twice (r := r) (k := k) hm)

/-- The small ring has two oriented nearest-neighbour mismatches and zero
distance-two mismatches, exactly as the modulo-index convention requires. -/
theorem two_site_boundary_case : Realizes 1 2 0 := by
  refine ⟨[false, true], ?_, by decide, by decide⟩
  constructor <;> decide

end Fireworks.Verification

#print axioms Fireworks.Verification.full_statement
#print axioms Fireworks.Verification.readable_statement
#print axioms Fireworks.Verification.two_site_boundary_case
#print axioms Fireworks.balanced_binary_ring_spectrum
#print axioms Fireworks.balanced_word_decomposition
#print axioms Fireworks.cyclicD2_append_comm
#print axioms Fireworks.expand_d2
#print axioms Fireworks.two_compositions_spectrum
#print axioms Fireworks.next_neighbour_distance_ne_two
