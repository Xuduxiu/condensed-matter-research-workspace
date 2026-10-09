# Methodology

## Corpus boundary

The radar separates data sources into three scopes instead of mixing every paper into one ranking.

### Core journals

The main hot list defaults to core journals:

- Physical Review Letters
- Physical Review X
- Nature
- Science
- Nature Physics
- Nature Materials
- Nature Nanotechnology
- Nature Communications
- Science Advances
- npj Quantum Materials

### Context journals

Context journals support historical baseline and background checks. They are available in the UI through `Core + context`, but they do not enter the default core hot list.

- Physical Review B
- 2D Materials
- Nano Letters
- ACS Nano
- Advanced Materials
- Advanced Functional Materials
- Materials Today Physics
- npj 2D Materials and Applications

### Preprints

arXiv cond-mat records are used for early-warning and lifecycle context when `scope=all` or `--include-arxiv` is used. They are not directly mixed into the default core hot list.

## Baseline and trend windows

The system distinguishes field history from recent display windows:

- `historical_first_seen`: first nonzero month inside the long baseline window.
- `trend_first_seen`: first nonzero month inside the active trend window.
- Default baseline: `2015-01-01` to today.
- Default trend window: latest 24 months.

A recent two-year window is not enough to determine whether a concept is new in condensed matter physics. It only tells us when a term first appeared in the selected display interval. Lifecycle and novelty therefore use the long historical baseline.

## Condensed matter filter

Filtering is implemented in `backend/nlp/condmat_filter.py`.

Layer A keeps records when source concepts, arXiv categories, journal metadata, title, or abstract contain hard condensed-matter terms such as condensed matter physics, quantum materials, superconductivity, magnetism, topological semimetal, or strongly correlated electrons.

Layer B keeps records when title or abstract contains domain keywords such as superconductivity, quantum Hall, Chern insulator, topological, Weyl, Dirac semimetal, spin liquid, Kitaev, moiré, flat band, charge density wave, Mott, Hubbard, strange metal, exciton, polariton, van der Waals, TMD, WSe2, MoS2, ZrTe5, FeSe, nickelate, and altermagnet.

Layer C excludes obvious non-condensed-matter areas when no keep rule has fired. Photonics and quantum optics are not globally excluded because exciton-polariton, cavity material, and 2D semiconductor work may be relevant.

## Phrase extraction

Term extraction is implemented in `backend/nlp/extract_terms.py`.

Each paper is processed from title plus abstract. The extractor returns paper-level terms, not repeated token counts. The term types are:

- `concept`
- `material`
- `method`

Lifecycle further maps terms to concept classes:

- `physics_concept`
- `material_system`
- `method`
- `platform_material`
- `general_field`

Platform materials, common methods, and general field terms are deliberately resistant to `emerging` classification.

## Normalization

Normalization is implemented in `backend/nlp/normalize.py` and the dictionaries in `backend/nlp/dictionaries.py`.

Examples:

- `moire` -> `moiré`
- `TBG` -> `twisted bilayer graphene`
- `QAH` -> `quantum anomalous Hall effect`
- `FQH` -> `fractional quantum Hall effect`
- `FCI` -> `fractional Chern insulator`
- `CDW` -> `charge density wave`
- `TMD` -> `transition metal dichalcogenide`
- `vdW` -> `van der Waals`
- `h-BN`, `hexagonal boron nitride`, `hexagonal BN`, `boron nitride`, `BN encapsulation` -> `hBN`

Raw `BN` is not globally normalized to `hBN`. It is only weak-mapped when contextual terms such as hexagonal, 2D, van der Waals, encapsulation, heterostructure, graphene, TMD, WSe2, MoS2, or MoTe2 are present.

## Deduplication

The update pipeline deduplicates within each run by DOI first. If DOI is absent, it uses a normalized title key. SQLite also enforces a unique DOI index for persisted records.

arXiv and published records should not be merged blindly. DOI is the authority key when present. Without DOI, title-level deduplication remains conservative.

## Reproducibility

Every update inserts a row into `update_runs` and writes `data/processed/update_*.json`. Logs include inserted papers, updated papers, duplicate count, failed request count, windows, scope, and whether mock data was used.

The deterministic mock corpus uses a fixed random seed and includes long-history hBN and graphene, post-2018 moiré, post-2023 fractional Chern insulator and altermagnetism, and intermittent ZrTe5.

## Display eligibility and term quality gates

Display filtering is implemented in `backend/nlp/term_filters.py`. Every extracted term receives:

- `display_eligible`: `1` if the term can be shown in rankings and visualizations.
- `display_reason`: a short reason such as `physics_dictionary`, `material_formula`, `method_dictionary`, or `generic_or_broken`.
- `source`: extraction source such as dictionary, synonym, material detector, method dictionary, or contextual rule.

The default UI filters out broad or broken fragments such as `Quantum`, `Band`, `Hall`, `Physics quantum materials`, and `Quantum materials van`. OpenAlex concepts are treated as weak evidence and are not expanded into arbitrary display n-grams.

## Material extraction

Material extraction is split from phrase extraction:

- `backend/nlp/material_registry.py`: canonical material registry and aliases.
- `backend/nlp/material_extract.py`: formula and phrase detector for material systems.

The detector recognizes canonical materials and high-confidence formulas such as hBN, graphene, WSe2, MoS2, ZrTe5, HfTe5, FeSe, MnBi2Te4, Cd3As2, TaAs, AV3Sb5, CsV3Sb5, KV3Sb5, and RbV3Sb5. Platform materials such as hBN and graphene are classified separately from physics concepts.

## Smoothing policy

Backend smoothing modes (`raw`, `rolling3`, `rolling6`, `ewma`) are available for concept detail and Compare views. Smoothing is a display transformation only. The raw monthly series remains the source for stored counts, lifecycle metrics, and exports.
