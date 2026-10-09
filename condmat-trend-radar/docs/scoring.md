# Scoring

## Paper-level occurrence

The default unit is paper-level occurrence:

```text
freq(term, month, scope) = number of distinct papers in that month and scope containing term
```

A term appearing multiple times in the same title or abstract counts once for that paper.

## Corpus scope

Monthly term statistics are stored by `corpus_scope`:

- `core`: core journal papers only.
- `core_context`: core plus context journals.
- `all`: core, context, and preprint sources.

The overview and heatmap default to `core`. Lifecycle and novelty use the `all` baseline so historical context is not lost.

## Journal weight

Journal-weighted frequency is:

```text
weighted_freq(term, month) = sum journal_weight(paper) for papers containing term
```

Weights:

```text
Nature / Science: 5
Nature Physics / Nature Materials / Nature Nanotechnology: 4
PRL / PRX: 4
Nature Communications / Science Advances / npj Quantum Materials: 3
PRB / Nano Letters / ACS Nano / Advanced Materials / 2D Materials: 2
arXiv only: 1
unknown metadata: 1.5
```

These weights are configurable in `backend/nlp/dictionaries.py`.

## Growth rate

For ranking rising and cooling concepts:

```text
growth = (freq_recent + 1) / (freq_previous + 1)
```

`freq_recent` is the last 3 months. `freq_previous` is the 3 months before that. The `+1` smoothing keeps rare new concepts finite.

## Momentum

Monthly momentum is:

```text
momentum = log(1 + weighted_freq) * growth_factor * citation_factor
```

The implementation uses:

```text
growth_factor = min(growth, 4)
citation_signal = min(log(1 + average_cited_by_count), 3)
citation_factor = 1 + 0.35 * citation_signal
```

This keeps citations from dominating very new papers while still making established high-signal papers visible.

## Novelty score

Novelty is based on the long baseline before the trend window:

```text
historical_count_before_trend = total paper-level count before trend_from
novelty_score = 1 / (1 + historical_count_before_trend)
```

A term with a large pre-trend history has a low novelty score. This prevents long-lived terms such as `hBN`, graphene, ARPES, STM, DFT, superconductivity, or quantum Hall effect from being classified as new simply because the UI is showing a recent two-year window.

## Concept half-life

For every concept:

1. Build monthly weighted-frequency series across the baseline.
2. Find `peak_month` and `peak_value`.
3. Search after the peak for the first month where value is less than or equal to `0.5 * peak_value`.
4. If found, `half_life_months` is the month distance from the peak.
5. If not found, half-life is `null` and the concept is treated as ongoing.

## Active and burst duration

Active duration:

```text
activity_threshold = max(1, 0.2 * peak_value)
active_duration_months = last_month_above_threshold - first_month_above_threshold + 1
```

Burst duration:

```text
burst_threshold = mean(series) + std(series)
burst_duration_months = longest continuous run above burst_threshold
```

## Status rules

Status uses historical first-seen and historical counts, not trend-window first-seen alone.

`emerging` requires all of:

```text
historical_count_before_trend <= 5
recent_3m_weighted_freq >= 3
growth_recent_vs_previous >= 2
trend_total_count >= 3
```

`persistent` is preferred when any of these hold:

```text
active_months_across_baseline >= 18
historical_count_before_trend >= 50
concept_class is platform_material, method, or general_field
is_platform_term is true
```

`active`:

```text
recent_6m_count > 0
current_freq >= 0.5 * peak_freq
historical_count_before_trend > 5
```

`cooling`:

```text
historically had a peak
recent_freq < 0.5 * peak_freq
recent_6m_count > 0
```

`stale`:

```text
historically existed
recent_6m_count == 0
```

## Normalized share metrics

Monthly denominators are stored in `monthly_corpus_stats`:

```text
total_papers(month, scope)
total_weighted_papers(month, scope)
```

Term statistics then include:

```text
normalized_share = raw_freq / total_papers
weighted_normalized_share = weighted_freq / total_weighted_papers
```

These metrics reduce artifacts from changing corpus size. They are useful when comparing quiet years against months with larger metadata coverage. The dashboard exposes both in Heatmap and Compare.

## Compare z-score

Compare supports `z_score` as a view-level metric. It standardizes the selected metric series inside the selected comparison window:

```text
z_score(month) = (value(month) - mean(window)) / std(window)
```

This is not stored in SQLite and should be read as a relative visual comparison, not an absolute scientific score.
