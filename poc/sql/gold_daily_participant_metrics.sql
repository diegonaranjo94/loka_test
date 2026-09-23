-- gold_daily_participant_metrics
--
-- THE single source of truth. The clinical team consumes this table directly
-- (daily grain, daily freshness); the weekly biostatistics table and the
-- chronotype cohort table are both derived from it, which is what stops the
-- two consumers' numbers from drifting apart.
--
-- The spine is the participant-day coverage grid, not the event tables. A day
-- with no data at all is still a row here, with coverage_pct = 0 - it has to
-- be, or a device outage silently looks like a rest day.
--
-- Nothing is filtered on quality. The flags travel with the row and the
-- consumer decides: `is_complete_day` is the one to filter on for
-- volume metrics (steps), while sleep metrics are session-derived and stay
-- valid regardless of minute-grid coverage.
WITH steps_daily AS (
    SELECT
        participant_id,
        date,
        SUM(steps)                                   AS steps_total,
        COUNT(*) FILTER (WHERE steps > 0)            AS active_minutes,
        MAX(steps)                                   AS steps_peak_minute,
        COUNT(*) FILTER (WHERE was_sentinel)         AS steps_minutes_nulled,
        COUNT(*) FILTER (WHERE was_rescaled)         AS steps_minutes_rescaled,
        -- Step-weighted mean hour of day: when this participant's activity
        -- actually happens. The activity half of the chronotype signal.
        SUM(steps * (hour(timestamp) + minute(timestamp) / 60.0))
            AS steps_hour_weight
    FROM steps_minute
    GROUP BY 1, 2
),
hr_daily AS (
    SELECT
        participant_id,
        date,
        ROUND(AVG(heart_rate_bpm) FILTER (WHERE NOT is_stuck), 1)  AS hr_mean_bpm,
        MIN(heart_rate_bpm)       FILTER (WHERE NOT is_stuck)      AS hr_min_bpm,
        MAX(heart_rate_bpm)       FILTER (WHERE NOT is_stuck)      AS hr_max_bpm,
        -- 5th percentile is a more robust resting-HR proxy than the minimum,
        -- which is one bad minute away from being wrong.
        ROUND(QUANTILE_CONT(heart_rate_bpm, 0.05)
              FILTER (WHERE NOT is_stuck), 1)                      AS hr_resting_bpm,
        COUNT(*) FILTER (WHERE is_stuck)                           AS hr_stuck_minutes,
        COUNT(*) FILTER (WHERE was_out_of_range)                   AS hr_minutes_nulled,
        COUNT(*) FILTER (WHERE was_utc)                            AS hr_minutes_tz_corrected
    FROM heart_rate_minute
    GROUP BY 1, 2
)
SELECT
    c.participant_id,
    c.date,
    CAST(1 + (c.date - DATE '2026-01-08') / 7 AS INTEGER)          AS study_week,
    CAST(1 + (c.date - DATE '2026-01-08')     AS INTEGER)          AS study_day,
    p.chronotype                                                    AS chronotype_declared,
    p.age,
    p.gender,
    p.max_heart_rate,

    -- activity ------------------------------------------------------------
    s.steps_total,
    s.active_minutes,
    s.steps_peak_minute,
    CASE WHEN s.steps_total > 0
         THEN ROUND(s.steps_hour_weight / s.steps_total, 3)
    END                                                             AS activity_midpoint_hour,

    -- heart rate ------------------------------------------------------------
    h.hr_mean_bpm,
    h.hr_resting_bpm,
    h.hr_min_bpm,
    h.hr_max_bpm,

    -- sleep (all stage-derived; the source efficiency_pct is not used) -------
    sl.total_sleep_min,
    sl.time_in_bed_min,
    ROUND(sl.sleep_efficiency_pct, 2)                               AS sleep_efficiency_pct,
    ROUND(sl.deep_sleep_pct, 2)                                     AS deep_sleep_pct,
    ROUND(sl.rem_sleep_pct, 2)                                      AS rem_sleep_pct,
    ROUND(sl.mid_sleep_hour_signed, 3)                              AS mid_sleep_hour,
    ROUND(sl.sleep_onset_hour_signed, 3)                            AS sleep_onset_hour,
    sl.restlessness,

    -- self-report ------------------------------------------------------------
    w.fatigue_score,
    w.stress_score,
    w.readiness_score,
    w.sleep_quality_score,

    -- provenance / quality ---------------------------------------------------
    c.steps_minutes,
    c.hr_minutes,
    ROUND(c.coverage_pct, 2)                                        AS coverage_pct,
    c.is_complete_day,
    c.is_missing_day,
    (sl.session_key IS NOT NULL)                                    AS has_sleep_session,
    (w.participant_id IS NOT NULL)                                  AS has_survey,
    COALESCE(s.steps_minutes_nulled, 0)
      + COALESCE(h.hr_minutes_nulled, 0)                            AS minutes_nulled,
    COALESCE(s.steps_minutes_rescaled, 0)                           AS minutes_unit_rescaled,
    COALESCE(h.hr_stuck_minutes, 0)                                 AS hr_stuck_minutes,
    COALESCE(h.hr_minutes_tz_corrected, 0)                          AS minutes_tz_corrected
FROM coverage c
LEFT JOIN participants   p  ON p.participant_id  = c.participant_id
LEFT JOIN steps_daily    s  ON s.participant_id  = c.participant_id AND s.date = c.date
LEFT JOIN hr_daily       h  ON h.participant_id  = c.participant_id AND h.date = c.date
LEFT JOIN sleep_sessions sl ON sl.participant_id = c.participant_id AND sl.date = c.date
LEFT JOIN survey         w  ON w.participant_id  = c.participant_id AND w.date = c.date
ORDER BY c.participant_id, c.date
