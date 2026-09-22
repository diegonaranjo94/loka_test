-- Analytical query 4 - one row per participant, for the effect-size test.
--
-- Collapsing to the participant before comparing cohorts matters: 1 393
-- nights are not 1 393 independent observations, they are 50 participants
-- measured 28 times each. Testing on night-level rows would shrink the
-- standard error by ~5x and manufacture significance out of nothing.
SELECT
    participant_id,
    ANY_VALUE(chronotype_declared)                          AS chronotype,
    MEDIAN(mid_sleep_hour)                                  AS mid_sleep_hour_median,
    MEDIAN(sleep_onset_hour)                                AS sleep_onset_hour_median,
    AVG(total_sleep_min)                                    AS total_sleep_min_mean,
    AVG(sleep_efficiency_pct)                               AS sleep_efficiency_pct_mean,
    AVG(deep_sleep_pct)                                     AS deep_sleep_pct_mean,
    AVG(steps_total)          FILTER (WHERE is_complete_day) AS steps_total_mean,
    AVG(activity_midpoint_hour) FILTER (WHERE is_complete_day) AS activity_midpoint_hour_mean,
    AVG(hr_resting_bpm)       FILTER (WHERE is_complete_day) AS hr_resting_bpm_mean,
    COUNT(*) FILTER (WHERE has_sleep_session)               AS nights
FROM gold_daily_participant_metrics
WHERE chronotype_declared IS NOT NULL
GROUP BY participant_id
ORDER BY participant_id
