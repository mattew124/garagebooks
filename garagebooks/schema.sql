-- GarageBooks schema. Idempotent: safe to re-run (scripts/provision-db.sh
-- does so on every deploy). Lives in its own `garagebooks` schema. Beside TeslaMate
-- that is TeslaMate's database, so Grafana needs no second datasource; with no
-- TeslaMate it is any PostgreSQL database. Nothing here needs TeslaMate's tables
-- (cars, positions, charging_processes) to exist: where they do, they are read,
-- never changed and never pointed at by a foreign key.
--
-- Run as the database superuser/owner, NOT as the app role: it creates the
-- schema and the reporting view.

-- Releases up to 0.4.0-rc1 called the schema `expenses`. Rename it, with everything in it, the
-- first time this release starts. Only a schema that is ours (it has our `setting` table), and
-- only while there is no `garagebooks` schema yet: another app's `expenses` is left alone.
DO $$
BEGIN
    IF to_regclass('expenses.setting') IS NOT NULL AND to_regnamespace('garagebooks') IS NULL THEN
        ALTER SCHEMA expenses RENAME TO garagebooks;
    END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS garagebooks;

-- kind: 'purchase' = capital cost (what you paid for the car),
--       'running'  = everything spent keeping it on the road.
CREATE TABLE IF NOT EXISTS garagebooks.category (
    id        smallserial PRIMARY KEY,
    name      text NOT NULL UNIQUE CHECK (btrim(name) <> ''),
    kind      text NOT NULL DEFAULT 'running' CHECK (kind IN ('purchase', 'running', 'energy')),
    sort_order integer NOT NULL DEFAULT 100
);
-- kind 'energy' = charging and fuel entered by hand: what TeslaMate's charging costs are for a
-- car it tracks. Databases made before it existed get the wider check.
ALTER TABLE garagebooks.category DROP CONSTRAINT IF EXISTS category_kind_check;
ALTER TABLE garagebooks.category ADD CONSTRAINT category_kind_check
    CHECK (kind IN ('purchase', 'running', 'energy'));

INSERT INTO garagebooks.category (name, kind, sort_order) VALUES
    ('Initial purchase',  'purchase',  10),
    ('Insurance',         'running',   20),
    ('Registration',      'running',   30),
    ('Tyres',             'running',   40),
    ('Charging',          'energy',    90),
    ('Fuel',              'energy',    95)
ON CONFLICT (name) DO NOTHING;

-- The cars. GarageBooks keeps its own list, so a car needs no TeslaMate: one added by hand has
-- teslamate_car_id NULL. A car TeslaMate tracks is added by sync_teslamate_cars() below, with
-- TeslaMate's own id where that is free (so nothing that was saved against it moves), and
-- follows TeslaMate's name until it is renamed here (name_from_teslamate). The link is not a
-- foreign key: TeslaMate may be absent, and may delete its car without asking us.
--   kind: BEV (battery electric), ICE (petrol or diesel), PHEV (plug-in hybrid), HEV (hybrid)
-- Cars added by hand are numbered from 1000, clear of TeslaMate's own small ids.
CREATE SEQUENCE IF NOT EXISTS garagebooks.car_id_seq AS smallint START 1000 MINVALUE 1000;
CREATE TABLE IF NOT EXISTS garagebooks.car (
    id                  smallint PRIMARY KEY DEFAULT nextval('garagebooks.car_id_seq'),
    name                text NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 100),
    kind                text NOT NULL DEFAULT 'BEV' CHECK (kind IN ('BEV', 'ICE', 'PHEV', 'HEV')),
    make                text CHECK (make IS NULL OR (btrim(make) <> '' AND length(make) <= 60)),
    model               text CHECK (model IS NULL OR (btrim(model) <> '' AND length(model) <= 60)),
    teslamate_car_id    smallint UNIQUE,
    name_from_teslamate boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- Selling the car: when (a moment, entered as a date in APP_TZ) and for how much. Both or neither.
-- Added to databases made before it existed; safe to run again.
ALTER TABLE garagebooks.car ADD COLUMN IF NOT EXISTS sold_at timestamptz;
ALTER TABLE garagebooks.car ADD COLUMN IF NOT EXISTS sale_price numeric(12,2) CHECK (sale_price >= 0);
ALTER TABLE garagebooks.car DROP CONSTRAINT IF EXISTS car_sale_both;
ALTER TABLE garagebooks.car ADD CONSTRAINT car_sale_both CHECK ((sold_at IS NULL) = (sale_price IS NULL));

-- Adds a row for each TeslaMate car that has none, and keeps the names of cars not renamed here
-- in step. Returns how many it added. Does nothing when there is no TeslaMate. Run on every
-- start and by the app from time to time, as whoever calls it (the app's role may read cars).
CREATE OR REPLACE FUNCTION garagebooks.sync_teslamate_cars() RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    tm    record;
    added integer := 0;
BEGIN
    IF to_regclass('public.cars') IS NULL THEN
        RETURN 0;
    END IF;
    FOR tm IN
        SELECT c.id, left(COALESCE(NULLIF(btrim(c.name), ''), NULLIF(right(c.vin, 6), ''), 'Car ' || c.id), 100) AS name,
               NULLIF(left(btrim(c.model), 60), '') AS model
        FROM public.cars c
        WHERE NOT EXISTS (SELECT 1 FROM garagebooks.car g WHERE g.teslamate_car_id = c.id)
        ORDER BY c.id
    LOOP
        INSERT INTO garagebooks.car (id, name, kind, make, model, teslamate_car_id, name_from_teslamate)
        VALUES (CASE WHEN EXISTS (SELECT 1 FROM garagebooks.car g WHERE g.id = tm.id)
                     THEN nextval('garagebooks.car_id_seq') ELSE tm.id END,
                tm.name, 'BEV', 'Tesla', tm.model, tm.id, true);
        added := added + 1;
    END LOOP;
    UPDATE garagebooks.car g
       SET name = left(btrim(c.name), 100)
      FROM public.cars c
     WHERE g.teslamate_car_id = c.id AND g.name_from_teslamate
       AND NULLIF(btrim(c.name), '') IS NOT NULL AND g.name <> left(btrim(c.name), 100);
    RETURN added;
END
$fn$;
SELECT garagebooks.sync_teslamate_cars();

-- Odometer readings entered by hand: the odometer for a car TeslaMate doesn't track, and for
-- any car while TeslaMate can't be read. An expense's own odometer counts as a reading too.
CREATE TABLE IF NOT EXISTS garagebooks.odometer_reading (
    id       bigserial PRIMARY KEY,
    car_id   smallint NOT NULL REFERENCES garagebooks.car (id) ON DELETE CASCADE,
    read_at  timestamptz NOT NULL,
    km       numeric(9, 1) NOT NULL CHECK (km >= 0),
    UNIQUE (car_id, read_at)
);

-- Novated leases (roadmap L1): what the provider's statement says about the lease behind a car.
-- A car can have several over time (a refinance starts a new one from its date); the app keeps
-- their date ranges from overlapping. Dates are plain dates. Money is dollars. Nothing personal is
-- stored here (no names, employer, payroll or bank numbers). The statutory percentage is data,
-- never a constant in code that calculates.
CREATE TABLE IF NOT EXISTS garagebooks.lease (
    id                      serial PRIMARY KEY,
    car_id                  smallint NOT NULL REFERENCES garagebooks.car (id) ON DELETE CASCADE,
    package_name            text CHECK (package_name IS NULL OR (btrim(package_name) <> '' AND length(package_name) <= 100)),
    financier               text NOT NULL CHECK (btrim(financier) <> '' AND length(financier) <= 100),
    start_date              date NOT NULL,
    end_date                date NOT NULL,
    refinance_date          date,
    term_months             smallint NOT NULL CHECK (term_months BETWEEN 1 AND 120),
    pay_frequency           text NOT NULL CHECK (pay_frequency IN ('weekly', 'fortnightly', 'monthly')),
    pretax_payment          numeric(12, 2) NOT NULL DEFAULT 0 CHECK (pretax_payment >= 0),
    posttax_payment         numeric(12, 2) NOT NULL DEFAULT 0 CHECK (posttax_payment >= 0),
    admin_fee               numeric(12, 2) NOT NULL DEFAULT 0 CHECK (admin_fee >= 0),
    car_cost                numeric(12, 2) CHECK (car_cost IS NULL OR car_cost >= 0),
    residual_amount         numeric(12, 2) CHECK (residual_amount IS NULL OR residual_amount >= 0),
    residual_percent        numeric(6, 3) CHECK (residual_percent IS NULL OR residual_percent BETWEEN 0 AND 100),
    fbt_base_value          numeric(12, 2) CHECK (fbt_base_value IS NULL OR fbt_base_value >= 0),
    fbt_method              text NOT NULL DEFAULT 'statutory' CHECK (fbt_method IN ('statutory', 'operating_cost')),
    statutory_percent       numeric(5, 2) NOT NULL DEFAULT 20 CHECK (statutory_percent BETWEEN 0 AND 100),
    ev_exempt               boolean NOT NULL DEFAULT false,
    tax_treatment           text CHECK (tax_treatment IS NULL OR tax_treatment IN ('employee_contribution', 'other')),
    tax_treatment_note      text CHECK (tax_treatment_note IS NULL OR (btrim(tax_treatment_note) <> '' AND length(tax_treatment_note) <= 100)),
    estimated_km_per_year   integer CHECK (estimated_km_per_year IS NULL OR estimated_km_per_year BETWEEN 0 AND 1000000),
    rego_expiry             date,
    rego_renewed_by_provider boolean NOT NULL DEFAULT false,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CHECK (end_date > start_date),
    CHECK (refinance_date IS NULL OR (refinance_date >= start_date AND refinance_date <= end_date))
);
CREATE INDEX IF NOT EXISTS lease_car_idx ON garagebooks.lease (car_id, start_date);

-- The provider's yearly budget for each running cost (a "pool"). Each row holds from its date until
-- a later row for the same pool: a re-budget adds a row, so earlier periods are never rewritten.
-- Entering a lease adds the first set, effective from the lease's start date.
--   pool: lease, fuel_charging (fuel or charging), insurance, registration (and CTP), maintenance,
--         tyres, other (other running costs)
CREATE TABLE IF NOT EXISTS garagebooks.lease_budget (
    lease_id       integer NOT NULL REFERENCES garagebooks.lease (id) ON DELETE CASCADE,
    pool           text NOT NULL CHECK (pool IN ('lease', 'fuel_charging', 'insurance', 'registration',
                                                 'maintenance', 'tyres', 'other')),
    yearly_amount  numeric(12, 2) NOT NULL CHECK (yearly_amount >= 0),
    effective_from date NOT NULL,
    PRIMARY KEY (lease_id, pool, effective_from)
);

-- The FBT year (1 April to 31 March) a date falls in, as the year it starts in: 2025 is "FBT 2025-26".
-- The app, the dashboard and any query of your own share this one definition; pass the date in the
-- time zone you mean (occurred_at AT TIME ZONE 'Australia/Brisbane')::date. Roadmap L2.
CREATE OR REPLACE FUNCTION garagebooks.fbt_year(d date) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT CASE WHEN EXTRACT(month FROM d) >= 4 THEN EXTRACT(year FROM d)::integer
                  ELSE EXTRACT(year FROM d)::integer - 1 END $$;

-- The pool each category's expenses are paid from, or NULL for none. Added once, with defaults for
-- the standard categories; after that the owner's edits (on the Categories page) are never overwritten.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'garagebooks' AND table_name = 'category'
                     AND column_name = 'budget_pool') THEN
        ALTER TABLE garagebooks.category ADD COLUMN budget_pool text
            CHECK (budget_pool IS NULL OR budget_pool IN ('lease', 'fuel_charging', 'insurance', 'registration',
                                                          'maintenance', 'tyres', 'other'));
        UPDATE garagebooks.category SET budget_pool = 'insurance' WHERE name = 'Insurance';
        UPDATE garagebooks.category SET budget_pool = 'registration' WHERE name = 'Registration';
        UPDATE garagebooks.category SET budget_pool = 'tyres' WHERE name IN ('Tyres', 'Wheel alignment', 'Tyre rotation');
        UPDATE garagebooks.category SET budget_pool = 'maintenance' WHERE name IN ('Cabin air filter', 'Air filter');
        UPDATE garagebooks.category SET budget_pool = 'fuel_charging' WHERE name IN ('Charging', 'Fuel');
    END IF;
END $$;

