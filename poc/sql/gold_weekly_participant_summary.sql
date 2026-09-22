-- gold_weekly_participant_summary
--
-- The biostatistics consumer. Same source of truth as the clinical daily
-- table (it reads FROM that table, not from Silver), different schema:
-- one row per participant-week, every metric carried as mean + SD + n so a
-- downstream model never has to re-derive dispersion or guess how many days
-- actually contributed.
--
-- Two separate denominators on purpose:
--   * activity metrics are averaged over COMPLETE days only - a 6-hour
--     outage day would otherwise drag a weekly step mean down by 25% and look
--     like a behavioural change.
--   * sleep metrics are session-derived and independent of minute coverage,
--     so they use every night that produced a valid session.
SELECT
    participant_id,
    study_week,
    MIN(date)                                           AS week_start,
    MAX(date)                                           AS week_end,
    ANY_VALUE(chronotype_declared)                      AS chronotype_declared,
    ANY_VALUE(age)                                      AS age,
    ANY_VALUE(gender)                                   AS gender,

    -- denominators ---------------------------------------------------------
    COUNT(*)                                            AS days_in_week,
    COUNT(*) FILTER (WHERE is_complete_day)             AS days_complete,
    COUNT(*) FILTER (WHERE is_missing_day)              AS days_missing,
    COUNT(*) FILTER (WHERE has_sleep_session)           AS nights_with_sleep,
    COUNT(*) FILTER (WHERE has_survey)                  AS days_with_survey,
    ROUND(AVG(coverage_pct), 2)                         AS mean_coverage_pct,

    -- activity (complete days only) ----------------------------------------
    ROUND(AVG(steps_total)    FILTER (WHERE is_complete_day), 1)      AS steps_total_mean,
    ROUND(STDDEV_SAMP(steps_total) FILTER (WHERE is_complete_day), 1) AS steps_total_sd,
    SUM(steps_total)          FILTER (WHERE is_complete_day)          AS steps_total_sum,
    ROUND(AVG(active_minutes) FILTER (WHERE is_complete_day), 1)      AS active_minutes_mean,
    ROUND(AVG(activity_midpoint_hour) FILTER (WHERE is_complete_day), 3)
                                                                      AS activity_midpoint_hour_mean,

    -- heart rate (complete days only) ---------------------------------------
    ROUND(AVG(hr_resting_bpm) FILTER (WHERE is_complete_day), 2)      AS hr_resting_bpm_mean,
    ROUND(STDDEV_SAMP(hr_resting_bpm) FILTER (WHERE is_complete_day), 2) AS hr_resting_bpm_sd,
    ROUND(AVG(hr_mean_bpm)    FILTER (WHERE is_complete_day), 2)      AS hr_mean_bpm_mean,
    ROUND(MAX(hr_max_bpm)     FILTER (WHERE is_complete_day), 0)      AS hr_max_bpm_week,

    -- sleep (every valid session) --------------------------------------------
    ROUND(AVG(total_sleep_min), 1)                      AS total_sleep_min_mean,
    ROUND(STDDEV_SAMP(total_sleep_min), 1)              AS total_sleep_min_sd,
    ROUND(AVG(sleep_efficiency_pct), 2)                 AS sleep_efficiency_pct_mean,
    ROUND(STDDEV_SAMP(sleep_efficiency_pct), 2)         AS sleep_efficiency_pct_sd,
    ROUND(AVG(deep_sleep_pct), 2)                       AS deep_sleep_pct_mean,
    ROUND(AVG(rem_sleep_pct), 2)                        AS rem_sleep_pct_mean,
    ROUND(AVG(mid_sleep_hour), 3)                       AS mid_sleep_hour_mean,
    ROUND(STDDEV_SAMP(mid_sleep_hour), 3)               AS mid_sleep_hour_sd,
    ROUND(AVG(sleep_onset_hour), 3)                     AS sleep_onset_hour_mean,
    ROUND(AVG(restlessness), 4)                         AS restlessness_mean,

    -- self-report -------------------------------------------------------------
    ROUND(AVG(fatigue_score), 2)                        AS fatigue_score_mean,
    ROUND(AVG(stress_score), 2)                         AS stress_score_mean,
    ROUND(AVG(readiness_score), 2)                      AS readiness_score_mean,
    ROUND(AVG(sleep_quality_score), 2)                  AS sleep_quality_score_mean,

    -- quality carried forward, never silently dropped -------------------------
    SUM(minutes_nulled)                                 AS minutes_nulled,
    SUM(hr_stuck_minutes)                               AS hr_stuck_minutes,
    SUM(minutes_unit_rescaled)                          AS minutes_unit_rescaled
FROM gold_daily_participant_metrics
GROUP BY participant_id, study_week
ORDER BY participant_id, study_week
