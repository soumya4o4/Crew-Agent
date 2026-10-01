-- STEP 1 of 2 (run this first, once): removes the old, unused Alembic tables.
-- They were checked and are empty. Then run
-- supabase/migrations/20261001000000_travel_concierge_schema.sql as STEP 2.

drop table if exists tool_calls, flight_selections, flight_searches, bookings, tasks,
                     messages, conversations, users, alembic_version cascade;