-- Subfolder of the car's OneDrive folder that this category's invoices are uploaded
-- into (created on demand). NULL = a subfolder named after the category. The column
-- is added once, with sensible defaults for the standard categories; after that the
-- owner's edits (on the Categories page) are never overwritten.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'garagebooks' AND table_name = 'category'
                     AND column_name = 'upload_folder') THEN
        ALTER TABLE garagebooks.category ADD COLUMN upload_folder text
            CHECK (upload_folder IS NULL OR (btrim(upload_folder) <> '' AND length(upload_folder) <= 255));
        UPDATE garagebooks.category SET upload_folder = 'Maintenance'
            WHERE name IN ('Tyres', 'Cabin air filter', 'Air filter', 'Wheel alignment', 'Tyre rotation');
        UPDATE garagebooks.category SET upload_folder = name WHERE name IN ('Insurance', 'Registration');
    END IF;
END $$;

-- What the invoice importer has learned from the owner's corrections. Labels only (the
-- words printed beside a value, e.g. 'annual premium'), never amounts or document text.
-- A label is trusted once it has been confirmed `hits` >= 2 times.
CREATE TABLE IF NOT EXISTS garagebooks.learned_label (
    field text NOT NULL CHECK (field IN ('amount', 'date', 'odometer_km')),
    label text NOT NULL CHECK (length(label) BETWEEN 3 AND 40),
    hits  integer NOT NULL DEFAULT 1,
    PRIMARY KEY (field, label)
);
-- Vendor (lower case) -> the category the owner gave its invoices.
CREATE TABLE IF NOT EXISTS garagebooks.learned_vendor (
    vendor      text NOT NULL CHECK (length(vendor) BETWEEN 1 AND 100),
    category_id smallint NOT NULL REFERENCES garagebooks.category (id) ON DELETE CASCADE,
    hits        integer NOT NULL DEFAULT 1,
    PRIMARY KEY (vendor, category_id)
);

-- PDFs in a car's folder the owner marked "skip" on the import page, so they stop being
-- offered. Keyed by the file's link (or its path, in the folder on the server), the same thing
-- an imported expense stores.
CREATE TABLE IF NOT EXISTS garagebooks.import_skip (
    car_id smallint NOT NULL REFERENCES garagebooks.car (id) ON DELETE CASCADE,
    url    text NOT NULL CHECK (url <> ''),
    PRIMARY KEY (car_id, url)
);

-- Settings, saved from the app's Settings page (the same idea as TeslaMate's own).
-- A single row. Both are optional, and http(s) only: they are rendered as links.
--   app_url  where this app is published; the Grafana dashboard links back to it
--   grafana_url   the Grafana dashboard for this app; the app's header links to it
CREATE TABLE IF NOT EXISTS garagebooks.setting (
    id            smallint PRIMARY KEY CHECK (id = 1),
    app_url  text CHECK (app_url IS NULL OR app_url ~* '^https?://[^[:space:]]+$'),
    grafana_url   text CHECK (grafana_url IS NULL OR grafana_url ~* '^https?://[^[:space:]]+$')
);
-- Added after the table first shipped, so existing databases get it too.
--   invoice_date_format  the date in uploaded invoice names (yyyy, yy, mm, dd); NULL = yymmdd
ALTER TABLE garagebooks.setting ADD COLUMN IF NOT EXISTS invoice_date_format text
    CHECK (invoice_date_format IS NULL OR length(invoice_date_format) <= 20);
--   invoice_name_format  the file name template (%date %category %vendor %car %amount); NULL = the original style
ALTER TABLE garagebooks.setting ADD COLUMN IF NOT EXISTS invoice_name_format text
    CHECK (invoice_name_format IS NULL OR length(invoice_name_format) <= 100);
--   multi_amount         which figure to take when an invoice offers several (6 or 12 months): max, min or blank; NULL = max
ALTER TABLE garagebooks.setting ADD COLUMN IF NOT EXISTS multi_amount text
    CHECK (multi_amount IS NULL OR multi_amount IN ('max', 'min', 'blank'));
INSERT INTO garagebooks.setting (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
-- Up to 0.4.0-rc1: app_url was called expenses_url, and the dashboard's uid was tm-expenses-tco,
-- which a saved dashboard link still names. Both follow the rename, once.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'garagebooks' AND table_name = 'setting'
                 AND column_name = 'expenses_url') THEN
        ALTER TABLE garagebooks.setting RENAME COLUMN expenses_url TO app_url;
        ALTER TABLE garagebooks.setting RENAME CONSTRAINT setting_expenses_url_check TO setting_app_url_check;
    END IF;
END $$;
UPDATE garagebooks.setting SET grafana_url = replace(grafana_url, '/d/tm-expenses-tco', '/d/garagebooks-tco')
    WHERE grafana_url LIKE '%/d/tm-expenses-tco%';

CREATE TABLE IF NOT EXISTS garagebooks.expense (
    id           bigserial PRIMARY KEY,
    car_id       smallint NOT NULL REFERENCES garagebooks.car (id),
    category_id  smallint NOT NULL REFERENCES garagebooks.category (id),
    -- Moment the cost was incurred (timestamptz; the UI enters it in APP_TZ).
    occurred_at  timestamptz NOT NULL,
    -- Negative for a rebate or refund (it reduces the totals).
    amount       numeric(12, 2) NOT NULL,
    -- Odometer in km at occurred_at. Optional: not every cost has a reading to go with it.
    odometer_km  numeric(9, 1) CHECK (odometer_km IS NULL OR odometer_km >= 0),
    vendor       text,
    notes        text,
    -- Link to the PDF invoice (a cloud-storage share link). https only: it is rendered
    -- as an <a href>, so never allow javascript:/data: schemes in here.
    invoice_url  text CHECK (invoice_url IS NULL OR invoice_url ~* '^https://[^[:space:]]+$'),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- An invoice kept in the folder on the server (INVOICES_DIR) rather than behind a link: its path
-- below that folder, always starting with '/', so remounting the folder elsewhere breaks nothing.
-- The app serves it at /invoice/<id>.
ALTER TABLE garagebooks.expense ADD COLUMN IF NOT EXISTS invoice_path text
    CHECK (invoice_path IS NULL OR (invoice_path LIKE '/_%' AND length(invoice_path) <= 1024));

CREATE INDEX IF NOT EXISTS expense_car_time_idx ON garagebooks.expense (car_id, occurred_at);
CREATE INDEX IF NOT EXISTS expense_category_idx ON garagebooks.expense (category_id);

-- The folder each car's invoice PDFs go to, chosen on the app's OneDrive or Invoice
-- folder page. OneDrive uploads go by folder_id, so renaming or moving the folder in OneDrive
-- doesn't break them; folder_path is only for showing where that is.
CREATE TABLE IF NOT EXISTS garagebooks.car_folder (
    car_id      smallint PRIMARY KEY REFERENCES garagebooks.car (id) ON DELETE CASCADE,
    folder_id   text NOT NULL CHECK (folder_id <> ''),
    folder_path text NOT NULL
);
-- Where that folder is: OneDrive (the only choice when the table first shipped) or the folder on
-- the server, in which case folder_id is its path below INVOICES_DIR. One or the other per car.
ALTER TABLE garagebooks.car_folder ADD COLUMN IF NOT EXISTS backend text NOT NULL DEFAULT 'onedrive'
    CHECK (backend IN ('onedrive', 'local'));

-- The OneDrive connection: at most one row. client_id/client_secret are the
-- installer's own Entra app registration, entered on the OneDrive page.
-- client_secret, refresh_token and auth_state are encrypted by the app with
-- GARAGEBOOKS_ENCRYPTION_KEY, so a database dump or backup on its own can't open the
-- drive. auth_state holds the state and PKCE verifier of a sign-in in progress.
CREATE TABLE IF NOT EXISTS garagebooks.onedrive_account (
    id              boolean PRIMARY KEY DEFAULT true CHECK (id),
    client_id       text,
    client_secret   text,
    secret_expires  date,
    account         text,
    refresh_token   text,
    auth_state      text,
    auth_expires_at timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Up to 0.4.0 these three tables pointed at TeslaMate's public.cars. Point them at our own
-- car table instead (every TeslaMate car has a row there with the same id by now, so nothing
-- moves), which also stops our foreign key standing in the way of TeslaMate deleting a car.
DO $$
DECLARE
    fk record;
BEGIN
    FOR fk IN
        SELECT con.conname, con.conrelid::regclass AS tbl, con.confdeltype
        FROM pg_constraint con
        JOIN pg_class t   ON t.oid = con.conrelid    JOIN pg_namespace tn ON tn.oid = t.relnamespace
        JOIN pg_class ref ON ref.oid = con.confrelid JOIN pg_namespace rn ON rn.oid = ref.relnamespace
        WHERE con.contype = 'f' AND tn.nspname = 'garagebooks' AND rn.nspname = 'public' AND ref.relname = 'cars'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', fk.tbl, fk.conname);
        EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I FOREIGN KEY (car_id) REFERENCES garagebooks.car (id)%s',
                       fk.tbl, fk.conname, CASE fk.confdeltype WHEN 'c' THEN ' ON DELETE CASCADE' ELSE '' END);
    END LOOP;
END $$;

-- One row per car: everything the TCO dashboard's headline panels need, so the
-- definitions (what counts as "start odometer", what is charging) live in one
-- place instead of being copy-pasted across panels.
--   charging_total            charging and fuel: TeslaMate's charging costs plus 'energy' expenses
--   teslamate_charging_total  TeslaMate's part of that alone (0 for a car it doesn't track)
--   start_km    odometer when you bought it (purchase expense), else the lowest odometer known
--   current_km  the highest odometer known: TeslaMate's, a reading entered by hand, an expense's
--   owned_since purchase date, else the earliest position, reading or expense
--   sale_price, sold_at   what the car sold for and when (NULL while it is owned); tco is net of the sale price
--   owned_until the sale date (never before owned_since), NULL while owned: time owned and the per-day
--               and per-month figures stop there. current_km for a sold car is the highest odometer
--               known at or before the sale.
--
-- The view comes in two forms, built by build_ownership(): with TeslaMate's charging costs
-- and positions, or without them (there is no TeslaMate, or the app is holding its figures
-- back: it can't be read, or is a version the app wasn't tested with). Without them, a car
-- that TeslaMate tracks has NULL where the answer needs TeslaMate (charging_total, tco),
-- never a wrong zero; a car entered by hand is complete either way. The columns and their
-- types are the same in both forms. Returns whether TeslaMate's tables are in the view.
--
-- SECURITY DEFINER, so the app's least-privilege role can switch forms as TeslaMate comes and
-- goes; its only input is the boolean.
CREATE OR REPLACE FUNCTION garagebooks.build_ownership(use_teslamate boolean DEFAULT true) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    tm       boolean := COALESCE(use_teslamate, false)
                        AND to_regclass('public.charging_processes') IS NOT NULL
                        AND to_regclass('public.positions') IS NOT NULL;
    charging text;
    odometer text;
    body     text;
