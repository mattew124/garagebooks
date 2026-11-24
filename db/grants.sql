-- Least-privilege grants for the web app's role. Run as the superuser after
-- schema.sql. :app_role is supplied by scripts/provision-db.sh (psql -v).
-- The app can write expenses, add categories, keep its OneDrive sign-in and
-- folders, and read the things it needs from TeslaMate (where there is one) — nothing
-- else in the database.
GRANT USAGE ON SCHEMA garagebooks TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.expense TO :"app_role";
GRANT SELECT, UPDATE ON garagebooks.setting TO :"app_role";
GRANT SELECT, INSERT ON garagebooks.category TO :"app_role";
GRANT UPDATE (upload_folder, budget_pool, gst_free, has_period) ON garagebooks.category TO :"app_role";
-- Lease details and budgets per car.
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.lease, garagebooks.lease_budget TO :"app_role";
GRANT EXECUTE ON FUNCTION garagebooks.fbt_year(date) TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.car_folder, garagebooks.onedrive_account TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.learned_label, garagebooks.learned_vendor, garagebooks.import_skip TO :"app_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA garagebooks TO :"app_role";
-- Its own car list and odometer readings; sync_teslamate_cars() runs as the app.
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.car, garagebooks.odometer_reading TO :"app_role";
GRANT EXECUTE ON FUNCTION garagebooks.sync_teslamate_cars() TO :"app_role";
-- Switching the ownership view between its two forms (with and without TeslaMate) and nothing else.
GRANT EXECUTE ON FUNCTION garagebooks.build_ownership(boolean) TO :"app_role";
-- The same for the odometer function the kilometre pace panel uses.
GRANT EXECUTE ON FUNCTION garagebooks.build_odometer_at(boolean) TO :"app_role";
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
-- The change history: the app adds to it (through the triggers) and reads it, never rewrites it.
GRANT SELECT, INSERT ON garagebooks.change_log TO :"app_role";
-- Cost after tax: the ATO's home charging rates (the app adds a year and changes a rate), and the one
-- definition of an expense's cost after tax that the app and the dashboard share.
GRANT SELECT, INSERT, UPDATE ON garagebooks.ev_charging_rate TO :"app_role";
GRANT EXECUTE ON FUNCTION garagebooks.expense_after_tax(text) TO :"app_role";
-- The tax rate by income year: the ATO's brackets (the ato_rates job replaces a year's), each person's choice of
-- bracket and Medicare status, and the functions that turn them into a marginal rate.
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.tax_bracket, garagebooks.tax_choice TO :"app_role";
GRANT EXECUTE ON FUNCTION garagebooks.income_year(date), garagebooks.tax_table_year(integer),
    garagebooks.tax_position(text, integer), garagebooks.marginal_rate(text, date) TO :"app_role";
-- Scheduled jobs: the scheduler adds a row for each job and records each run.
GRANT SELECT, INSERT, UPDATE ON garagebooks.job TO :"app_role";
-- Notifications: what has been sent is added, read and pruned, never rewritten. (The settings and the
-- OneDrive sign-in columns are covered by the grants on their tables above.)
GRANT SELECT, INSERT, DELETE ON garagebooks.notification TO :"app_role";
-- (The GST and payment reference columns of an expense are covered by the grant on the table.)
-- The other PDFs merged into an expense on the import page.
GRANT SELECT, INSERT, DELETE ON garagebooks.expense_document TO :"app_role";
-- The items a service covered, and which expense had which.
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.service_item, garagebooks.expense_service_item TO :"app_role";
GRANT SELECT ON garagebooks.expense_service TO :"app_role";
-- Claims sent to the salary packaging provider's connector, one per expense.
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.expense_claim TO :"app_role";
-- The provider's transaction history, cached from the claims connector.
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.provider_transaction TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.provider_claim TO :"app_role";
GRANT SELECT, INSERT, UPDATE ON garagebooks.provider_sync TO :"app_role";
-- Invoices by email: each car's mailbox folder, and which emails became which expense.
GRANT SELECT, INSERT, UPDATE, DELETE ON garagebooks.car_mail_folder, garagebooks.mail_import TO :"app_role";
