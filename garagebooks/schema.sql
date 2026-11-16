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
    ('Cabin air filter',  'running',   50),
    ('Air filter',        'running',   60),
    ('Wheel alignment',   'running',   70),
    ('Tyre rotation',     'running',   80),
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
