-- Run against the UAT database ONLY (nurseconnect_uat), after the nurse has registered
-- and after:  python3 qualify_all_workers.py   (run from ~/nurseconnect-uat)
-- Replace NURSE_EMAIL. Puts the test nurse in Mumbai and online.
SELECT w.id, u.email, w.onboarding_status, w.availability, w.base_city
FROM worker_profiles w JOIN users u ON u.id = w.user_id
WHERE u.email = 'NURSE_EMAIL';

UPDATE worker_profiles
SET base_city = 'Mumbai', home_latitude = 19.0760, home_longitude = 72.8777, availability = 'online'
WHERE user_id = (SELECT id FROM users WHERE email = 'NURSE_EMAIL');
