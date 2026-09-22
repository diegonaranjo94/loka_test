-- Analytical query 2 - does the observed data agree with the declared label?
--
-- participants.csv hands us chronotype A/B as ground truth. This derives a
-- chronotype independently from each participant's own median mid-sleep time
-- (median split across the cohort) and checks agreement. If the two disagree
-- badly, either the label is unreliable or our sleep derivation is - and both
-- are worth knowing before anyone publishes a cohort finding.
WITH per_participant AS (
    SELECT
        participant_id,
        ANY_VALUE(chronotype_declared)                              AS chronotype_declared,
        MEDIAN(mid_sleep_hour)                                      AS mid_sleep_median,
        COUNT(*) FILTER (WHERE has_sleep_session)                   AS nights
    FROM gold_daily_participant_metrics
    WHERE has_sleep_session
    GROUP BY participant_id
),
cut AS (
    SELECT MEDIAN(mid_sleep_median) AS threshold FROM per_participant
),
labelled AS (
    SELECT
        p.*,
        c.threshold,
        CASE WHEN p.mid_sleep_median <= c.threshold THEN 'A' ELSE 'B' END AS chronotype_derived
    FROM per_participant p CROSS JOIN cut c
)
SELECT
    chronotype_declared,
    chronotype_derived,
    COUNT(*)                                     AS n_participants,
    ROUND(MIN(mid_sleep_median), 3)              AS mid_sleep_median_min,
    ROUND(AVG(mid_sleep_median), 3)              AS mid_sleep_median_mean,
    ROUND(MAX(mid_sleep_median), 3)              AS mid_sleep_median_max,
    ROUND(ANY_VALUE(threshold), 3)               AS split_threshold_hour,
    (chronotype_declared = chronotype_derived)   AS agrees
FROM labelled
GROUP BY chronotype_declared, chronotype_derived
ORDER BY chronotype_declared, chronotype_derived
