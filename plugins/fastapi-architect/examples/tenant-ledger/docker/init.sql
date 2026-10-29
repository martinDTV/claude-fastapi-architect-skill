-- Application role: can log in, is not the owner of any table and cannot bypass RLS.
-- Trust auth inside the container network is for the local demo only.
CREATE ROLE ledger_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD 'ledger_app';
