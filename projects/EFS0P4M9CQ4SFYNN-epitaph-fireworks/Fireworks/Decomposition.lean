import Fireworks.BinaryRing

namespace Fireworks

/-- Every binary word consists of an initial true run, alternating positive
false/true run pairs, and a final false run. The two outer runs may be empty. -/
theorem decompose_linear (xs : List Bool) :
    ∃ p q a b, Positive a ∧ Positive b ∧ a.length = b.length ∧
      xs = List.replicate p true ++ (expand a b ++ List.replicate q false) := by
  induction xs with
  | nil => exact ⟨0, 0, [], [], by simp [Positive], by simp [Positive], rfl, rfl⟩
  | cons x xs ih =>
    obtain ⟨p, q, a, b, ha, hb, hab, hx⟩ := ih
    cases x with
    | true =>
      refine ⟨p + 1, q, a, b, ha, hb, hab, ?_⟩
      simp only [List.replicate_succ, List.cons_append, hx]
    | false =>
      cases p with
      | succ p =>
        refine ⟨0, q, 1 :: a, (p + 1) :: b, ?_, ?_, ?_, ?_⟩
        · intro c hc
          simp only [List.mem_cons] at hc
          rcases hc with rfl | hc
          · omega
          · exact ha c hc
        · intro c hc
          simp only [List.mem_cons] at hc
          rcases hc with rfl | hc
          · omega
          · exact hb c hc
        · simpa using hab
        · simp [expand, hx, List.replicate_succ, List.append_assoc]
      | zero =>
        cases a with
        | nil =>
          have hb0 : b = [] := by simpa using hab.symm
          subst b
          refine ⟨0, q + 1, [], [], ha, hb, rfl, ?_⟩
          simpa [expand, List.replicate_succ] using congrArg (List.cons false) hx
        | cons c cs =>
          cases b with
          | nil => simp at hab
          | cons d ds =>
            refine ⟨0, q, (c + 1) :: cs, d :: ds, ?_, hb, hab, ?_⟩
            · intro z hz
              simp only [List.mem_cons] at hz
              rcases hz with rfl | hz
              · omega
              · exact ha z (by simp [hz])
            · simpa [expand, List.replicate_succ, List.append_assoc] using
                congrArg (List.cons false) hx

/-- A word that starts with false and ends with true has a positive alternating
run decomposition without any leftover boundary pieces. -/
theorem decompose_ends (xs : List Bool)
    (hfirst : xs.head? = some false) (hlast : xs.getLast? = some true) :
    ∃ a b, Positive a ∧ Positive b ∧ a.length = b.length ∧ xs = expand a b := by
  obtain ⟨p, q, a, b, ha, hb, hab, hx⟩ := decompose_linear xs
  have hp : p = 0 := by
    by_cases hp : p = 0
    · exact hp
    · rw [hx] at hfirst
      simp [List.head?_replicate, hp] at hfirst
  have hq : q = 0 := by
    by_cases hq : q = 0
    · exact hq
    · rw [hx] at hlast
      simp [List.getLast?_replicate, hq] at hlast
  exact ⟨a, b, ha, hb, hab, by simpa [hp, hq] using hx⟩

private theorem decomp_expand_head (a b : List Nat) (ha : Positive a)
    (hab : a.length = b.length) (hne : a ≠ []) :
    (expand a b).head? = some false := by
  cases a with
  | nil => contradiction
  | cons c cs =>
    cases b with
    | nil => simp at hab
    | cons d ds =>
      have hc : c ≠ 0 := Nat.ne_of_gt (ha c (by simp))
      simp [expand, List.head?_replicate, hc]

private theorem decomp_expand_last (a b : List Nat) (hb : Positive b)
    (hab : a.length = b.length) (hne : a ≠ []) :
    (expand a b).getLast? = some true := by
  induction a generalizing b with
  | nil => contradiction
  | cons c cs ih =>
    cases b with
    | nil => simp at hab
    | cons d ds =>
      have hd : d ≠ 0 := Nat.ne_of_gt (hb d (by simp))
      have hbs : Positive ds := fun z hz => hb z (by simp [hz])
      have hlen : cs.length = ds.length := by simpa using hab
      by_cases hcs : cs = []
      · subst cs
        have hds : ds = [] := by simpa using hlen.symm
        subst ds
        simp [expand, List.getLast?_replicate, hd]
      · have ht := ih ds hbs hlen hcs
        simp [expand, ht]