BEGIN
  LOOP
    charging := 'CROSS JOIN (SELECT NULL::numeric AS charging_total) ch';
    odometer := 'CROSS JOIN (SELECT NULL::double precision AS first_km, NULL::double precision AS last_km,
                                    NULL::timestamptz AS first_at) pos';
    IF tm THEN
        charging := 'LEFT JOIN LATERAL (
            SELECT SUM(cp.cost)::numeric AS charging_total
            FROM public.charging_processes cp
            WHERE cp.car_id = car.teslamate_car_id
        ) ch ON true';
        odometer := 'LEFT JOIN LATERAL (
            SELECT MIN(p.odometer)::double precision AS first_km,
                   MAX(p.odometer) FILTER (WHERE car.sold_at IS NULL OR p.date <= car.sold_at AT TIME ZONE ''UTC'')::double precision AS last_km,
                   MIN(p.date) AT TIME ZONE ''UTC''  AS first_at
            FROM public.positions p
            WHERE p.car_id = car.teslamate_car_id
        ) pos ON true';
    END IF;
    body := format($view$
SELECT
    v.car_id, v.car_name, v.purchase_total, v.running_total, v.charging_total,
    v.purchase_total + v.running_total + v.charging_total - COALESCE(v.sale_price, 0) AS tco,
    v.start_km, v.current_km,
    -- No odometer known => no distance, not 0.
    CASE WHEN v.current_km IS NULL THEN NULL
         ELSE GREATEST(v.current_km - v.start_km, 0)
    END AS distance_km,
    v.owned_since, v.teslamate_charging_total,
    v.sale_price, v.sold_at,
    CASE WHEN v.sold_at IS NULL THEN NULL ELSE GREATEST(v.sold_at, v.owned_since) END AS owned_until
FROM (
    SELECT
        car.id                                         AS car_id,
        car.name::text                                 AS car_name,
        COALESCE(ex.purchase_total, 0)::numeric        AS purchase_total,
        COALESCE(ex.running_total, 0)::numeric         AS running_total,
        (tmc.total + COALESCE(ex.energy_total, 0))::numeric AS charging_total,
        COALESCE(ex.purchase_km::double precision, LEAST(pos.first_km, man.first_km)) AS start_km,
        GREATEST(pos.last_km, man.last_km)             AS current_km,
        COALESCE(ex.purchase_at, LEAST(pos.first_at, man.first_at, ex.first_at)) AS owned_since,
        tmc.total                                      AS teslamate_charging_total,
        car.sale_price, car.sold_at
    FROM garagebooks.car car
    LEFT JOIN LATERAL (
        SELECT
            SUM(e.amount) FILTER (WHERE c.kind = 'purchase') AS purchase_total,
            SUM(e.amount) FILTER (WHERE c.kind = 'running')  AS running_total,
            SUM(e.amount) FILTER (WHERE c.kind = 'energy')   AS energy_total,
            (ARRAY_AGG(e.odometer_km ORDER BY e.occurred_at)
                FILTER (WHERE c.kind = 'purchase' AND e.odometer_km IS NOT NULL))[1] AS purchase_km,
            MIN(e.occurred_at) FILTER (WHERE c.kind = 'purchase') AS purchase_at,
            MIN(e.occurred_at) AS first_at
        FROM garagebooks.expense e
        JOIN garagebooks.category c ON c.id = e.category_id
        WHERE e.car_id = car.id
    ) ex ON true
    LEFT JOIN LATERAL (
        SELECT MIN(r.km)::double precision AS first_km, MAX(r.km) FILTER (WHERE car.sold_at IS NULL OR r.at <= car.sold_at)::double precision AS last_km,
               MIN(r.at) AS first_at
        FROM (
            SELECT o.km, o.read_at AS at FROM garagebooks.odometer_reading o WHERE o.car_id = car.id
            UNION ALL
            SELECT e.odometer_km, e.occurred_at FROM garagebooks.expense e
            WHERE e.car_id = car.id AND e.odometer_km IS NOT NULL
        ) r
    ) man ON true
    %s
    %s
    -- TeslaMate's charging for this car: its total, 0 for a car it doesn't track, and NULL
    -- (unknown) for a car it tracks while its tables are not in this view.
    CROSS JOIN LATERAL (
        SELECT CASE WHEN car.teslamate_car_id IS NULL THEN 0::numeric
                    WHEN %L THEN COALESCE(ch.charging_total, 0)
               END AS total
    ) tmc
) v$view$, charging, odometer, tm);
    BEGIN
        BEGIN
            EXECUTE 'CREATE OR REPLACE VIEW garagebooks.ownership AS' || body;
        EXCEPTION WHEN invalid_table_definition THEN
            -- A view from an older release, whose columns took TeslaMate's types: make it afresh.
            EXECUTE 'DROP VIEW garagebooks.ownership';
            EXECUTE 'CREATE VIEW garagebooks.ownership AS' || body;
        END;
        RETURN tm;
    EXCEPTION WHEN OTHERS THEN
        -- TeslaMate's tables are not as expected (a column renamed, say): build the form without
        -- them, so a TeslaMate that changed never stops GarageBooks starting.
        IF NOT tm THEN
            RAISE;
        END IF;
        RAISE WARNING 'garagebooks.ownership is built without TeslaMate: %', SQLERRM;
        tm := false;
    END;
  END LOOP;
END
$fn$;
REVOKE ALL ON FUNCTION garagebooks.build_ownership(boolean) FROM PUBLIC;
SELECT garagebooks.build_ownership(true);

-- Rebates and refunds are negative amounts. Databases made before that allowed them still carry
-- the old CHECK (amount >= 0); drop it (a no-op once gone).
ALTER TABLE garagebooks.expense DROP CONSTRAINT IF EXISTS expense_amount_check;

-- Reminders (roadmap item 4): service and paperwork that fall due on a date, an odometer reading
-- or whichever comes first. What is due is worked out when a page or the dashboard is viewed (the
-- app's reminders.status() and the dashboard's Reminders panel share the lead times below), never
-- stored. A resolved reminder is kept, with the expense that did the job; a recurring one makes
-- the next when it is resolved, and previous_id links the chain back. edited_at is set when the owner
-- changes a reminder, so un-resolving can tell an automatic next reminder from one they touched.
CREATE TABLE IF NOT EXISTS garagebooks.reminder (
    id                  serial PRIMARY KEY,
    car_id              smallint NOT NULL REFERENCES garagebooks.car (id) ON DELETE CASCADE,
    title               text NOT NULL CHECK (btrim(title) <> '' AND length(title) <= 100),
    category_id         smallint REFERENCES garagebooks.category (id) ON DELETE SET NULL,
    due_date            date,
    due_km              numeric(9, 1) CHECK (due_km IS NULL OR due_km >= 0),
    lead_days           integer NOT NULL DEFAULT 30 CHECK (lead_days BETWEEN 0 AND 3650),
    lead_km             numeric(9, 1) NOT NULL DEFAULT 1000 CHECK (lead_km >= 0),
    every_months        integer CHECK (every_months IS NULL OR every_months BETWEEN 1 AND 600),
    every_km            numeric(9, 1) CHECK (every_km IS NULL OR every_km > 0),
    notes               text CHECK (notes IS NULL OR length(notes) <= 2000),
    resolved_at         timestamptz,
    resolved_expense_id bigint REFERENCES garagebooks.expense (id) ON DELETE SET NULL,
    resolved_km         numeric(9, 1),
    previous_id         integer REFERENCES garagebooks.reminder (id) ON DELETE SET NULL,
    edited_at           timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (due_date IS NOT NULL OR due_km IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS reminder_car_idx ON garagebooks.reminder (car_id) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS reminder_previous_idx ON garagebooks.reminder (previous_id);

-- (service_item_id is NULL here and filled in by the redefinition at the end of this file, once service_item exists;
-- next_lease_start is NULL here and in the next one, and filled in by the last.)
-- Everything that is due, in one place for the app and Grafana: the open reminders, plus two derived
-- from a car's current lease, so they follow the lease and can't drift from it (the lease that
-- covers today, else the next to start): registration expiry, which is informational when the provider renews it,
-- and the lease's end, with a longer lead time. reminder_id is NULL for a derived one.
-- (Dates use the database's own day; the app works out "today" in APP_TZ for its pages.)
CREATE OR REPLACE VIEW garagebooks.reminder_due AS
SELECT 'reminder'::text AS kind, r.id AS reminder_id, r.car_id, r.title, r.category_id, r.due_date,
       r.due_km, r.lead_days, r.lead_km, false AS informational, NULL::smallint AS service_item_id,
       NULL::date AS next_lease_start
FROM garagebooks.reminder r
WHERE r.resolved_at IS NULL
UNION ALL
SELECT 'registration', NULL::integer, cl.car_id, 'Registration expires', NULL::smallint, cl.rego_expiry,
       NULL::numeric(9, 1), 30, 0::numeric(9, 1), cl.rego_renewed_by_provider, NULL::smallint, NULL::date
FROM garagebooks.lease cl
WHERE cl.rego_expiry IS NOT NULL
  AND cl.id = (SELECT l.id FROM garagebooks.lease l WHERE l.car_id = cl.car_id AND l.end_date >= CURRENT_DATE
               ORDER BY l.start_date LIMIT 1)
UNION ALL
SELECT 'lease_end', NULL::integer, cl.car_id, 'Lease ends', NULL::smallint, cl.end_date,
       NULL::numeric(9, 1), 90, 0::numeric(9, 1), false, NULL::smallint, NULL::date
FROM garagebooks.lease cl
WHERE cl.id = (SELECT l.id FROM garagebooks.lease l WHERE l.car_id = cl.car_id AND l.end_date >= CURRENT_DATE
               ORDER BY l.start_date LIMIT 1);
-- Change history: an append-only log of every add, edit and delete of an expense or a category,
-- written by triggers, so changes made by other tools straight in the database are caught too.
--   actor      who, from the identity header the sign-in gate sets (AUTH_USER_HEADER); NULL = unknown
--   row_id     the changed row's id; before_row / after_row are the whole row as jsonb
-- Only the history before this release is missing: nothing is invented for it.
CREATE TABLE IF NOT EXISTS garagebooks.change_log (
    id         bigserial PRIMARY KEY,
    changed_at timestamptz NOT NULL DEFAULT now(),
    actor      text,
    table_name text NOT NULL CHECK (table_name IN ('expense', 'category')),
    row_id     bigint NOT NULL,
    operation  text NOT NULL CHECK (operation IN ('insert', 'update', 'delete')),
    before_row jsonb,
    after_row  jsonb
);
CREATE INDEX IF NOT EXISTS change_log_time_idx ON garagebooks.change_log (changed_at DESC, id DESC);

CREATE OR REPLACE FUNCTION garagebooks.log_change() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    old_row jsonb := CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE to_jsonb(OLD) END;
    new_row jsonb := CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE to_jsonb(NEW) END;
BEGIN
    -- The app stamps updated_at on every save: an edit that changes nothing else is not a change.
    IF TG_OP = 'UPDATE' AND (old_row - 'updated_at') = (new_row - 'updated_at') THEN
        RETURN NULL;
    END IF;
    INSERT INTO garagebooks.change_log (actor, table_name, row_id, operation, before_row, after_row)
    VALUES (NULLIF(left(btrim(current_setting('garagebooks.actor', true)), 200), ''), TG_TABLE_NAME,
            (COALESCE(new_row, old_row) ->> 'id')::bigint, lower(TG_OP), old_row, new_row);
    RETURN NULL;
END
$fn$;

DROP TRIGGER IF EXISTS log_change ON garagebooks.expense;
CREATE TRIGGER log_change AFTER INSERT OR UPDATE OR DELETE ON garagebooks.expense
    FOR EACH ROW EXECUTE FUNCTION garagebooks.log_change();
DROP TRIGGER IF EXISTS log_change ON garagebooks.category;
CREATE TRIGGER log_change AFTER INSERT OR UPDATE OR DELETE ON garagebooks.category
    FOR EACH ROW EXECUTE FUNCTION garagebooks.log_change();

CREATE OR REPLACE FUNCTION garagebooks.change_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'garagebooks.change_log is append-only';
END
$fn$;

DROP TRIGGER IF EXISTS append_only ON garagebooks.change_log;
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON garagebooks.change_log
    FOR EACH ROW EXECUTE FUNCTION garagebooks.change_log_append_only();
DROP TRIGGER IF EXISTS append_only_truncate ON garagebooks.change_log;
CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON garagebooks.change_log
    FOR EACH STATEMENT EXECUTE FUNCTION garagebooks.change_log_append_only();

-- Lease details, second pass. Added to databases made before them; safe to run again.
--   associate_lease  the car is owned outright and leased to the employer by an associate: there is
--                    no financier and no residual (balloon), so those are left empty
--   form_url / form_path            the provider's lease form the details were read from, kept for reference
--   agreement_url / agreement_path  the lease agreement, kept for reference and never read
-- Each document is a link (OneDrive) or a path in the invoice folder on the server, as an expense's invoice is.
ALTER TABLE garagebooks.lease ALTER COLUMN financier DROP NOT NULL;
ALTER TABLE garagebooks.lease ADD COLUMN IF NOT EXISTS associate_lease boolean NOT NULL DEFAULT false;
ALTER TABLE garagebooks.lease ADD COLUMN IF NOT EXISTS form_url text
    CHECK (form_url IS NULL OR form_url ~* '^https://[^[:space:]]+$');
ALTER TABLE garagebooks.lease ADD COLUMN IF NOT EXISTS form_path text
    CHECK (form_path IS NULL OR (form_path LIKE '/_%' AND length(form_path) <= 1024));
ALTER TABLE garagebooks.lease ADD COLUMN IF NOT EXISTS agreement_url text
    CHECK (agreement_url IS NULL OR agreement_url ~* '^https://[^[:space:]]+$');
ALTER TABLE garagebooks.lease ADD COLUMN IF NOT EXISTS agreement_path text
    CHECK (agreement_path IS NULL OR (agreement_path LIKE '/_%' AND length(agreement_path) <= 1024));
-- Lease budgets (the provider's yearly budget pools) are optional and off until turned on in Settings:
-- the lease pages, the Categories page and the FBT year report leave them out while this is false.
ALTER TABLE garagebooks.setting ADD COLUMN IF NOT EXISTS lease_budgets boolean NOT NULL DEFAULT false;

-- Claims (roadmap L3, the "claimed" part): an expense sent to the salary packaging provider through a
-- claims connector (see docs/claims-connector.md), kept so nothing is claimed twice. One row per
-- expense. ON DELETE RESTRICT: a claimed expense can't be deleted until its claim record is removed
-- (the app says so). `fields` is what the owner typed for the claim form, as sent. approved_amount,
-- paid_date and status_checked_at are filled in when the provider's statuses are refreshed.
CREATE TABLE IF NOT EXISTS garagebooks.expense_claim (
    expense_id        bigint PRIMARY KEY REFERENCES garagebooks.expense (id) ON DELETE RESTRICT,
    provider          text NOT NULL CHECK (btrim(provider) <> ''),
    provider_id       text NOT NULL CHECK (btrim(provider_id) <> ''),
    reference         text,
    status            text,
    amount            numeric(12, 2),
    gst               numeric(12, 2),
    fields            jsonb,
    submitted_at      timestamptz NOT NULL DEFAULT now(),
    status_checked_at timestamptz,
    approved_amount   numeric(12, 2),
    paid_date         date
);

-- The change history covers claims too. A claim has no id of its own: its row_id is the expense's.
-- status_checked_at moves on every refresh, so like updated_at it doesn't count as a change.
ALTER TABLE garagebooks.change_log DROP CONSTRAINT IF EXISTS change_log_table_name_check;
ALTER TABLE garagebooks.change_log ADD CONSTRAINT change_log_table_name_check
    CHECK (table_name IN ('expense', 'category', 'expense_claim'));

CREATE OR REPLACE FUNCTION garagebooks.log_change() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    old_row jsonb := CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE to_jsonb(OLD) END;
    new_row jsonb := CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE to_jsonb(NEW) END;
BEGIN
    -- The app stamps updated_at on every save: an edit that changes nothing else is not a change.
    IF TG_OP = 'UPDATE' AND (old_row - 'updated_at' - 'status_checked_at') = (new_row - 'updated_at' - 'status_checked_at') THEN
        RETURN NULL;
    END IF;
    INSERT INTO garagebooks.change_log (actor, table_name, row_id, operation, before_row, after_row)
    VALUES (NULLIF(left(btrim(current_setting('garagebooks.actor', true)), 200), ''), TG_TABLE_NAME,
            COALESCE(COALESCE(new_row, old_row) ->> 'id', COALESCE(new_row, old_row) ->> 'expense_id')::bigint,
            lower(TG_OP), old_row, new_row);
    RETURN NULL;
END
$fn$;

DROP TRIGGER IF EXISTS log_change ON garagebooks.expense_claim;
CREATE TRIGGER log_change AFTER INSERT OR UPDATE OR DELETE ON garagebooks.expense_claim
    FOR EACH ROW EXECUTE FUNCTION garagebooks.log_change();

-- ---- the provider's transaction history (a cache) ----------------------------------------------------
-- What the claims connector says the provider paid or deducted, kept here so the Claims page can be filtered
-- and still opens when the connector is down. Rebuilt from the connector on every sync: nothing here is the
-- owner's own data, and it is not in the change history. (provider, provider_id) is the connector's own key.
-- kind: 'claim_payment' (a claim paid out), 'per_km_charging' (an electric car's cents-per-km home charging
-- reimbursement: it has no expense, and needs none) or 'other'. claim_provider_id is the claim it belongs to,
-- when the connector knows. expense_id is the expense it was matched to by the app (never by hand); deleting
-- the expense leaves the transaction unmatched.
CREATE TABLE IF NOT EXISTS garagebooks.provider_transaction (
    id                bigserial PRIMARY KEY,
    provider          text NOT NULL CHECK (btrim(provider) <> ''),
    provider_id       text NOT NULL CHECK (btrim(provider_id) <> ''),
    occurred_on       date NOT NULL,
    amount            numeric(12, 2) NOT NULL,
    gst               numeric(12, 2),
    type              text,
    status            text,
    description       text,
    payee             text,
    kind              text NOT NULL DEFAULT 'other' CHECK (kind IN ('claim_payment', 'per_km_charging', 'other')),
    car_id            integer REFERENCES garagebooks.car (id) ON DELETE SET NULL,
    claim_provider_id text,
    reference         text,
    expense_id        bigint REFERENCES garagebooks.expense (id) ON DELETE SET NULL,
    synced_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_id)
);
CREATE INDEX IF NOT EXISTS provider_transaction_on_idx ON garagebooks.provider_transaction (occurred_on DESC);
CREATE INDEX IF NOT EXISTS provider_transaction_expense_idx ON garagebooks.provider_transaction (expense_id);

-- The provider's claim history (also a cache): one row per claim line, so a payment, which may be only part of
-- a claim, can be checked against what the claim should have paid. expense_id is the expense the line is for:
-- the claim made from here (expense_claim.provider_id), else the one expense with the line's car, day and
-- amount. Both are set by the app, and a deleted expense leaves the line unmatched.
CREATE TABLE IF NOT EXISTS garagebooks.provider_claim (
    id              bigserial PRIMARY KEY,
    provider        text NOT NULL CHECK (btrim(provider) <> ''),
    provider_id     text NOT NULL CHECK (btrim(provider_id) <> ''),
    line_id         text NOT NULL DEFAULT '',
    reference       text,
    occurred_on     date NOT NULL,
    amount          numeric(12, 2) NOT NULL,
    approved_amount numeric(12, 2),
    status          text,
    paid_date       date,
    description     text,
    car_id          integer REFERENCES garagebooks.car (id) ON DELETE SET NULL,
    expense_id      bigint REFERENCES garagebooks.expense (id) ON DELETE SET NULL,
    synced_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_id, line_id)
);
CREATE INDEX IF NOT EXISTS provider_claim_expense_idx ON garagebooks.provider_claim (expense_id);
CREATE INDEX IF NOT EXISTS provider_claim_claim_idx ON garagebooks.provider_claim (provider_id);

-- Fetching the provider's history can take minutes (its own site is slow), so the app does it in the background
-- and keeps the state of the one running sync here, where every web worker can see it. One row, always id 1.
-- outcome: 'synced', 'signin' (the provider needs signing in) or 'problem' (message says why). A 'running' sync
-- older than half an hour is taken to have died with its worker.
CREATE TABLE IF NOT EXISTS garagebooks.provider_sync (
    id          integer PRIMARY KEY CHECK (id = 1),
    state       text NOT NULL DEFAULT 'idle' CHECK (state IN ('idle', 'running')),
    started_at  timestamptz,
    finished_at timestamptz,
    outcome     text CHECK (outcome IN ('synced', 'signin', 'problem')),
    message     text,
    since       date
);
INSERT INTO garagebooks.provider_sync (id) VALUES (1) ON CONFLICT DO NOTHING;

-- 'direct_payment': the provider paid something itself on the owner's behalf, with no claim made (a registration
-- renewal, say). It belongs to an expense like a claim does, and is matched to one by car, nearby day and amount.
ALTER TABLE garagebooks.provider_transaction DROP CONSTRAINT IF EXISTS provider_transaction_kind_check;
ALTER TABLE garagebooks.provider_transaction ADD CONSTRAINT provider_transaction_kind_check
    CHECK (kind IN ('claim_payment', 'per_km_charging', 'direct_payment', 'other'));

-- Kilometre pace (roadmap L4): the odometer at a moment, for the dashboard's panel, with the same answer
-- as sources.odometer_at in the app. A car TeslaMate tracks gets TeslaMate's last position at or before
-- the moment, else (and for any other car) the latest reading entered by hand or on an expense at or
-- before it; NULL when nothing is known, never 0. Built in two forms by build_odometer_at(), as
-- build_ownership() is: with TeslaMate's positions, or without them (there is no TeslaMate, or the app
-- is holding its figures back), when only what was entered by hand answers. Same arguments and result
-- either way. Both are SECURITY DEFINER, so the dashboard's reader needs no access to TeslaMate's tables
-- and the app's least-privilege role can switch forms; their only inputs are a car, a moment and a boolean.
CREATE OR REPLACE FUNCTION garagebooks.build_odometer_at(use_teslamate boolean DEFAULT true) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    tm      boolean := COALESCE(use_teslamate, false) AND to_regclass('public.positions') IS NOT NULL;
    teslamate text;
