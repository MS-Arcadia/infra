-- Provisions one database and one role per service on a single PostgreSQL instance.
--
-- This is the concrete answer to "should every microservice have its own database?".
-- Yes — but "its own database" does not have to mean "its own server. What
-- Database-per-Service actually requires is that no service can read or write another
-- service's data, and that is enforced here by grants, not by convention:
--
--   * each service owns a separate database with its own role,
--   * each role can connect only to its own database (REVOKE CONNECT from the rest),
--   * PUBLIC gets nothing, so a new database is private by default.
--
-- The result is that wallet_user physically cannot see payment_intents, exactly as it
-- could not if the two lived on different hosts. What is gained is that a laptop, a CI
-- runner and a course demo need one container instead of fourteen. What is given up is
-- independent failure and independent scaling of the storage layer — which is why the
-- Kubernetes manifests promote each database to its own StatefulSet, and why no
-- application code contains a cross-database query that would make that impossible.
--
-- This file runs once, on first boot of an empty data directory. Changing it later has
-- no effect until the volume is recreated: schema changes belong in each service's own
-- versioned migrations, not here.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- Wallet service
-- ---------------------------------------------------------------------------

CREATE ROLE wallet_user WITH LOGIN PASSWORD 'wallet_pass';
COMMENT ON ROLE wallet_user IS 'Owner of arcadia_wallet. Has no access to any other service database.';

CREATE DATABASE arcadia_wallet OWNER wallet_user;
COMMENT ON DATABASE arcadia_wallet IS 'Wallet service: balances, the append-only ledger, gift cards, holds.';

-- ---------------------------------------------------------------------------
-- Payment gateway adapter
-- ---------------------------------------------------------------------------

CREATE ROLE payment_user WITH LOGIN PASSWORD 'payment_pass';
COMMENT ON ROLE payment_user IS 'Owner of arcadia_payment. Has no access to any other service database.';

CREATE DATABASE arcadia_payment OWNER payment_user;
COMMENT ON DATABASE arcadia_payment IS 'Payment adapter: bank payment intents.';

-- ---------------------------------------------------------------------------
-- Databases for the services still to be built.
--
-- Created up front so that a new service needs no privileged database work to start —
-- it just receives a DSN. Each one is as isolated as the two above.
-- ---------------------------------------------------------------------------

CREATE ROLE auth_user      WITH LOGIN PASSWORD 'auth_pass';
CREATE ROLE profile_user   WITH LOGIN PASSWORD 'profile_pass';
CREATE ROLE catalog_user   WITH LOGIN PASSWORD 'catalog_pass';
CREATE ROLE store_user     WITH LOGIN PASSWORD 'store_pass';
CREATE ROLE market_user    WITH LOGIN PASSWORD 'market_pass';
CREATE ROLE review_user    WITH LOGIN PASSWORD 'review_pass';
CREATE ROLE community_user WITH LOGIN PASSWORD 'community_pass';
CREATE ROLE festival_user  WITH LOGIN PASSWORD 'festival_pass';
CREATE ROLE notif_user     WITH LOGIN PASSWORD 'notif_pass';
CREATE ROLE media_user     WITH LOGIN PASSWORD 'media_pass';
CREATE ROLE reco_user      WITH LOGIN PASSWORD 'reco_pass';

CREATE DATABASE arcadia_auth      OWNER auth_user;
CREATE DATABASE arcadia_profile   OWNER profile_user;
CREATE DATABASE arcadia_catalog   OWNER catalog_user;
CREATE DATABASE arcadia_store     OWNER store_user;
CREATE DATABASE arcadia_market    OWNER market_user;
CREATE DATABASE arcadia_review    OWNER review_user;
CREATE DATABASE arcadia_community OWNER community_user;
CREATE DATABASE arcadia_festival  OWNER festival_user;
CREATE DATABASE arcadia_notif     OWNER notif_user;
CREATE DATABASE arcadia_media     OWNER media_user;
CREATE DATABASE arcadia_reco      OWNER reco_user;

-- ---------------------------------------------------------------------------
-- Isolation.
--
-- By default PostgreSQL lets every role connect to every database and create objects in
-- the public schema. Both defaults are revoked: without this, "database per service" is
-- a naming convention rather than a boundary.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    -- Each row pairs a database with the single role permitted to connect to it.
    pairing RECORD;
BEGIN
    FOR pairing IN
        SELECT * FROM (VALUES
            ('arcadia_wallet',    'wallet_user'),
            ('arcadia_payment',   'payment_user'),
            ('arcadia_auth',      'auth_user'),
            ('arcadia_profile',   'profile_user'),
            ('arcadia_catalog',   'catalog_user'),
            ('arcadia_store',     'store_user'),
            ('arcadia_market',    'market_user'),
            ('arcadia_review',    'review_user'),
            ('arcadia_community', 'community_user'),
            ('arcadia_festival',  'festival_user'),
            ('arcadia_notif',     'notif_user'),
            ('arcadia_media',     'media_user'),
            ('arcadia_reco',      'reco_user')
        ) AS t(dbname, rolename)
    LOOP
        -- Nobody may connect except the owner (and the superuser, which is how the
        -- migration runner and psql still work).
        EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', pairing.dbname);
        EXECUTE format('GRANT ALL ON DATABASE %I TO %I', pairing.dbname, pairing.rolename);
    END LOOP;
END
$$;

-- Lock down the public schema inside each service database. Since PostgreSQL 15 the
-- public schema is no longer world-writable, but being explicit means the same file
-- behaves identically on an older major version.
\connect arcadia_wallet
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO wallet_user;

\connect arcadia_payment
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO payment_user;
