-- Least-privilege grants for the web app's role. Run as the superuser after
-- schema.sql. :app_role is supplied by scripts/provision-db.sh (psql -v).
-- The app can write expenses, add categories, keep its OneDrive sign-in and
-- folders, and read the things it needs from TeslaMate (where there is one) — nothing
-- else in the database.
GRANT USAGE ON SCHEMA garagebooks TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.expense TO :"app_role";
GRANT SELECT, UPDATE ON garagebooks.setting TO :"app_role";
GRANT SELECT, INSERT ON garagebooks.category TO :"app_role";
GRANT UPDATE (upload_folder, budget_pool) ON garagebooks.category TO :"app_role";
-- Lease details and budgets per car.
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.lease, garagebooks.lease_budget TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.car_folder, garagebooks.onedrive_account TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.learned_label, garagebooks.learned_vendor, garagebooks.import_skip TO :"app_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA garagebooks TO :"app_role";
-- Its own car list and odometer readings; sync_teslamate_cars() runs as the app.
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.car, garagebooks.odometer_reading TO :"app_role";
GRANT EXECUTE ON FUNCTION garagebooks.sync_teslamate_cars() TO :"app_role";
-- Switching the ownership view between its two forms (with and without TeslaMate) and nothing else.
GRANT EXECUTE ON FUNCTION garagebooks.build_ownership(boolean) TO :"app_role";
-- What it reads from TeslaMate, when there is a TeslaMate in this database.
SELECT to_regclass('public.cars') IS NOT NULL AND to_regclass('public.positions') IS NOT NULL
       AND to_regclass('public.charging_processes') IS NOT NULL AS has_teslamate \gset
\if :has_teslamate
-- USAGE on public is not implied: the live TeslaMate DB has revoked it from PUBLIC.
GRANT USAGE ON SCHEMA public TO :"app_role";
GRANT SELECT ON public.cars TO :"app_role";
-- The newest migration says which TeslaMate this is; the app holds back the figures of an untested one.
GRANT SELECT ON public.schema_migrations TO :"app_role";
GRANT SELECT (car_id, date, odometer) ON public.positions TO :"app_role";
GRANT SELECT (car_id, start_date, cost) ON public.charging_processes TO :"app_role";
\endif
-- Reminders, and the view that adds the ones derived from a car's lease.
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.reminder TO :"app_role";
GRANT SELECT ON garagebooks.reminder_due TO :"app_role";