BEGIN
  LOOP
    teslamate := 'NULL::double precision';
    IF tm THEN
        teslamate := '(SELECT round(p.odometer::numeric, 1)::double precision FROM public.positions p
                       WHERE p.car_id = (SELECT c.teslamate_car_id FROM garagebooks.car c WHERE c.id = p_car)
                         AND p.odometer IS NOT NULL AND p.date <= (p_at AT TIME ZONE ''UTC'')
                       ORDER BY p.date DESC LIMIT 1)';
    END IF;
    BEGIN
        EXECUTE format($def$
CREATE OR REPLACE FUNCTION garagebooks.odometer_at(p_car integer, p_at timestamptz) RETURNS double precision
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $body$
    SELECT COALESCE(
        %s,
        (SELECT r.km::double precision FROM (
            SELECT o.km, o.read_at AS taken FROM garagebooks.odometer_reading o WHERE o.car_id = p_car AND o.read_at <= p_at
            UNION ALL
            SELECT e.odometer_km, e.occurred_at FROM garagebooks.expense e
            WHERE e.car_id = p_car AND e.odometer_km IS NOT NULL AND e.occurred_at <= p_at
        ) r ORDER BY r.taken DESC, r.km DESC LIMIT 1))
$body$$def$, teslamate);
        RETURN tm;
    EXCEPTION WHEN OTHERS THEN
        -- TeslaMate's tables are not as expected: build the form without them, so a TeslaMate that
        -- changed never stops GarageBooks starting.
        IF NOT tm THEN
            RAISE;
        END IF;
        RAISE WARNING 'garagebooks.odometer_at is built without TeslaMate: %', SQLERRM;
        tm := false;
    END;
  END LOOP;
END
$fn$;
REVOKE ALL ON FUNCTION garagebooks.build_odometer_at(boolean) FROM PUBLIC;
SELECT garagebooks.build_odometer_at(true);

-- Cost after tax (roadmap L5): what an expense paid from pre-tax salary through a novated lease's package
-- costs once the GST is claimed back and the rest comes from pre-tax salary. All of it is read-only
-- arithmetic on what is entered; nothing here is tax advice, and no salary is ever stored.
--   setting.marginal_tax_rate  the owner's marginal rate as a percentage, Medicare levy included (34 for
--                              32 + 2); NULL = not entered, and no after-tax figure is shown anywhere
--   expense.packaged           true = paid from the package, false = paid personally, NULL = automatic:
--                              packaged when a lease of the car covers the date, the category has a budget
--                              pool and is not a purchase
--   category.gst_free          "GST only partly applies" (the column kept its first name, when the option was
--                              "GST-free"): the GST is not taken to be one eleventh of the invoice, it is the
--                              expense's gst_amount, and with none known no GST is counted (registration: only its CTP
--                              part has GST). Added once, with its defaults; the owner's later edits are never overwritten
--   lease.charging_reimbursed  the provider reimburses home charging at the ATO's cents-per-km rate
ALTER TABLE garagebooks.setting ADD COLUMN IF NOT EXISTS marginal_tax_rate numeric(5, 2)
    CHECK (marginal_tax_rate IS NULL OR marginal_tax_rate BETWEEN 0 AND 100);
ALTER TABLE garagebooks.expense ADD COLUMN IF NOT EXISTS packaged boolean;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'garagebooks' AND table_name = 'category' AND column_name = 'gst_free') THEN
        ALTER TABLE garagebooks.category ADD COLUMN gst_free boolean NOT NULL DEFAULT false;
        -- The defaults are not an edit by anyone: keep them out of the change history.
        ALTER TABLE garagebooks.category DISABLE TRIGGER log_change;
        UPDATE garagebooks.category SET gst_free = true WHERE name = 'Registration';
        ALTER TABLE garagebooks.category ENABLE TRIGGER log_change;
    END IF;
END $$;
ALTER TABLE garagebooks.lease ADD COLUMN IF NOT EXISTS charging_reimbursed boolean NOT NULL DEFAULT false;

-- The ATO's rate for reimbursing an electric car's home charging, in cents per km, by FBT year (the year
-- it starts in, as fbt_year() numbers them). Data, not code: add or change a year on the Settings page.
-- A year with no row uses the latest earlier year's rate; a year before the first has none.
CREATE TABLE IF NOT EXISTS garagebooks.ev_charging_rate (
    fbt_year      integer PRIMARY KEY,
    cents_per_km  numeric(6, 2) NOT NULL CHECK (cents_per_km >= 0)
);
INSERT INTO garagebooks.ev_charging_rate (fbt_year, cents_per_km) VALUES
    (2022, 4.20), (2023, 4.20), (2024, 4.20), (2025, 4.20), (2026, 5.47)
ON CONFLICT (fbt_year) DO NOTHING;

