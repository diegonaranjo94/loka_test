# Analytical query output

Run `run_20260922T004952Z` - queries executed against the Gold layer (`poc/sql/analytics_*.sql`), no Silver or Bronze access.

Mid-sleep and onset times are expressed as hours either side of midnight (`-1.5` = 22:30, `+3.0` = 03:00) so evening and morning types sit on one continuous axis instead of wrapping at 24.

## Chronotype cohort comparison (whole study)

`analytics_01_cohort_comparison.sql`

| metric                            | unit            |   morning_type_a |   evening_type_b |   delta_b_minus_a |
|:----------------------------------|:----------------|-----------------:|-----------------:|------------------:|
| Participants                      |                 |          28      |          22      |            -6     |
| Nights with a valid sleep session | nights          |         779      |         614      |          -165     |
| Complete participant-days         | days            |         779      |         614      |          -165     |
| Mid-sleep time                    | h from midnight |           2.576  |           2.508  |            -0.068 |
| Sleep onset time                  | h from midnight |          -0.886  |          -0.897  |            -0.011 |
| Total sleep                       | min             |         414.5    |         407.5    |            -7     |
| Sleep efficiency (stage-derived)  | %               |          87.11   |          87.13   |             0.02  |
| Deep sleep                        | % of sleep      |          17.67   |          18.28   |             0.61  |
| REM sleep                         | % of sleep      |          20.32   |          20.76   |             0.44  |
| Restlessness index                | 0-1             |           0.0925 |           0.0918 |            -0.001 |
| Activity midpoint (step-weighted) | h of day        |          14.007  |          14.002  |            -0.005 |
| Daily steps                       | steps           |        8884      |        9035.2    |           151.2   |
| Active minutes per day            | min             |         932.1    |         931.8    |            -0.3   |
| Resting heart rate (p5)           | bpm             |          62.46   |          62.76   |             0.3   |
| Readiness score                   | 0-10            |           4.87   |           4.94   |             0.07  |
| Sleep quality score               | 1-5             |           2.7    |           2.68   |            -0.02  |
| Fatigue score                     | 1-5             |           2.67   |           2.66   |            -0.01  |

## Declared vs observed chronotype

`analytics_02_chronotype_validation.sql`

| chronotype_declared   | chronotype_derived   |   n_participants |   mid_sleep_median_min |   mid_sleep_median_mean |   mid_sleep_median_max |   split_threshold_hour | agrees   |
|:----------------------|:---------------------|-----------------:|-----------------------:|------------------------:|-----------------------:|-----------------------:|:---------|
| A                     | A                    |               13 |                  2.19  |                   2.406 |                  2.548 |                  2.555 | True     |
| A                     | B                    |               15 |                  2.557 |                   2.752 |                  3.207 |                  2.555 | False    |
| B                     | A                    |               12 |                  2.225 |                   2.397 |                  2.553 |                  2.555 | False    |
| B                     | B                    |               10 |                  2.59  |                   2.742 |                  2.911 |                  2.555 | True     |

## Weekly trend by chronotype cohort

`analytics_03_weekly_trend.sql`

