-- GarageBooks schema. Idempotent: safe to re-run (scripts/provision-db.sh
-- does so on every deploy). Lives in its own `garagebooks` schema inside the
-- TeslaMate database so Grafana can join it to TeslaMate's own tables
-- (cars, positions, charging_processes) without a second datasource.
--
-- Run as the database superuser/owner, NOT as the app role: it creates the
-- schema, the foreign key onto public.cars and the reporting view.

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
    kind      text NOT NULL DEFAULT 'running' CHECK (kind IN ('purchase', 'running')),
    sort_order integer NOT NULL DEFAULT 100
);

INSERT INTO garagebooks.category (name, kind, sort_order) VALUES
    ('Initial purchase',  'purchase',  10),
    ('Insurance',         'running',   20),
    ('Registration',      'running',   30),
    ('Tyres',             'running',   40),
    ('Cabin air filter',  'running',   50),
    ('Air filter',        'running',   60),
    ('Wheel alignment',   'running',   70),
    ('Tyre rotation',     'running',   80)
ON CONFLICT (name) DO NOTHING;

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
    car_id smallint NOT NULL REFERENCES public.cars (id) ON DELETE CASCADE,
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
    car_id       smallint NOT NULL REFERENCES public.cars (id),
    category_id  smallint NOT NULL REFERENCES garagebooks.category (id),
    -- Moment the cost was incurred (timestamptz; the UI enters it in APP_TZ).
    occurred_at  timestamptz NOT NULL,
    -- Negative for a rebate or refund (it reduces the totals).
    amount       numeric(12, 2) NOT NULL,
    -- Odometer in km at occurred_at. Optional: costs from before TeslaMate
    -- started tracking the car have no position to look it up from.
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
    car_id      smallint PRIMARY KEY REFERENCES public.cars (id) ON DELETE CASCADE,
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

-- One row per car: everything the TCO dashboard's headline panels need, so the
-- definitions (what counts as "start odometer", what is charging) live in one
-- place instead of being copy-pasted across panels.
--   start_km    odometer when you bought it (purchase expense), else the first
--               odometer TeslaMate ever recorded
--   owned_since purchase date, else first TeslaMate position
CREATE OR REPLACE VIEW garagebooks.ownership AS
SELECT
    car.id                                         AS car_id,
    car.name                                       AS car_name,
    COALESCE(ex.purchase_total, 0)                 AS purchase_total,
    COALESCE(ex.running_total, 0)                  AS running_total,
    COALESCE(ch.charging_total, 0)                 AS charging_total,
    COALESCE(ex.purchase_total, 0) + COALESCE(ex.running_total, 0)
        + COALESCE(ch.charging_total, 0)           AS tco,
    COALESCE(ex.purchase_km, pos.first_km)         AS start_km,
    pos.last_km                                    AS current_km,
    -- GREATEST() ignores NULLs, so guard explicitly: no positions => no distance, not 0.
    CASE WHEN pos.last_km IS NULL THEN NULL
         ELSE GREATEST(pos.last_km - COALESCE(ex.purchase_km, pos.first_km), 0)
    END                                            AS distance_km,
    COALESCE(ex.purchase_at, pos.first_at AT TIME ZONE 'UTC') AS owned_since
FROM public.cars car
LEFT JOIN LATERAL (
    SELECT
        SUM(e.amount) FILTER (WHERE c.kind = 'purchase') AS purchase_total,
        SUM(e.amount) FILTER (WHERE c.kind = 'running')  AS running_total,
        (ARRAY_AGG(e.odometer_km ORDER BY e.occurred_at)
            FILTER (WHERE c.kind = 'purchase' AND e.odometer_km IS NOT NULL))[1] AS purchase_km,
        MIN(e.occurred_at) FILTER (WHERE c.kind = 'purchase') AS purchase_at
    FROM garagebooks.expense e
    JOIN garagebooks.category c ON c.id = e.category_id
    WHERE e.car_id = car.id
) ex ON true
LEFT JOIN LATERAL (
    SELECT SUM(cp.cost) AS charging_total
    FROM public.charging_processes cp
    WHERE cp.car_id = car.id
) ch ON true
LEFT JOIN LATERAL (
    SELECT
        MIN(p.odometer) AS first_km,
        MAX(p.odometer) AS last_km,
        MIN(p.date)     AS first_at
    FROM public.positions p
    WHERE p.car_id = car.id
) pos ON true;

-- Rebates and refunds are negative amounts. Databases made before that allowed them still carry
-- the old CHECK (amount >= 0); drop it (a no-op once gone).
ALTER TABLE garagebooks.expense DROP CONSTRAINT IF EXISTS expense_amount_check;