-- Every expense with whether it is packaged and what it costs after tax, in the time zone `tz` (the date
-- an expense is on decides the lease that covers it, as for the FBT year). The one definition the app
-- and the dashboard share; tests/test_after_tax.py holds it to lease.effective_cost(). With no rate
-- entered effective_cost is NULL for a packaged expense: never the invoice standing in for it.
--   ex_gst    = amount, or amount x 10 / 11 unless the category's GST only partly applies (category.gst_free)
--   post      = the lease's post-tax payment / (pre-tax + post-tax payment), 0 with no lease or no payments
--   effective = ex_gst x ((1 - post) x (1 - rate) + post), rounded half away from zero to the cent
-- Written as one division so nothing is rounded before the end. It reads no TeslaMate table.
CREATE OR REPLACE FUNCTION garagebooks.expense_after_tax(tz text)
RETURNS TABLE (expense_id bigint, car_id smallint, occurred_at timestamptz, amount numeric,
               packaged boolean, effective_cost numeric)
LANGUAGE sql STABLE
AS $fn$
    SELECT e.id, e.car_id, e.occurred_at, e.amount, p.packaged,
           CASE WHEN NOT p.packaged THEN e.amount
                WHEN r.rate IS NULL THEN NULL
                ELSE round(e.amount * CASE WHEN k.gst_free THEN 1 ELSE 10 END
                               * (q.pre * (100 - r.rate) + 100 * q.post)
                               / ((CASE WHEN k.gst_free THEN 1 ELSE 11 END) * 100 * (q.pre + q.post)), 2) END
    FROM garagebooks.expense e
    JOIN garagebooks.category k ON k.id = e.category_id
    LEFT JOIN LATERAL (SELECT l.pretax_payment, l.posttax_payment FROM garagebooks.lease l
                       WHERE l.car_id = e.car_id
                         AND (e.occurred_at AT TIME ZONE tz)::date BETWEEN l.start_date AND l.end_date
                       ORDER BY l.start_date LIMIT 1) l ON true
    CROSS JOIN LATERAL (SELECT COALESCE(e.packaged, l.pretax_payment IS NOT NULL AND k.budget_pool IS NOT NULL
                                                    AND k.kind <> 'purchase') AS packaged) p
    CROSS JOIN LATERAL (SELECT CASE WHEN COALESCE(l.pretax_payment, 0) + COALESCE(l.posttax_payment, 0) > 0
                                    THEN l.pretax_payment ELSE 1 END AS pre,
                                   CASE WHEN COALESCE(l.pretax_payment, 0) + COALESCE(l.posttax_payment, 0) > 0
                                    THEN l.posttax_payment ELSE 0 END AS post) q
    CROSS JOIN (SELECT (SELECT marginal_tax_rate FROM garagebooks.setting WHERE id = 1) AS rate) r
$fn$;

-- Scheduled jobs (roadmap item 7): one row per job the app knows, written by the scheduler (jobs.py).
-- last_started_at is also the claim: a job that is due is taken with one UPDATE of this row, so two
-- web workers, or the app and a worker container, never both run it.
--   last_finished_at  NULL while it runs (or when the process stopped part-way)
--   last_ok           how the last run went; last_message is a few words for the Settings page
CREATE TABLE IF NOT EXISTS garagebooks.job (
    name             text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_]{0,39}$'),
    last_started_at  timestamptz,
    last_finished_at timestamptz,
    last_ok          boolean,
    last_message     text CHECK (last_message IS NULL OR length(last_message) <= 500)
);

-- Notifications (roadmap item 8): the Settings page's address for push notices, and what has been sent.
--   notify_url     where a notice is POSTed (an ntfy topic, an Apprise API, a Home Assistant webhook): a
--                  secret for most of them, so it is never shown back, logged or put in an error. NULL = off.
--   notify_format  'ntfy' (the message as text, the title in a header) or 'json' (for Apprise and
--                  Home Assistant). NULL = ntfy.
--   notification   one row per notice sent, so each thing is told once. The key names the event and
--                  what it was about (for a reminder, the date and odometer it was due on), so a
--                  reminder that is edited is a new event. Rows are pruned after a year.
--   onedrive_account.signin_lost_at / signin_checked_at: set when a refresh finds the saved sign-in no
--                  longer works, and when it was last tried, so the job needn't ask Microsoft every run.
ALTER TABLE garagebooks.setting ADD COLUMN IF NOT EXISTS notify_url text
    CHECK (notify_url IS NULL OR (notify_url ~* '^https?://[^[:space:]]+$' AND length(notify_url) <= 500));
ALTER TABLE garagebooks.setting ADD COLUMN IF NOT EXISTS notify_format text
    CHECK (notify_format IS NULL OR notify_format IN ('ntfy', 'json'));
CREATE TABLE IF NOT EXISTS garagebooks.notification (
    key     text PRIMARY KEY CHECK (length(key) <= 200),
    sent_at timestamptz NOT NULL DEFAULT now(),
    title   text NOT NULL CHECK (length(title) <= 300)
);
--   notify_teslamate  whether to tell the owner when TeslaMate is newer than this release was tested with.
ALTER TABLE garagebooks.setting ADD COLUMN IF NOT EXISTS notify_teslamate boolean NOT NULL DEFAULT false;
ALTER TABLE garagebooks.onedrive_account ADD COLUMN IF NOT EXISTS signin_lost_at timestamptz;
ALTER TABLE garagebooks.onedrive_account ADD COLUMN IF NOT EXISTS signin_checked_at timestamptz;

-- Invoices by email: an Outlook.com mailbox folder per car, read with the OneDrive app registration.
--   onedrive_account.mail_refresh_token  the mailbox's own sign-in (Mail.Read), encrypted like refresh_token;
--                  a separate sign-in from the drive's, so it can be another account, and NULL = not connected
--   mail_account / mail_signin_lost_at / mail_signin_checked_at: as account and signin_*_at, for the mailbox
--   car_mail_folder  the folder each car's emailed invoices arrive in (where they come from, not where they
--                  are kept: that stays car_folder). waiting / newest_at are what the mail job last found
--                  not yet imported, so a notice can say so once per new email.
--   mail_import    which emails (and which PDF in each) became which expense, by the email's link; an
--                  expense that is deleted takes its row with it, and the email is offered again.
ALTER TABLE garagebooks.onedrive_account ADD COLUMN IF NOT EXISTS mail_refresh_token text;
ALTER TABLE garagebooks.onedrive_account ADD COLUMN IF NOT EXISTS mail_account text;
ALTER TABLE garagebooks.onedrive_account ADD COLUMN IF NOT EXISTS mail_signin_lost_at timestamptz;
ALTER TABLE garagebooks.onedrive_account ADD COLUMN IF NOT EXISTS mail_signin_checked_at timestamptz;
CREATE TABLE IF NOT EXISTS garagebooks.car_mail_folder (
    car_id      smallint PRIMARY KEY REFERENCES garagebooks.car (id) ON DELETE CASCADE,
    folder_id   text NOT NULL CHECK (folder_id <> ''),
    folder_path text NOT NULL,
    waiting     integer CHECK (waiting IS NULL OR waiting >= 0),
    newest_at   timestamptz,
    checked_at  timestamptz
);
CREATE TABLE IF NOT EXISTS garagebooks.mail_import (
    ref         text PRIMARY KEY CHECK (ref ~* '^https://[^[:space:]]+$' AND length(ref) <= 4000),
    car_id      smallint NOT NULL REFERENCES garagebooks.car (id) ON DELETE CASCADE,
    expense_id  bigint NOT NULL REFERENCES garagebooks.expense (id) ON DELETE CASCADE,
    imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mail_import_expense_idx ON garagebooks.mail_import (expense_id);

-- The Microsoft account: the app registration OneDrive and the mailbox share.
--   onedrive_account.callback  the redirect URI its sign-ins use: 'microsoft' (/microsoft/callback) for a
--                  registration saved since that address was added; NULL for one saved before, which keeps
--                  /onedrive/callback, the address its installer registered. Saving the registration again moves it.
ALTER TABLE garagebooks.onedrive_account ADD COLUMN IF NOT EXISTS callback text
    CHECK (callback IS NULL OR callback = 'microsoft');

-- Import rows and periods. Added to databases made before them; safe to run again.
--   expense.period_start / period_end  the days an insurance policy or a registration covers (read from the
--                                      document when imported). Either may be empty; if both are there the end
--                                      is not before the start.
--   category.has_period                the category's expenses have such a period (insurance and registration
--                                      by default; the owner's later edits are never overwritten)
ALTER TABLE garagebooks.expense ADD COLUMN IF NOT EXISTS period_start date;
ALTER TABLE garagebooks.expense ADD COLUMN IF NOT EXISTS period_end date;
ALTER TABLE garagebooks.expense DROP CONSTRAINT IF EXISTS expense_period_order;
ALTER TABLE garagebooks.expense ADD CONSTRAINT expense_period_order
    CHECK (period_start IS NULL OR period_end IS NULL OR period_end >= period_start);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'garagebooks' AND table_name = 'category' AND column_name = 'has_period') THEN
        ALTER TABLE garagebooks.category ADD COLUMN has_period boolean NOT NULL DEFAULT false;
        ALTER TABLE garagebooks.category DISABLE TRIGGER log_change;
        UPDATE garagebooks.category SET has_period = true WHERE name IN ('Insurance', 'Registration');
        ALTER TABLE garagebooks.category ENABLE TRIGGER log_change;
    END IF;
END $$;

-- Services: the Service category, and the items a service covered (fuel filter, brake fluid...), chosen per
-- expense. The list is the owner's to edit on the Categories page, so it is seeded once, when the table is
-- made: running this file again never brings back an item that was removed. An item's name is unique ignoring
-- case. Removing an item unlinks it from the expenses that had it; renaming keeps the links.
--   service_item          the list
--   expense_service_item  which items an expense had
--   expense_service       one row per expense and item, with what Grafana needs to chart or tabulate them
DO $$
BEGIN
    IF to_regclass('garagebooks.service_item') IS NULL THEN
        CREATE TABLE garagebooks.service_item (
            id         smallserial PRIMARY KEY,
            name       text NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 60),
            sort_order integer NOT NULL DEFAULT 100
        );
        INSERT INTO garagebooks.service_item (name, sort_order) VALUES
            ('Fuel filter', 10), ('Oil filter', 20), ('Engine oil', 30), ('Tyre rotation', 40),
            ('Windscreen washer fluid', 50), ('Windscreen wipers', 60), ('Brakes', 70), ('Brake fluid', 80),
            ('Timing belt', 90), ('Air filter', 91), ('Coolant', 92), ('Cabin air filter', 93), ('Wheel alignment', 94);
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS service_item_name_key ON garagebooks.service_item (lower(name));
CREATE TABLE IF NOT EXISTS garagebooks.expense_service_item (
    expense_id bigint   NOT NULL REFERENCES garagebooks.expense (id) ON DELETE CASCADE,
    item_id    smallint NOT NULL REFERENCES garagebooks.service_item (id) ON DELETE CASCADE,
    PRIMARY KEY (expense_id, item_id)
);
CREATE INDEX IF NOT EXISTS expense_service_item_item_idx ON garagebooks.expense_service_item (item_id);
-- The Service category is added once, in the maintenance budget pool, and not as anyone's edit: kept out of the
-- change history. An existing category of that name is left as it is.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM garagebooks.category WHERE name = 'Service') THEN
        ALTER TABLE garagebooks.category DISABLE TRIGGER log_change;
        INSERT INTO garagebooks.category (name, kind, sort_order, budget_pool, upload_folder)
        VALUES ('Service', 'running', 85, 'maintenance', 'Maintenance');
        ALTER TABLE garagebooks.category ENABLE TRIGGER log_change;
    END IF;
END $$;
CREATE OR REPLACE VIEW garagebooks.expense_service AS
    SELECT e.id AS expense_id, e.car_id, e.occurred_at, e.amount, e.vendor, e.odometer_km, i.id AS item_id, i.name AS item
    FROM garagebooks.expense_service_item l
    JOIN garagebooks.expense e ON e.id = l.expense_id
    JOIN garagebooks.service_item i ON i.id = l.item_id;

-- Registration's GST, and the payment references on a registration notice.
--   expense.gst_amount        the GST in the amount, as the document states it. When there is one it replaces the
--                             category's GST rule (category.gst_free, or one eleventh of the amount) in
--                             expense_after_tax(): GST on a registration is only on its CTP insurance part, so
--                             neither rule fits. Between 0 and the amount (the same sign as the amount).
--   expense.card_payment_ref, bpay_biller_code, bpay_ref
--                             how the notice says to pay it. Never listed or charted; the edit form keeps them in a
--                             closed "Payment references" section, and the change history doesn't show them.
ALTER TABLE garagebooks.expense ADD COLUMN IF NOT EXISTS gst_amount numeric(12, 2);
ALTER TABLE garagebooks.expense DROP CONSTRAINT IF EXISTS expense_gst_amount_range;
ALTER TABLE garagebooks.expense ADD CONSTRAINT expense_gst_amount_range
    CHECK (gst_amount IS NULL OR (amount >= 0 AND gst_amount BETWEEN 0 AND amount)
                              OR (amount < 0 AND gst_amount BETWEEN amount AND 0));
