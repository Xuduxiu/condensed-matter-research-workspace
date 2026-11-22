import Fireworks.BinaryRing

namespace Fireworks
namespace RotationAux

theorem scan1_append (p : Bool) (xs ys : List Bool) :
    scan1 p (xs ++ ys) = scan1 p xs + scan1 (xs.getLastD p) ys := by
  induction xs generalizing p with
  | nil => simp [scan1, List.getLastD_nil]
  | cons x xs ih =>
      simp only [List.cons_append, scan1, List.getLastD_cons, ih]
      omega

theorem end2_append (p q : Bool) (xs ys : List Bool) :
    end2 p q (xs ++ ys) =
      end2 (end2 p q xs).1 (end2 p q xs).2 ys := by
  induction xs generalizing p q with
  | nil => rfl
  | cons x xs ih => simpa only [List.cons_append, end2] using ih q x

theorem scan2_append (p q : Bool) (xs ys : List Bool) :
    scan2 p q (xs ++ ys) = scan2 p q xs +
      scan2 (end2 p q xs).1 (end2 p q xs).2 ys := by
  induction xs generalizing p q with
  | nil => simp [scan2, end2]
  | cons x xs ih =>
      simp only [List.cons_append, scan2, end2, ih]
      omega

theorem end2_irrel (p q p' q' : Bool) (xs : List Bool) (hn : 2 ≤ xs.length) :
    end2 p q xs = end2 p' q' xs := by
  cases xs with
  | nil => simp at hn
  | cons x xs =>
      cases xs with
      | nil => simp at hn
      | cons y ys => rfl

theorem end2_snd_irrel (p p' q : Bool) (xs : List Bool) :
    (end2 p q xs).2 = (end2 p' q xs).2 := by
  cases xs <;> rfl

theorem cyclicD2_eq_scan (xs : List Bool) (hn : 2 ≤ xs.length) :
    cyclicD2 xs = scan2 (end2 false false xs).1 (end2 false false xs).2 xs := by
  unfold cyclicD2
  rw [end2_append, end2_irrel _ _ false false xs hn]

end RotationAux

/-- Moving the first symbol to the end preserves nearest-neighbour distance. -/
theorem cyclicD1_rotate_one (x : Bool) (xs : List Bool) :
    cyclicD1 (x :: xs) = cyclicD1 (xs ++ [x]) := by
  simp only [cyclicD1, List.getLastD_cons, List.getLastD_concat,
    scan1, RotationAux.scan1_append]
  omega

/-- Moving the first symbol to the end preserves next-nearest-neighbour distance. -/
theorem cyclicD2_rotate_one (x : Bool) (xs : List Bool) :
    cyclicD2 (x :: xs) = cyclicD2 (xs ++ [x]) := by
  cases xs with
  | nil => rfl
  | cons y ys =>
      have hleft : 2 ≤ (x :: y :: ys).length := by simp
      have hright : 2 ≤ ((y :: ys) ++ [x]).length := by simp
      rw [RotationAux.cyclicD2_eq_scan _ hleft, RotationAux.cyclicD2_eq_scan _ hright]
      simp only [List.cons_append, end2, RotationAux.end2_append]
      simp only [end2, scan2, RotationAux.scan2_append]
      rw [RotationAux.end2_snd_irrel false x y ys]
      omega

/-- Exchanging a prefix and suffix is a cyclic rotation. -/
theorem cyclicD1_append_comm (xs ys : List Bool) :
    cyclicD1 (xs ++ ys) = cyclicD1 (ys ++ xs) := by
  induction xs generalizing ys with
  | nil => simp
  | cons x xs ih =>
      rw [List.cons_append, cyclicD1_rotate_one, List.append_assoc,
        ih (ys ++ [x])]
      simp [List.append_assoc]

/-- Both distance observables are invariant under the same cyclic rotations. -/
theorem cyclicD2_append_comm (xs ys : List Bool) :
    cyclicD2 (xs ++ ys) = cyclicD2 (ys ++ xs) := by
  induction xs generalizing ys with
  | nil => simp
  | cons x xs ih =>
      rw [List.cons_append, cyclicD2_rotate_one, List.append_assoc,
        ih (ys ++ [x])]
      simp [List.append_assoc]

end Fireworks