/-- Any binary word containing both symbols admits a cut such that swapping the
two pieces places a true-to-false boundary at the cyclic origin. -/
theorem mixed_word_split (xs : List Bool) (hf : false ∈ xs) (ht : true ∈ xs) :
    ∃ u v, xs = u ++ v ∧
      (v ++ u).head? = some false ∧ (v ++ u).getLast? = some true := by
  obtain ⟨p, q, a, b, ha, hb, hab, hx⟩ := decompose_linear xs
  by_cases hp : p = 0
  · by_cases hq : q = 0
    · have hne : a ≠ [] := by
        intro hz
        simp [hx, hp, hq, hz, expand] at hf
      refine ⟨[], xs, by simp, ?_, ?_⟩
      · simpa [hx, hp, hq] using decomp_expand_head a b ha hab hne
      · simpa [hx, hp, hq] using decomp_expand_last a b hb hab hne
    · have hne : a ≠ [] := by
        intro hz
        simp [hx, hp, hz, expand] at ht
      refine ⟨expand a b, List.replicate q false, ?_, ?_, ?_⟩
      · simpa [hp] using hx
      · simp [List.head?_replicate, hq]
      · simp [decomp_expand_last a b hb hab hne]
  · refine ⟨List.replicate p true, expand a b ++ List.replicate q false, hx, ?_, ?_⟩
    · by_cases hne : a = []
      · have hq : q ≠ 0 := by
          intro hz
          simp [hx, hne, hz, expand] at hf
        simp [hne, expand, List.head?_replicate, hq]
      · simp [decomp_expand_head a b ha hab hne]
    · simp [List.getLast?_replicate, hp]

/-- Completeness of the positive run representation, up to one cyclic cut.
No maximal-run or representability hypothesis is imposed on the input word. -/
theorem mixed_word_decomposition (xs : List Bool)
    (hf : false ∈ xs) (ht : true ∈ xs) :
    ∃ u v a b, xs = u ++ v ∧ v ++ u = expand a b ∧
      Positive a ∧ Positive b ∧ a.length = b.length := by
  obtain ⟨u, v, huv, hfirst, hlast⟩ := mixed_word_split xs hf ht
  obtain ⟨a, b, ha, hb, hab, he⟩ := decompose_ends (v ++ u) hfirst hlast
  exact ⟨u, v, a, b, huv, he, ha, hb, hab⟩

/-- Every nonempty balanced binary cyclic word is represented by positive run
lengths of equal size and equal sum after exchanging two contiguous pieces. -/
theorem balanced_word_decomposition (xs : List Bool) (m : Nat) (hm : 0 < m)
    (hf : xs.count false = m) (ht : xs.count true = m) :
    ∃ u v a b, xs = u ++ v ∧ v ++ u = expand a b ∧
      Positive a ∧ Positive b ∧ a.length = b.length ∧
      a.sum = m ∧ b.sum = m ∧ 0 < a.length := by
  have hfm : false ∈ xs := List.count_pos_iff.mp (by omega)
  have htm : true ∈ xs := List.count_pos_iff.mp (by omega)
  obtain ⟨u, v, a, b, huv, he, ha, hb, hab⟩ := mixed_word_decomposition xs hfm htm
  have hfc : (v ++ u).count false = m := by
    rw [huv, List.count_append] at hf
    simp only [List.count_append]
    omega
  have htc : (v ++ u).count true = m := by
    rw [huv, List.count_append] at ht
    simp only [List.count_append]
    omega
  rw [he, expand_count_false a b hab] at hfc
  rw [he, expand_count_true a b hab] at htc
  have hapos : 0 < a.length := by
    cases a with
    | nil => simp at hfc; omega
    | cons => simp
  exact ⟨u, v, a, b, huv, he, ha, hb, hab, hfc, htc, hapos⟩

end Fireworks