ALTER TABLE garagebooks.expense ADD COLUMN IF NOT EXISTS card_payment_ref text
    CHECK (card_payment_ref IS NULL OR length(card_payment_ref) <= 60);
ALTER TABLE garagebooks.expense ADD COLUMN IF NOT EXISTS bpay_biller_code text
    CHECK (bpay_biller_code IS NULL OR length(bpay_biller_code) <= 60);
ALTER TABLE garagebooks.expense ADD COLUMN IF NOT EXISTS bpay_ref text
    CHECK (bpay_ref IS NULL OR length(bpay_ref) <= 60);

-- The after-tax function again, with the stored GST: ex_gst = amount - gst_amount when there is one.
CREATE OR REPLACE FUNCTION garagebooks.expense_after_tax(tz text)
RETURNS TABLE (expense_id bigint, car_id smallint, occurred_at timestamptz, amount numeric,
               packaged boolean, effective_cost numeric)
LANGUAGE sql STABLE
AS $fn$
    SELECT e.id, e.car_id, e.occurred_at, e.amount, p.packaged,
           CASE WHEN NOT p.packaged THEN e.amount
                WHEN r.rate IS NULL THEN NULL
                WHEN e.gst_amount IS NOT NULL
                    THEN round((e.amount - e.gst_amount) * (q.pre * (100 - r.rate) + 100 * q.post)
                               / (100 * (q.pre + q.post)), 2)
                ELSE round(e.amount * CASE WHEN k.gst_free THEN 1 ELSE 10 END
                               * (q.pre * (100 - r.rate) + 100 * q.post)
                               / ((CASE WHEN k.gst_free THEN 1 ELSE 11 END) * 100 * (q.pre + q.post)), 2) END
    FROM garagebooks.expense e
    JOIN garagebooks.category k ON k.id = e.category_id
    LEFT JOIN LATERAL (SELECT l.pretax_payment, l.posttax_payment FROM garagebooks.lease l
                       WHERE l.car_id = e.car_id
                         AND (e.occurred_at AT TIME ZONE tz)::date BETWEEN l.start_date AND l.end_date
                       ORDER BY l.start_date LIMIT 1) l ON true
    CROSS JOIN LATERAL (SELECT COALESCE(e.packaged, l.pretax_payment IS NOT NULL AND k.budget_pool IS NOT NULL
                                                    AND k.kind <> 'purchase') AS packaged) p
    CROSS JOIN LATERAL (SELECT CASE WHEN COALESCE(l.pretax_payment, 0) + COALESCE(l.posttax_payment, 0) > 0
                                    THEN l.pretax_payment ELSE 1 END AS pre,
                                   CASE WHEN COALESCE(l.pretax_payment, 0) + COALESCE(l.posttax_payment, 0) > 0
                                    THEN l.posttax_payment ELSE 0 END AS post) q
    CROSS JOIN (SELECT (SELECT marginal_tax_rate FROM garagebooks.setting WHERE id = 1) AS rate) r
$fn$;

-- Car details read from the purchase invoice (all optional): the VIN (17 characters, without I, O and Q), the
-- year it was built, its model year and its body type. Filled in from the import, only where blank; the Cars
-- page edits them. None of them is a TeslaMate-sourced field, so a sync never touches them.
ALTER TABLE garagebooks.car ADD COLUMN IF NOT EXISTS vin text
    CHECK (vin IS NULL OR vin ~ '^[A-HJ-NPR-Z0-9]{17}$');
ALTER TABLE garagebooks.car ADD COLUMN IF NOT EXISTS build_year smallint
    CHECK (build_year IS NULL OR build_year BETWEEN 1900 AND 2100);
ALTER TABLE garagebooks.car ADD COLUMN IF NOT EXISTS model_year smallint
    CHECK (model_year IS NULL OR model_year BETWEEN 1900 AND 2100);
ALTER TABLE garagebooks.car ADD COLUMN IF NOT EXISTS body_type text
    CHECK (body_type IS NULL OR (btrim(body_type) <> '' AND length(body_type) <= 40));

-- Documents attached to an expense: the other PDFs merged into it on the import page (an invoice plus its service
-- report, say). The expense's own invoice stays in expense.invoice_url / invoice_path; each document is a link
-- (OneDrive) or a path in the invoice folder on the server, one or the other, as an invoice is, and counts as known
-- so it is never offered for import again. Deleted with its expense; removing one only forgets it (the file stays).
CREATE TABLE IF NOT EXISTS garagebooks.expense_document (
    id           bigserial PRIMARY KEY,
    expense_id   bigint NOT NULL REFERENCES garagebooks.expense (id) ON DELETE CASCADE,
    name         text NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 300),
    invoice_url  text CHECK (invoice_url IS NULL OR invoice_url ~* '^https://[^[:space:]]+$'),
    invoice_path text CHECK (invoice_path IS NULL OR (invoice_path LIKE '/_%' AND length(invoice_path) <= 1024)),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK ((invoice_url IS NULL) <> (invoice_path IS NULL))
);
CREATE INDEX IF NOT EXISTS expense_document_expense_idx ON garagebooks.expense_document (expense_id);
CREATE UNIQUE INDEX IF NOT EXISTS expense_document_url_key ON garagebooks.expense_document (invoice_url) WHERE invoice_url IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS expense_document_path_key ON garagebooks.expense_document (invoice_path) WHERE invoice_path IS NOT NULL;

-- Service items in reminders, and the end of the service-type categories.
--   reminder.service_item_id  a reminder can be for a service item ("Cabin air filter every 2 years"): an expense
--                             with that item ticked resolves it, as one in the reminder's category does. Removing an
--                             item leaves its reminders as they were, without the item.
ALTER TABLE garagebooks.reminder ADD COLUMN IF NOT EXISTS service_item_id smallint
    REFERENCES garagebooks.service_item (id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS reminder_item_idx ON garagebooks.reminder (service_item_id) WHERE service_item_id IS NOT NULL;
-- reminder_due again, with the item as its last column (the lease-derived rows have none).
CREATE OR REPLACE VIEW garagebooks.reminder_due AS
SELECT 'reminder'::text AS kind, r.id AS reminder_id, r.car_id, r.title, r.category_id, r.due_date,
       r.due_km, r.lead_days, r.lead_km, false AS informational, r.service_item_id, NULL::date AS next_lease_start
FROM garagebooks.reminder r
WHERE r.resolved_at IS NULL
UNION ALL
SELECT 'registration', NULL::integer, cl.car_id, 'Registration expires', NULL::smallint, cl.rego_expiry,
       NULL::numeric(9, 1), 30, 0::numeric(9, 1), cl.rego_renewed_by_provider, NULL::smallint, NULL::date
FROM garagebooks.lease cl
WHERE cl.rego_expiry IS NOT NULL
  AND cl.id = (SELECT l.id FROM garagebooks.lease l WHERE l.car_id = cl.car_id AND l.end_date >= CURRENT_DATE
               ORDER BY l.start_date LIMIT 1)
UNION ALL
SELECT 'lease_end', NULL::integer, cl.car_id, 'Lease ends', NULL::smallint, cl.end_date,
       NULL::numeric(9, 1), 90, 0::numeric(9, 1), false, NULL::smallint, NULL::date
FROM garagebooks.lease cl
WHERE cl.id = (SELECT l.id FROM garagebooks.lease l WHERE l.car_id = cl.car_id AND l.end_date >= CURRENT_DATE
               ORDER BY l.start_date LIMIT 1);

-- Releases before this had the categories Cabin air filter, Air filter, Wheel alignment and Tyre rotation. They are
-- items of the Service category now (Tyres stays a category of its own: tyres are bought, not serviced). Once, on
-- the first start that finds them (a comment on service_item says it has been done, so a category of that name
-- the owner adds later is theirs to keep), for each one that exists:
--   - its item is added to the list if missing (matched ignoring case);
--   - every expense in it moves to Service with that item ticked (the change history records the move);
--   - its reminders move to Service and to that item, so they are resolved by an expense with the item ticked;
--   - what the importer learned about vendors moves over (hits add up);
--   - Service takes its invoice subfolder if Service has none; the budget pool is left as it is (Service is in the
--     maintenance pool, and the owner's choice for it is never overwritten);
--   - the category is deleted, but only when nothing in any table refers to it any more (not logged in the history).
DO $$
DECLARE
    old record;
    svc smallint;
    item smallint;
    ref record;
    in_use boolean;
BEGIN
    IF obj_description('garagebooks.service_item'::regclass, 'pg_class') IS NOT DISTINCT FROM 'service categories moved' THEN
        RETURN;
    END IF;
    SELECT id INTO svc FROM garagebooks.category WHERE name = 'Service';
    IF svc IS NULL THEN
        RETURN;
    END IF;
    -- Housekeeping of the standard categories is not anyone's edit: kept out of the change history (the moves of
    -- the owner's expenses, which are changes to their data, are logged as usual).
    ALTER TABLE garagebooks.category DISABLE TRIGGER log_change;
    FOR old IN SELECT id, name, upload_folder FROM garagebooks.category
               WHERE name IN ('Cabin air filter', 'Air filter', 'Wheel alignment', 'Tyre rotation') ORDER BY sort_order, id
    LOOP
        INSERT INTO garagebooks.service_item (name, sort_order) VALUES (old.name, 94) ON CONFLICT DO NOTHING;
        SELECT id INTO item FROM garagebooks.service_item WHERE lower(name) = lower(old.name);
        INSERT INTO garagebooks.expense_service_item (expense_id, item_id)
            SELECT id, item FROM garagebooks.expense WHERE category_id = old.id ON CONFLICT DO NOTHING;
        UPDATE garagebooks.expense SET category_id = svc WHERE category_id = old.id;
        UPDATE garagebooks.reminder SET category_id = svc, service_item_id = item WHERE category_id = old.id;
        INSERT INTO garagebooks.learned_vendor (vendor, category_id, hits)
            SELECT vendor, svc, hits FROM garagebooks.learned_vendor WHERE category_id = old.id
        ON CONFLICT (vendor, category_id) DO UPDATE SET hits = garagebooks.learned_vendor.hits + EXCLUDED.hits;
        DELETE FROM garagebooks.learned_vendor WHERE category_id = old.id;
        UPDATE garagebooks.category SET upload_folder = old.upload_folder
            WHERE id = svc AND upload_folder IS NULL AND old.upload_folder IS NOT NULL;
        in_use := false;
        FOR ref IN SELECT k.conrelid::regclass AS tbl, a.attname AS col
                   FROM pg_constraint k JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1]
                   WHERE k.contype = 'f' AND k.confrelid = 'garagebooks.category'::regclass
        LOOP
            EXECUTE format('SELECT EXISTS (SELECT 1 FROM %s WHERE %I = $1)', ref.tbl, ref.col) INTO in_use USING old.id;
            EXIT WHEN in_use;
        END LOOP;
        IF NOT in_use THEN
            DELETE FROM garagebooks.category WHERE id = old.id;
        END IF;
    END LOOP;
    ALTER TABLE garagebooks.category ENABLE TRIGGER log_change;
    COMMENT ON TABLE garagebooks.service_item IS 'service categories moved';
END $$;

-- The tax rate by income year (1 July to 30 June), replacing the single marginal rate of earlier releases.
--   tax_bracket  the ATO's resident income tax brackets for each income year (the year it starts in: 2025 is
--                2025-26), as data. Seeded with the published years; the app's ato_rates job reads the ATO's page
--                and replaces a year's rows only when the page reads cleanly (checked_at: when it last did).
--                The tax on an income in a bracket is base_tax + rate% x (income - threshold).
--   tax_choice   each person's bracket and Medicare levy status for an income year, chosen on the Settings page.
--                A bracket stands for an income (its middle, or $10,000 into the top one): never a salary. A year
--                with no choice uses the latest earlier year's, by the income it stands for.
--                person: 'employee' (the one packaging the car) or 'associate' (their partner, for an associate lease)
--                medicare: not_exempt (2% levy), half_exempt (1%) or exempt (0)
CREATE TABLE IF NOT EXISTS garagebooks.tax_bracket (
    income_year  integer NOT NULL CHECK (income_year BETWEEN 1990 AND 2200),
    lower_income integer NOT NULL CHECK (lower_income >= 0),
    threshold    numeric(12, 2) NOT NULL CHECK (threshold >= 0),
    base_tax     numeric(12, 2) NOT NULL CHECK (base_tax >= 0),
    rate         numeric(5, 2) NOT NULL CHECK (rate BETWEEN 0 AND 100),
    checked_at   timestamptz,
    PRIMARY KEY (income_year, lower_income)
);
INSERT INTO garagebooks.tax_bracket (income_year, lower_income, threshold, base_tax, rate)
SELECT y, b.lower_income, b.threshold, b.base_tax, b.rate
FROM generate_series(2020, 2023) y,
     (VALUES (0, 0, 0, 0), (18201, 18200, 0, 19), (45001, 45000, 5092, 32.5), (120001, 120000, 29467, 37),
             (180001, 180000, 51667, 45)) b (lower_income, threshold, base_tax, rate)
