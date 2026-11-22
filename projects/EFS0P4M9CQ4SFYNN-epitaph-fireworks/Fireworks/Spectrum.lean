import Fireworks.Composition

namespace Fireworks

/-- The complete proposed distance spectrum of a nonempty balanced binary ring.
Here `m` is the population of each colour, `h` the nearest-neighbour distance,
and `k` the next-nearest-neighbour distance. All bounds are exact. -/
def BalancedSpectrum (m h k : Nat) : Prop :=
  0 < h ∧ h % 2 = 0 ∧
    ((h = 2 * m ∧ k = 0) ∨
      (h < 2 * m ∧ k % 2 = 0 ∧ 4 ≤ k ∧ k ≤ 2 * h ∧ k + 2 * h ≤ 4 * m))

/-- Arithmetic form used by the positive-run construction. -/
theorem balancedSpectrum_iff_run_parameters {m h k : Nat} :
    BalancedSpectrum m h k ↔
    ∃ r u : Nat, 0 < r ∧ h = 2 * r ∧ k = 2 * u ∧
      ((m = r ∧ u = 0) ∨
        (r < m ∧ 2 ≤ u ∧ u ≤ 2 * r ∧ u + 2 * r ≤ 2 * m)) := by
  constructor
  · rintro ⟨hh, heven, hp⟩
    refine ⟨h / 2, k / 2, ?_, ?_, ?_, ?_⟩
    · omega
    · omega
    · rcases hp with ⟨hhm, hk⟩ | ⟨hhm, hkeven, hk, hkh, hkm⟩ <;> omega
    · rcases hp with ⟨hhm, hk⟩ | ⟨hhm, hkeven, hk, hkh, hkm⟩
      · left
        omega
      · right
        omega
  · rintro ⟨r, u, hr, hh, hk, hp⟩
    refine ⟨by omega, by omega, ?_⟩
    rcases hp with ⟨hm, hu⟩ | ⟨hm, hu, hur, hum⟩
    · left
      omega
    · right
      omega

/-- The `r` formulation: except for perfect alternation, every allowed even `k`
between four and `4 * min r (m-r)` occurs. -/
theorem balancedSpectrum_twice_iff {m r k : Nat} :
    BalancedSpectrum m (2 * r) k ↔
    0 < r ∧ r ≤ m ∧
      ((r = m ∧ k = 0) ∨
        (r < m ∧ k % 2 = 0 ∧ 4 ≤ k ∧ k ≤ 4 * r ∧ k ≤ 4 * (m - r))) := by
  unfold BalancedSpectrum
  omega

/-- Positive equal-composition runs realize exactly the arithmetic spectrum. -/
theorem balancedSpectrum_iff_compositions {m h k : Nat} :
    BalancedSpectrum m h k ↔
    ∃ xs ys : List Nat,
      0 < xs.length ∧ xs.length = ys.length ∧
      xs.sum = m ∧ ys.sum = m ∧ Positive xs ∧ Positive ys ∧
      h = 2 * xs.length ∧ k = 2 * (longRuns xs + longRuns ys) := by
  rw [balancedSpectrum_iff_run_parameters]
  constructor
  · rintro ⟨r, u, hr, hh, hk, hp⟩
    obtain ⟨xs, ys, hxl, hyl, hxs, hys, hxp, hyp, hq⟩ :=
      two_compositions_spectrum.mpr hp
    exact ⟨xs, ys, by omega, by omega, hxs, hys, hxp, hyp, by omega, by omega⟩
  · rintro ⟨xs, ys, hr, hl, hxs, hys, hxp, hyp, hh, hk⟩
    refine ⟨xs.length, longRuns xs + longRuns ys, hr, hh, hk, ?_⟩
    exact two_compositions_spectrum.mp ⟨xs, ys, rfl, hl.symm, hxs, hys, hxp, hyp, rfl⟩

end Fireworks
