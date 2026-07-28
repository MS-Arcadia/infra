-- One database and one role per service, on a single PostgreSQL instance.
--
-- This is the concrete answer to "should every microservice have its own database?".
-- Yes — but "its own database" need not mean "its own server". What
-- Database-per-Service actually requires is that no service can read or write another
-- service's data, and that is enforced here by grants rather than by convention:
-- CONNECT is revoked from PUBLIC, so wallet_user physically cannot reach
-- payment_intents, exactly as it could not if the two lived on different hosts.
--
-- What is gained is one container instead of one per service. What is given up is
-- independent failure and scaling of the storage layer. That is repaid by each service
-- knowing nothing but its own DATABASE_URL: promoting one database to its own instance
-- is a change to one connection string, and no application query joins across databases,
-- so nothing prevents it.
--
-- Only the services that exist get a database. Adding one later is four lines here
-- plus recreating the volume (`make nuke`), because this script runs once, on first boot
-- of an empty data directory.

\set ON_ERROR_STOP on

CREATE ROLE wallet_user WITH LOGIN PASSWORD 'wallet_pass';
CREATE DATABASE arcadia_wallet OWNER wallet_user;

CREATE ROLE payment_user WITH LOGIN PASSWORD 'payment_pass';
CREATE DATABASE arcadia_payment OWNER payment_user;

CREATE ROLE catalog_user WITH LOGIN PASSWORD 'catalog_pass';
CREATE DATABASE arcadia_catalog OWNER catalog_user;

CREATE ROLE order_user WITH LOGIN PASSWORD 'order_pass';
CREATE DATABASE arcadia_order OWNER order_user;

CREATE ROLE media_user WITH LOGIN PASSWORD 'media_pass';
CREATE DATABASE arcadia_media OWNER media_user;

-- Auth and Profile share a deployment and therefore a database. They are separate bounded
-- contexts and talk to each other only through events, so they could be split later — but giving
-- two contexts in one process two databases would mean two connection pools and a distributed
-- transaction between them for no boundary anyone is enforcing.
CREATE ROLE auth_user WITH LOGIN PASSWORD 'auth_pass';
CREATE DATABASE arcadia_auth OWNER auth_user;

-- By default PostgreSQL lets every role connect to every database. Revoking that is
-- what turns "a database per service" from a naming convention into a boundary.
REVOKE ALL ON DATABASE arcadia_wallet  FROM PUBLIC;
REVOKE ALL ON DATABASE arcadia_payment FROM PUBLIC;
REVOKE ALL ON DATABASE arcadia_catalog FROM PUBLIC;
REVOKE ALL ON DATABASE arcadia_order   FROM PUBLIC;
REVOKE ALL ON DATABASE arcadia_media   FROM PUBLIC;
REVOKE ALL ON DATABASE arcadia_auth    FROM PUBLIC;
GRANT  ALL ON DATABASE arcadia_wallet  TO wallet_user;
GRANT  ALL ON DATABASE arcadia_payment TO payment_user;
GRANT  ALL ON DATABASE arcadia_catalog TO catalog_user;
GRANT  ALL ON DATABASE arcadia_order   TO order_user;
GRANT  ALL ON DATABASE arcadia_media   TO media_user;
GRANT  ALL ON DATABASE arcadia_auth    TO auth_user;

\connect arcadia_wallet
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  ALL ON SCHEMA public TO wallet_user;

\connect arcadia_payment
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  ALL ON SCHEMA public TO payment_user;

\connect arcadia_catalog
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  ALL ON SCHEMA public TO catalog_user;

\connect arcadia_order
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  ALL ON SCHEMA public TO order_user;

\connect arcadia_media
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  ALL ON SCHEMA public TO media_user;

\connect arcadia_auth
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  ALL ON SCHEMA public TO auth_user;