UNION ALL
SELECT y, b.lower_income, b.threshold, b.base_tax, b.rate
FROM generate_series(2024, 2025) y,
     (VALUES (0, 0, 0, 0), (18201, 18200, 0, 16), (45001, 45000, 4288, 30), (135001, 135000, 31288, 37),
             (190001, 190000, 51638, 45)) b (lower_income, threshold, base_tax, rate)
UNION ALL
SELECT 2026, b.lower_income, b.threshold, b.base_tax, b.rate
FROM (VALUES (0, 0, 0, 0), (18201, 18200, 0, 15), (45001, 45000, 4020, 30), (135001, 135000, 31020, 37),
             (190001, 190000, 51370, 45)) b (lower_income, threshold, base_tax, rate)
ON CONFLICT (income_year, lower_income) DO NOTHING;
CREATE TABLE IF NOT EXISTS garagebooks.tax_choice (
    person        text NOT NULL CHECK (person IN ('employee', 'associate')),
    income_year   integer NOT NULL CHECK (income_year BETWEEN 1990 AND 2200),
    bracket_lower integer NOT NULL CHECK (bracket_lower >= 0),
    medicare      text NOT NULL DEFAULT 'not_exempt' CHECK (medicare IN ('not_exempt', 'half_exempt', 'exempt')),
    PRIMARY KEY (person, income_year)
);
-- The home charging rates gain where they came from and when the ato_rates job last confirmed them (NULL: the
-- rate shipped with GarageBooks, not yet checked).
ALTER TABLE garagebooks.ev_charging_rate ADD COLUMN IF NOT EXISTS source_url text
    CHECK (source_url IS NULL OR source_url ~ '^https://www\.ato\.gov\.au/');
ALTER TABLE garagebooks.ev_charging_rate ADD COLUMN IF NOT EXISTS checked_at timestamptz;

-- The income year a date falls in, as the year it starts in (2025 is 2025-26), like fbt_year().
CREATE OR REPLACE FUNCTION garagebooks.income_year(d date) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT CASE WHEN EXTRACT(month FROM d) >= 7 THEN EXTRACT(year FROM d)::integer
                  ELSE EXTRACT(year FROM d)::integer - 1 END $$;

-- The ATO year whose brackets an income year uses: its own, else the newest earlier one (a year not published
-- yet), else the oldest (a year before the first).
CREATE OR REPLACE FUNCTION garagebooks.tax_table_year(y integer) RETURNS integer
LANGUAGE sql STABLE
AS $$ SELECT COALESCE((SELECT max(income_year) FROM garagebooks.tax_bracket WHERE income_year <= y),
                      (SELECT min(income_year) FROM garagebooks.tax_bracket)) $$;

-- A person's tax position in an income year: the choice it comes from (that year's, or the latest earlier one),
-- the income its bracket stands for, and the bracket that income falls in this year. No row when nothing is
-- chosen at or before the year, or the chosen bracket is no longer one of its year's.
--   marginal_rate = the bracket's rate + the Medicare levy (2, 1 or 0), as a percentage.
-- tests/test_tax_years.py holds it to tax.py's labels and the Settings page.
CREATE OR REPLACE FUNCTION garagebooks.tax_position(who text, y integer)
RETURNS TABLE (chosen_year integer, table_year integer, income numeric, lower_income integer, threshold numeric,
               base_tax numeric, rate numeric, medicare text, levy numeric, marginal_rate numeric)
LANGUAGE sql STABLE
AS $fn$
    WITH c AS (SELECT * FROM garagebooks.tax_choice WHERE person = who AND income_year <= y
               ORDER BY income_year DESC LIMIT 1),
    b AS (SELECT t.*, lead(t.lower_income) OVER (PARTITION BY t.income_year ORDER BY t.lower_income) AS next_lower
          FROM garagebooks.tax_bracket t),
    i AS (SELECT c.income_year, c.medicare,
                 CASE WHEN b.next_lower IS NULL THEN b.lower_income + 10000
                      ELSE (b.lower_income + b.next_lower - 1) / 2.0 END AS income
          FROM c JOIN b ON b.income_year = garagebooks.tax_table_year(c.income_year) AND b.lower_income = c.bracket_lower)
    SELECT i.income_year, t.income_year, i.income, t.lower_income, t.threshold, t.base_tax, t.rate, i.medicare, m.levy,
           t.rate + m.levy
    FROM i
    CROSS JOIN LATERAL (SELECT CASE i.medicare WHEN 'not_exempt' THEN 2 WHEN 'half_exempt' THEN 1 ELSE 0 END::numeric AS levy) m
    JOIN LATERAL (SELECT * FROM b WHERE b.income_year = garagebooks.tax_table_year(y) AND b.lower_income <= i.income
                  ORDER BY b.lower_income DESC LIMIT 1) t ON true
$fn$;

-- The marginal rate (Medicare levy included, as a percentage) of a person on a date, by its income year; NULL
-- when none is chosen for that year or any earlier one.
CREATE OR REPLACE FUNCTION garagebooks.marginal_rate(who text, d date) RETURNS numeric
LANGUAGE sql STABLE
AS $$ SELECT marginal_rate FROM garagebooks.tax_position(who, garagebooks.income_year(d)) $$;

-- The single rate of earlier releases, once: if one was entered, every income year from the first with brackets
-- to the current one gets the bracket whose rate + 2% (not exempt) is nearest it (the lower on a tie), so the
-- after-tax figures stay the same where a bracket matches. The old column is then cleared, and no longer read.
DO $$
DECLARE
    old numeric;
BEGIN
    SELECT marginal_tax_rate INTO old FROM garagebooks.setting WHERE id = 1;
    IF old IS NULL OR EXISTS (SELECT 1 FROM garagebooks.tax_choice WHERE person = 'employee') THEN
        RETURN;
    END IF;
    INSERT INTO garagebooks.tax_choice (person, income_year, bracket_lower, medicare)
    SELECT 'employee', y, (SELECT b.lower_income FROM garagebooks.tax_bracket b
                           WHERE b.income_year = garagebooks.tax_table_year(y)
                           ORDER BY abs(b.rate + 2 - old), b.rate LIMIT 1), 'not_exempt'
    FROM generate_series((SELECT min(income_year) FROM garagebooks.tax_bracket),
                         garagebooks.income_year(CURRENT_DATE)) y;
    UPDATE garagebooks.setting SET marginal_tax_rate = NULL WHERE id = 1;
END $$;

-- The after-tax function again, with the rate by the expense's date: the employee's marginal rate for the
-- income year its date (in `tz`) falls in (marginal_rate()). Its columns are as before.
CREATE OR REPLACE FUNCTION garagebooks.expense_after_tax(tz text)
RETURNS TABLE (expense_id bigint, car_id smallint, occurred_at timestamptz, amount numeric,
               packaged boolean, effective_cost numeric)
LANGUAGE sql STABLE
AS $fn$
    SELECT e.id, e.car_id, e.occurred_at, e.amount, p.packaged,
           CASE WHEN NOT p.packaged THEN e.amount
                WHEN r.rate IS NULL THEN NULL
                WHEN e.gst_amount IS NOT NULL
                    THEN round((e.amount - e.gst_amount) * (q.pre * (100 - r.rate) + 100 * q.post)
                               / (100 * (q.pre + q.post)), 2)
                ELSE round(e.amount * CASE WHEN k.gst_free THEN 1 ELSE 10 END
                               * (q.pre * (100 - r.rate) + 100 * q.post)
                               / ((CASE WHEN k.gst_free THEN 1 ELSE 11 END) * 100 * (q.pre + q.post)), 2) END
    FROM garagebooks.expense e
    JOIN garagebooks.category k ON k.id = e.category_id
    LEFT JOIN LATERAL (SELECT l.pretax_payment, l.posttax_payment FROM garagebooks.lease l
                       WHERE l.car_id = e.car_id
                         AND (e.occurred_at AT TIME ZONE tz)::date BETWEEN l.start_date AND l.end_date
                       ORDER BY l.start_date LIMIT 1) l ON true
    CROSS JOIN LATERAL (SELECT COALESCE(e.packaged, l.pretax_payment IS NOT NULL AND k.budget_pool IS NOT NULL
                                                    AND k.kind <> 'purchase') AS packaged) p
    CROSS JOIN LATERAL (SELECT CASE WHEN COALESCE(l.pretax_payment, 0) + COALESCE(l.posttax_payment, 0) > 0
                                    THEN l.pretax_payment ELSE 1 END AS pre,
                                   CASE WHEN COALESCE(l.pretax_payment, 0) + COALESCE(l.posttax_payment, 0) > 0
                                    THEN l.posttax_payment ELSE 0 END AS post) q
    CROSS JOIN LATERAL (SELECT garagebooks.marginal_rate('employee', (e.occurred_at AT TIME ZONE tz)::date) AS rate) r
$fn$;

-- An associate lease after tax, for the household (roadmap L7, docs/lease-calculations.md "The what-if model").
--   lease.depreciation_value  the car's value for depreciation at the lease's start (the associate owns it); NULL
--                             uses the lease's car cost. Never more than the car limit counts.
--   car_limit                 the ATO's car limit by income year (the year it starts in), as data: the most of a
--                             car's cost that can be depreciated. A lease uses the limit of the income year it starts
--                             in, or the latest earlier year's.
ALTER TABLE garagebooks.lease ADD COLUMN IF NOT EXISTS depreciation_value numeric(12, 2)
    CHECK (depreciation_value IS NULL OR depreciation_value >= 0);
CREATE TABLE IF NOT EXISTS garagebooks.car_limit (
    income_year integer PRIMARY KEY CHECK (income_year BETWEEN 1990 AND 2200),
    amount      numeric(12, 2) NOT NULL CHECK (amount > 0)
);
INSERT INTO garagebooks.car_limit (income_year, amount) VALUES
    (2020, 59136), (2021, 60733), (2022, 64741), (2023, 68108), (2024, 69674), (2025, 69674)
ON CONFLICT (income_year) DO NOTHING;

-- The income tax on a taxable income by an income year's brackets (base + rate% x (income - threshold)); 0 for
-- an income of 0 or less. Unrounded: callers round once, at the end.
CREATE OR REPLACE FUNCTION garagebooks.income_tax(table_year integer, income numeric) RETURNS numeric
LANGUAGE sql STABLE
AS $$ SELECT COALESCE((SELECT b.base_tax + b.rate / 100 * (income - b.threshold) FROM garagebooks.tax_bracket b
                       WHERE b.income_year = table_year AND b.lower_income <= income AND income > 0
                       ORDER BY b.lower_income DESC LIMIT 1), 0) $$;

