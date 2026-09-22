-- gold_chronotype_cohort_metrics
--
-- The analytical layer for the cohort comparison. Also derived from the daily
-- table, so the cohort numbers reconcile exactly with what a clinician sees.
--
-- GROUPING SETS gives both grains in one pass: a study-wide row per chronotype
-- (study_week IS NULL) and a per-week row per chronotype, so the biostatistics
-- team can look at a trend and the clinical team at a headline from the same
-- table.
--
-- Chronotype A = morning type, B = evening type (participants.csv).
SELECT
    CASE WHEN study_week IS NULL THEN 'study' ELSE 'week' END      AS period_type,
    COALESCE(CAST(study_week AS VARCHAR), 'all')                   AS period_label,
    study_week,
    chronotype_declared                                            AS chronotype,
    COUNT(DISTINCT participant_id)                                 AS n_participants,
    COUNT(*)                                                       AS n_participant_days,
    COUNT(*) FILTER (WHERE is_complete_day)                        AS n_complete_days,
    COUNT(*) FILTER (WHERE has_sleep_session)                      AS n_nights,

    -- sleep timing: the primary chronotype signal -------------------------
    ROUND(AVG(mid_sleep_hour), 3)                                  AS mid_sleep_hour_mean,
    ROUND(STDDEV_SAMP(mid_sleep_hour), 3)                          AS mid_sleep_hour_sd,
    ROUND(MEDIAN(mid_sleep_hour), 3)                               AS mid_sleep_hour_median,
    ROUND(AVG(sleep_onset_hour), 3)                                AS sleep_onset_hour_mean,

    -- sleep quality --------------------------------------------------------
    ROUND(AVG(total_sleep_min), 1)                                 AS total_sleep_min_mean,
    ROUND(STDDEV_SAMP(total_sleep_min), 1)                         AS total_sleep_min_sd,
    ROUND(AVG(sleep_efficiency_pct), 2)                            AS sleep_efficiency_pct_mean,
    ROUND(AVG(deep_sleep_pct), 2)                                  AS deep_sleep_pct_mean,
    ROUND(AVG(rem_sleep_pct), 2)                                   AS rem_sleep_pct_mean,
    ROUND(AVG(restlessness), 4)                                    AS restlessness_mean,

    -- activity: the second half of the chronotype picture ------------------
    ROUND(AVG(activity_midpoint_hour) FILTER (WHERE is_complete_day), 3)
                                                                   AS activity_midpoint_hour_mean,
    ROUND(AVG(steps_total)    FILTER (WHERE is_complete_day), 1)   AS steps_total_mean,
    ROUND(STDDEV_SAMP(steps_total) FILTER (WHERE is_complete_day), 1) AS steps_total_sd,
    ROUND(AVG(active_minutes) FILTER (WHERE is_complete_day), 1)   AS active_minutes_mean,

    -- physiology and self-report -------------------------------------------
    ROUND(AVG(hr_resting_bpm) FILTER (WHERE is_complete_day), 2)   AS hr_resting_bpm_mean,
    ROUND(AVG(fatigue_score), 2)                                   AS fatigue_score_mean,
    ROUND(AVG(stress_score), 2)                                    AS stress_score_mean,
    ROUND(AVG(readiness_score), 2)                                 AS readiness_score_mean,
    ROUND(AVG(sleep_quality_score), 2)                             AS sleep_quality_score_mean
FROM gold_daily_participant_metrics
WHERE chronotype_declared IS NOT NULL
GROUP BY GROUPING SETS ((chronotype_declared), (chronotype_declared, study_week))
ORDER BY period_type DESC, study_week NULLS FIRST, chronotype
