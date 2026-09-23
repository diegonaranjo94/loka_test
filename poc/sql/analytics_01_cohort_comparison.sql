-- Analytical query 1 - chronotype cohort comparison (the headline deliverable)
--
-- Morning types (A) vs evening types (B) across sleep timing, sleep
-- architecture, activity and self-report. Reads the analytical layer only;
-- no Silver, no Bronze.
WITH s AS (
    SELECT * FROM gold_chronotype_cohort_metrics WHERE period_type = 'study'
),
ab AS (
    SELECT
        MAX(n_participants)               FILTER (WHERE chronotype = 'A') AS a_n,
        MAX(n_participants)               FILTER (WHERE chronotype = 'B') AS b_n,
        MAX(n_nights)                     FILTER (WHERE chronotype = 'A') AS a_nights,
        MAX(n_nights)                     FILTER (WHERE chronotype = 'B') AS b_nights,
        MAX(n_complete_days)              FILTER (WHERE chronotype = 'A') AS a_days,
        MAX(n_complete_days)              FILTER (WHERE chronotype = 'B') AS b_days,
        MAX(mid_sleep_hour_mean)          FILTER (WHERE chronotype = 'A') AS a_mid,
        MAX(mid_sleep_hour_mean)          FILTER (WHERE chronotype = 'B') AS b_mid,
        MAX(sleep_onset_hour_mean)        FILTER (WHERE chronotype = 'A') AS a_onset,
        MAX(sleep_onset_hour_mean)        FILTER (WHERE chronotype = 'B') AS b_onset,
        MAX(total_sleep_min_mean)         FILTER (WHERE chronotype = 'A') AS a_sleep,
        MAX(total_sleep_min_mean)         FILTER (WHERE chronotype = 'B') AS b_sleep,
        MAX(sleep_efficiency_pct_mean)    FILTER (WHERE chronotype = 'A') AS a_eff,
        MAX(sleep_efficiency_pct_mean)    FILTER (WHERE chronotype = 'B') AS b_eff,
        MAX(deep_sleep_pct_mean)          FILTER (WHERE chronotype = 'A') AS a_deep,
        MAX(deep_sleep_pct_mean)          FILTER (WHERE chronotype = 'B') AS b_deep,
        MAX(rem_sleep_pct_mean)           FILTER (WHERE chronotype = 'A') AS a_rem,
        MAX(rem_sleep_pct_mean)           FILTER (WHERE chronotype = 'B') AS b_rem,
        MAX(restlessness_mean)            FILTER (WHERE chronotype = 'A') AS a_rest,
        MAX(restlessness_mean)            FILTER (WHERE chronotype = 'B') AS b_rest,
        MAX(activity_midpoint_hour_mean)  FILTER (WHERE chronotype = 'A') AS a_actmid,
        MAX(activity_midpoint_hour_mean)  FILTER (WHERE chronotype = 'B') AS b_actmid,
        MAX(steps_total_mean)             FILTER (WHERE chronotype = 'A') AS a_steps,
        MAX(steps_total_mean)             FILTER (WHERE chronotype = 'B') AS b_steps,
        MAX(active_minutes_mean)          FILTER (WHERE chronotype = 'A') AS a_act,
        MAX(active_minutes_mean)          FILTER (WHERE chronotype = 'B') AS b_act,
        MAX(hr_resting_bpm_mean)          FILTER (WHERE chronotype = 'A') AS a_rhr,
        MAX(hr_resting_bpm_mean)          FILTER (WHERE chronotype = 'B') AS b_rhr,
        MAX(readiness_score_mean)         FILTER (WHERE chronotype = 'A') AS a_ready,
        MAX(readiness_score_mean)         FILTER (WHERE chronotype = 'B') AS b_ready,
        MAX(sleep_quality_score_mean)     FILTER (WHERE chronotype = 'A') AS a_sq,
        MAX(sleep_quality_score_mean)     FILTER (WHERE chronotype = 'B') AS b_sq,
        MAX(fatigue_score_mean)           FILTER (WHERE chronotype = 'A') AS a_fat,
        MAX(fatigue_score_mean)           FILTER (WHERE chronotype = 'B') AS b_fat
    FROM s
)
SELECT * FROM (
    SELECT 1 AS ord, 'Participants'                       AS metric, ''       AS unit, CAST(a_n      AS DOUBLE) AS morning_type_a, CAST(b_n      AS DOUBLE) AS evening_type_b FROM ab
    UNION ALL SELECT  2, 'Nights with a valid sleep session', 'nights', CAST(a_nights AS DOUBLE), CAST(b_nights AS DOUBLE) FROM ab
    UNION ALL SELECT  3, 'Complete participant-days',         'days',   CAST(a_days   AS DOUBLE), CAST(b_days   AS DOUBLE) FROM ab
    UNION ALL SELECT 10, 'Mid-sleep time',                    'h from midnight', a_mid,    b_mid    FROM ab
    UNION ALL SELECT 11, 'Sleep onset time',                  'h from midnight', a_onset,  b_onset  FROM ab
    UNION ALL SELECT 12, 'Total sleep',                       'min',    a_sleep,  b_sleep  FROM ab
    UNION ALL SELECT 13, 'Sleep efficiency (stage-derived)',  '%',      a_eff,    b_eff    FROM ab
    UNION ALL SELECT 14, 'Deep sleep',                        '% of sleep', a_deep, b_deep FROM ab
    UNION ALL SELECT 15, 'REM sleep',                         '% of sleep', a_rem,  b_rem  FROM ab
    UNION ALL SELECT 16, 'Restlessness index',                '0-1',    a_rest,   b_rest   FROM ab
    UNION ALL SELECT 20, 'Activity midpoint (step-weighted)', 'h of day', a_actmid, b_actmid FROM ab
    UNION ALL SELECT 21, 'Daily steps',                       'steps',  a_steps,  b_steps  FROM ab
    UNION ALL SELECT 22, 'Active minutes per day',            'min',    a_act,    b_act    FROM ab
    UNION ALL SELECT 30, 'Resting heart rate (p5)',           'bpm',    a_rhr,    b_rhr    FROM ab
    UNION ALL SELECT 40, 'Readiness score',                   '0-10',   a_ready,  b_ready  FROM ab
    UNION ALL SELECT 41, 'Sleep quality score',               '1-5',    a_sq,     b_sq     FROM ab
    UNION ALL SELECT 42, 'Fatigue score',                     '1-5',    a_fat,    b_fat    FROM ab
) t
ORDER BY ord