-- Each lease's figures for each income year (1 July to 30 June) it is held in: the one definition the FBT year
-- report and the dashboard share. Money is rounded to the cent once per figure. A figure that needs something not
-- entered (a bracket, the car's value) is NULL, never 0.
--   pays              pay dates in the year, stepping from the lease's start by its frequency (as lease.pay_dates)
--   payments          (pre-tax + post-tax payment) x pays: the lease payments. For an associate lease the associate
--                     receives them.
--   pretax            the pre-tax part of them, plus the admin fee x pays: what comes from pre-tax salary
--   employee_saving   pretax x your marginal rate for the year
--   depreciation      associate lease only: diminishing value, 25% a year (200% / 8 years) of the value at the
--                     start of the year x days held / days in the year; the first year's value is the lower of
--                     depreciation_value (or car_cost) and the car limit, each later year's is that less the
--                     depreciation before it
--   business_income   payments - depreciation (the associate's; a loss is negative)
--   associate_tax     the associate's income tax on (the income their bracket stands for + business_income), less on
--                     the income alone, by the year's brackets
--   associate_medicare  their levy (2%, 1% or 0) x business_income
--   small_business_offset  min(16% x their tax on the higher income x business_income / that income, $1,000); 0 for a loss
--   associate_cost    associate_tax + associate_medicare - small_business_offset: their extra tax
--   household_cost    associate lease: admin fees - employee_saving + associate_cost (the payments stay in the
--                     household); otherwise payments + admin fees - employee_saving. Negative is a saving.
-- Not modelled: the low income tax offset, the Medicare levy's low-income phase-in, GST on the payments, HELP.
CREATE OR REPLACE FUNCTION garagebooks.lease_income_year()
RETURNS TABLE (lease_id integer, car_id smallint, associate boolean, income_year integer, days_held integer,
               days_in_year integer, pays integer, payments numeric, pretax numeric, admin_fees numeric,
               employee_rate numeric, employee_saving numeric, depreciation numeric, business_income numeric,
               associate_rate numeric, associate_tax numeric, associate_medicare numeric, small_business_offset numeric,
               associate_cost numeric, household_cost numeric)
LANGUAGE plpgsql STABLE
AS $fn$
#variable_conflict use_column
DECLARE
    l record;
    y integer;
    first_day date;
    last_day date;
    value numeric;
    emp record;
    asc_pos record;
    net numeric;
    higher numeric;
BEGIN
    FOR l IN SELECT * FROM garagebooks.lease ORDER BY id LOOP
        value := least(COALESCE(l.depreciation_value, l.car_cost),
                       (SELECT c.amount FROM garagebooks.car_limit c
                        WHERE c.income_year <= garagebooks.income_year(l.start_date) ORDER BY c.income_year DESC LIMIT 1));
        FOR y IN garagebooks.income_year(l.start_date) .. garagebooks.income_year(l.end_date) LOOP
            first_day := greatest(l.start_date, make_date(y, 7, 1));
            last_day := least(l.end_date, make_date(y + 1, 6, 30));
            lease_id := l.id; car_id := l.car_id; associate := l.associate_lease; income_year := y;
            days_held := last_day - first_day + 1;
            days_in_year := make_date(y + 1, 7, 1) - make_date(y, 7, 1);
            SELECT count(*) INTO pays FROM generate_series(0, 2000) n
            CROSS JOIN LATERAL (SELECT CASE l.pay_frequency WHEN 'weekly' THEN l.start_date + 7 * n
                                                            WHEN 'fortnightly' THEN l.start_date + 14 * n
                                                            ELSE (l.start_date + make_interval(months => n))::date END AS d) p
            WHERE p.d BETWEEN first_day AND last_day;
            payments := round((l.pretax_payment + l.posttax_payment) * pays, 2);
            admin_fees := round(l.admin_fee * pays, 2);
            pretax := round((l.pretax_payment + l.admin_fee) * pays, 2);
            SELECT * INTO emp FROM garagebooks.tax_position('employee', y);
            employee_rate := emp.marginal_rate;
            employee_saving := round(pretax * employee_rate / 100, 2);
            depreciation := NULL; business_income := NULL; associate_rate := NULL; associate_tax := NULL;
            associate_medicare := NULL; small_business_offset := NULL; associate_cost := NULL;
            IF l.associate_lease THEN
                IF value IS NOT NULL THEN
                    depreciation := round(value * 0.25 * days_held / days_in_year, 2);
                    value := value - depreciation;
                    business_income := payments - depreciation;
                END IF;
                SELECT * INTO asc_pos FROM garagebooks.tax_position('associate', y);
                IF asc_pos.income IS NOT NULL AND business_income IS NOT NULL THEN
                    net := business_income;
                    higher := asc_pos.income + net;
                    associate_rate := asc_pos.marginal_rate;
                    associate_tax := round(garagebooks.income_tax(asc_pos.table_year, higher)
                                           - garagebooks.income_tax(asc_pos.table_year, asc_pos.income), 2);
                    associate_medicare := round(asc_pos.levy / 100 * net, 2);
                    small_business_offset := CASE WHEN net > 0 AND higher > 0
                        THEN round(least(1000, 0.16 * garagebooks.income_tax(asc_pos.table_year, higher) * net / higher), 2)
                        ELSE 0 END;
                    associate_cost := associate_tax + associate_medicare - small_business_offset;
                END IF;
                household_cost := admin_fees - employee_saving + associate_cost;
            ELSE
                household_cost := payments + admin_fees - employee_saving;
            END IF;
            RETURN NEXT;
        END LOOP;
    END LOOP;
END
$fn$;

-- The after-tax cost of ownership by income year (1 July to 30 June), per car: what the dashboard's "After-tax
-- cost of ownership" row draws, from the same definitions the app uses (expense_after_tax() for the running
-- costs, lease_income_year() for the leases). TeslaMate's charging is not here: the panels add it, as the
-- other after-tax panels do, so this reads no TeslaMate table. A sum that needs an unknown figure is NULL.
--   running_invoices / running_after_tax  running and energy costs entered (purchases left out)
--   lease_payments, admin_fees, employee_saving, depreciation, associate_cost, lease_household  summed over the
--                                          car's leases held in the year (lease_income_year())
--   household_cost = running_after_tax + lease_household
CREATE OR REPLACE FUNCTION garagebooks.after_tax_by_income_year(tz text)
RETURNS TABLE (car_id smallint, income_year integer, running_invoices numeric, running_after_tax numeric,
               lease_payments numeric, admin_fees numeric, employee_saving numeric, depreciation numeric,
               associate_cost numeric, lease_household numeric, household_cost numeric)
LANGUAGE sql STABLE
AS $fn$
    WITH running AS (
        SELECT t.car_id, garagebooks.income_year((t.occurred_at AT TIME ZONE tz)::date) AS y,
               sum(t.amount) AS invoices,
               CASE WHEN bool_or(t.packaged AND t.effective_cost IS NULL) THEN NULL ELSE sum(t.effective_cost) END AS after
        FROM garagebooks.expense_after_tax(tz) t
        JOIN garagebooks.expense e ON e.id = t.expense_id
        JOIN garagebooks.category c ON c.id = e.category_id
        WHERE c.kind IN ('running', 'energy')
        GROUP BY 1, 2),
    leases AS (
        SELECT l.car_id, l.income_year AS y, sum(l.payments) AS payments, sum(l.admin_fees) AS admin,
               CASE WHEN bool_or(l.employee_saving IS NULL) THEN NULL ELSE sum(l.employee_saving) END AS saving,
               sum(l.depreciation) AS depreciation,
               CASE WHEN bool_or(l.associate AND l.associate_cost IS NULL) THEN NULL ELSE sum(l.associate_cost) END AS associate,
               CASE WHEN bool_or(l.household_cost IS NULL) THEN NULL ELSE sum(l.household_cost) END AS household
        FROM garagebooks.lease_income_year() l
        GROUP BY 1, 2)
    SELECT COALESCE(r.car_id, l.car_id), COALESCE(r.y, l.y), COALESCE(r.invoices, 0),
           CASE WHEN r.car_id IS NULL THEN 0 ELSE r.after END,
           COALESCE(l.payments, 0), COALESCE(l.admin, 0), CASE WHEN l.car_id IS NULL THEN 0 ELSE l.saving END,
           l.depreciation, l.associate, CASE WHEN l.car_id IS NULL THEN 0 ELSE l.household END,
           (CASE WHEN r.car_id IS NULL THEN 0 ELSE r.after END) + (CASE WHEN l.car_id IS NULL THEN 0 ELSE l.household END)
    FROM running r FULL JOIN leases l ON l.car_id = r.car_id AND l.y = r.y
$fn$;

-- The lease reminders on the app's own day, and a lease that is followed by the next one.
--   setting.app_tz  APP_TZ, written by the app on every start, so the database judges "today" as the app does: on
--                   the server's own day (UTC, usually) a lease that ended last night was still current for hours.
--                   NULL (nothing has started yet) falls back to the server's day.
-- reminder_due again, with next_lease_start as its last column: the start of another lease of the same car that
-- begins after the current one does and no later than a week after it ends. With one, the lease end is information
-- (the next lease is already loaded): never due soon or overdue, never told, not counted.
ALTER TABLE garagebooks.setting ADD COLUMN IF NOT EXISTS app_tz text
    CHECK (app_tz IS NULL OR (btrim(app_tz) <> '' AND length(app_tz) <= 64));
CREATE OR REPLACE VIEW garagebooks.reminder_due AS
WITH today AS (
    SELECT COALESCE((SELECT (now() AT TIME ZONE s.app_tz)::date FROM garagebooks.setting s
                     WHERE s.id = 1 AND s.app_tz IS NOT NULL), CURRENT_DATE) AS d
), cur AS (
    SELECT DISTINCT ON (l.car_id) l.id, l.car_id, l.start_date, l.end_date, l.rego_expiry, l.rego_renewed_by_provider
    FROM garagebooks.lease l, today WHERE l.end_date >= today.d
    ORDER BY l.car_id, l.start_date, l.id
)
SELECT 'reminder'::text AS kind, r.id AS reminder_id, r.car_id, r.title, r.category_id, r.due_date,
       r.due_km, r.lead_days, r.lead_km, false AS informational, r.service_item_id, NULL::date AS next_lease_start
FROM garagebooks.reminder r
WHERE r.resolved_at IS NULL
UNION ALL
SELECT 'registration', NULL::integer, cur.car_id, 'Registration expires', NULL::smallint, cur.rego_expiry,
       NULL::numeric(9, 1), 30, 0::numeric(9, 1), cur.rego_renewed_by_provider, NULL::smallint, NULL::date
FROM cur
WHERE cur.rego_expiry IS NOT NULL
UNION ALL
SELECT 'lease_end', NULL::integer, cur.car_id, 'Lease ends', NULL::smallint, cur.end_date,
       NULL::numeric(9, 1), 90, 0::numeric(9, 1), nx.start_date IS NOT NULL, NULL::smallint, nx.start_date
FROM cur
LEFT JOIN LATERAL (SELECT min(n.start_date) AS start_date FROM garagebooks.lease n
                   WHERE n.car_id = cur.car_id AND n.id <> cur.id AND n.start_date > cur.start_date
                     AND n.start_date <= cur.end_date + 7) nx ON true;
-- Which cars a category or a service item applies to: 'all', 'electric' (cars that plug in: BEV and PHEV) or
-- 'combustion' (cars with an engine: ICE, PHEV and HEV). The expense form and the import page only offer what
-- applies to the car; expenses saved before keep what they have. The defaults are set once, when the column is
-- added, and not as anyone's edit (kept out of the change history); the owner's later choices are never overwritten.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'garagebooks' AND table_name = 'category' AND column_name = 'applies_to') THEN
        ALTER TABLE garagebooks.category ADD COLUMN applies_to text NOT NULL DEFAULT 'all'
            CHECK (applies_to IN ('all', 'electric', 'combustion'));
        ALTER TABLE garagebooks.category DISABLE TRIGGER log_change;
        UPDATE garagebooks.category SET applies_to = 'combustion' WHERE name = 'Fuel';
        UPDATE garagebooks.category SET applies_to = 'electric' WHERE name = 'Charging';
        ALTER TABLE garagebooks.category ENABLE TRIGGER log_change;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'garagebooks' AND table_name = 'service_item' AND column_name = 'applies_to') THEN
        ALTER TABLE garagebooks.service_item ADD COLUMN applies_to text NOT NULL DEFAULT 'all'
            CHECK (applies_to IN ('all', 'electric', 'combustion'));
        UPDATE garagebooks.service_item SET applies_to = 'combustion'
            WHERE lower(name) IN ('fuel filter', 'oil filter', 'engine oil', 'timing belt', 'air filter', 'engine air filter', 'spark plugs');
    END IF;
END $$;

-- Which wheels a cost was for (FL, FR, RL, RR): on a per-wheel service item an expense had (brakes), and on the
-- expense itself for a per-wheel category (Tyres). per_wheel says which items and categories ask. It is set once,
-- when the column is added, and not as anyone's edit; the owner's later choices are never overwritten.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'garagebooks' AND table_name = 'category' AND column_name = 'per_wheel') THEN
        ALTER TABLE garagebooks.category ADD COLUMN per_wheel boolean NOT NULL DEFAULT false;
        ALTER TABLE garagebooks.category DISABLE TRIGGER log_change;
        UPDATE garagebooks.category SET per_wheel = true WHERE name = 'Tyres';
        ALTER TABLE garagebooks.category ENABLE TRIGGER log_change;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'garagebooks' AND table_name = 'service_item' AND column_name = 'per_wheel') THEN
        ALTER TABLE garagebooks.service_item ADD COLUMN per_wheel boolean NOT NULL DEFAULT false;
        UPDATE garagebooks.service_item SET per_wheel = true WHERE lower(name) IN ('brakes', 'brake pads', 'brake rotors');
    END IF;
END $$;
ALTER TABLE garagebooks.expense ADD COLUMN IF NOT EXISTS wheels text[]
    CHECK (wheels <@ ARRAY['FL', 'FR', 'RL', 'RR']);
ALTER TABLE garagebooks.expense_service_item ADD COLUMN IF NOT EXISTS wheels text[]
    CHECK (wheels <@ ARRAY['FL', 'FR', 'RL', 'RR']);
