-- gold_participant_devices
-- One row per participant, linking participant_id -> device_id (required by
-- the brief) plus the device context an analyst needs to stratify by hardware.
-- The roster is the spine: a participant with no device_metadata row still
-- appears, with nulls, rather than disappearing from a join.
SELECT
    p.participant_id,
    p.device_id,
    d.device_model,
    d.wear_site,
    d.calibration_date,
    d.firmware_version,
    d.firmware_source,           -- 'device_label' where it was regex-recovered
    p.age,
    p.gender,
    p.chronotype AS chronotype_declared,
    p.max_heart_rate
FROM participants p
LEFT JOIN devices d
       ON d.participant_id = p.participant_id
      AND d.device_id      = p.device_id
ORDER BY p.participant_id
