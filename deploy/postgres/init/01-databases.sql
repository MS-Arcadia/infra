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
-- independent failure and scaling of the storage layer — which is why the Kubernetes
-- manifests point each service at its own hostname, making the promotion to a dedicated
-- instance a change to one connection string. No application query joins across
-- databases, so nothing prevents it.
--
-- Only the two services that exist get a database. Adding one later is four lines here
-- plus recreating the volume (`make nuke`), because this script runs once, on first boot
-- of an empty data directory.

\set ON_ERROR_STOP on

CREATE ROLE wallet_user WITH LOGIN PASSWORD 'wallet_pass';
CREATE DATABASE arcadia_wallet OWNER wallet_user;

CREATE ROLE payment_user WITH LOGIN PASSWORD 'payment_pass';
CREATE DATABASE arcadia_payment OWNER payment_user;

-- By default PostgreSQL lets every role connect to every database. Revoking that is
-- what turns "a database per service" from a naming convention into a boundary.
REVOKE ALL ON DATABASE arcadia_wallet  FROM PUBLIC;
REVOKE ALL ON DATABASE arcadia_payment FROM PUBLIC;
GRANT  ALL ON DATABASE arcadia_wallet  TO wallet_user;
GRANT  ALL ON DATABASE arcadia_payment TO payment_user;

\connect arcadia_wallet
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  ALL ON SCHEMA public TO wallet_user;

\connect arcadia_payment
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  ALL ON SCHEMA public TO payment_user;