|   study_week | chronotype   |   n_participants |   n_complete_days |   n_nights |   mid_sleep_hour_mean |   total_sleep_min_mean |   sleep_efficiency_pct_mean |   deep_sleep_pct_mean |   activity_midpoint_hour_mean |   steps_total_mean |   hr_resting_bpm_mean |   readiness_score_mean |
|-------------:|:-------------|-----------------:|------------------:|-----------:|----------------------:|-----------------------:|----------------------------:|----------------------:|------------------------------:|-------------------:|----------------------:|-----------------------:|
|            1 | A            |               28 |               109 |        111 |                 2.675 |                  424   |                       86.6  |                 17.74 |                        13.998 |             8885.7 |                 62.28 |                   4.92 |
|            1 | B            |               22 |                87 |         88 |                 2.357 |                  398.6 |                       87.33 |                 18.6  |                        14.005 |             9031.6 |                 62.67 |                   5.01 |
|            2 | A            |               28 |               195 |        194 |                 2.605 |                  415.3 |                       87.19 |                 17.97 |                        14.012 |             8864.6 |                 62.47 |                   4.99 |
|            2 | B            |               22 |               154 |        154 |                 2.468 |                  402.9 |                       87.17 |                 18.35 |                        13.998 |             9039.1 |                 62.81 |                   5.07 |
|            3 | A            |               28 |               196 |        194 |                 2.499 |                  405.7 |                       87.4  |                 17.49 |                        14.002 |             8891.3 |                 62.39 |                   4.85 |
|            3 | B            |               22 |               154 |        152 |                 2.496 |                  414.6 |                       87.13 |                 17.85 |                        14.009 |             9023.9 |                 62.73 |                   4.76 |
|            4 | A            |               28 |               195 |        196 |                 2.557 |                  417.5 |                       87.02 |                 17.7  |                        14.01  |             8902.2 |                 62.63 |                   4.77 |
|            4 | B            |               22 |               153 |        154 |                 2.553 |                  406.6 |                       86.88 |                 18.39 |                        13.999 |             9054.7 |                 62.73 |                   4.89 |
|            5 | A            |               28 |                84 |         84 |                 2.6   |                  413.1 |                       87.11 |                 17.23 |                        14.009 |             8867.7 |                 62.49 |                   4.82 |
|            5 | B            |               22 |                66 |         66 |                 2.719 |                  416.2 |                       87.32 |                 18.41 |                        14.004 |             9011.7 |                 62.88 |                   5.12 |

## Does the chronotype label separate the cohorts?

`analytics_04_cohort_effect_size.sql`, collapsed to one row per participant before testing - 1 393 nights are 50 participants measured 28 times, not 1 393 independent observations.

| metric                | defines_chronotype   |   morning_a_mean |   evening_b_mean |   difference_b_minus_a |   cohens_d |   welch_t |   p_approx | separates_cohorts   |
|:----------------------|:---------------------|-----------------:|-----------------:|-----------------------:|-----------:|----------:|-----------:|:--------------------|
| Mid-sleep time (h)    | True                 |            2.591 |            2.554 |                 -0.038 |     -0.165 |     -0.59 |     0.5547 | False               |
| Sleep onset time (h)  | True                 |           -0.87  |           -0.813 |                  0.057 |      0.341 |      1.25 |     0.2108 | False               |
| Activity midpoint (h) | True                 |           14.007 |           14.002 |                 -0.004 |     -0.249 |     -0.9  |     0.3659 | False               |
| Total sleep (min)     | False                |          414.47  |          407.527 |                 -6.943 |     -0.535 |     -1.88 |     0.0601 | False               |
| Sleep efficiency (%)  | False                |           87.108 |           87.127 |                  0.019 |      0.032 |      0.12 |     0.9083 | False               |
| Deep sleep (%)        | False                |           17.665 |           18.28  |                  0.615 |      0.493 |      1.63 |     0.1025 | False               |
| Daily steps           | False                |         8887.53  |         9032.21  |                144.681 |      0.053 |      0.18 |     0.8537 | False               |
| Resting HR (bpm)      | False                |           62.469 |           62.766 |                  0.297 |      0.056 |      0.2  |     0.843  | False               |

`cohens_d` is the standardised difference; |d| >= 0.5 is the conventional threshold for a difference large enough to matter. `p_approx` is a normal approximation to Welch's two-sided test.

## Reading

**The declared chronotype does not correspond to anything observable in this dataset.** A chronotype derived from observed mid-sleep time agrees with the declared label for 23/50 participants (46%) - indistinguishable from a coin flip - and none of mid-sleep time, sleep onset or activity midpoint separates the cohorts. Morning and evening types go to bed at the same time and move at the same hour of day, which is the whole of what chronotype means.

This is a finding about the data, not a pipeline defect: the same code recovers every injected defect in `eda/findings.md` at the documented row counts, and the sleep derivation reproduces the stage anchors exactly. `chronotype` in `participants.csv` is a label with no signal behind it.

The consequence for the deliverable is that the cohort comparison is reported as a null result. Any 'morning types sleep better' conclusion drawn from these tables would be noise, and a platform that presents one without this check is the actual failure mode. Worth raising with the study team: is the label mis-assigned at enrolment, or is it simply not encoded in the synthetic generator?
