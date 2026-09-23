-- Analytical query 3 - weekly trend by cohort
--
-- The same metrics the clinical team sees daily, rolled to the grain the
-- biostatistics team works in, split by chronotype. Demonstrates that both
-- consumers are served from one lineage: this reads the cohort table, which
-- reads the daily table, which reads Silver.
SELECT
    study_week,
    chronotype,
    n_participants,
    n_complete_days,
    n_nights,
    mid_sleep_hour_mean,
    total_sleep_min_mean,
    sleep_efficiency_pct_mean,
    deep_sleep_pct_mean,
    activity_midpoint_hour_mean,
    steps_total_mean,
    hr_resting_bpm_mean,
    readiness_score_mean
FROM gold_chronotype_cohort_metrics
WHERE period_type = 'week'
ORDER BY study_week, chronotype
