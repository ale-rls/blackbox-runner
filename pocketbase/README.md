# PocketBase deployment and migrations

This directory is the version-controlled source of truth for the Blackbox
PocketBase schema.

- `Dockerfile` pins the PocketBase release and copies the migrations into the
  production image.
- `pb_migrations/` is committed. PocketBase applies pending migrations
  automatically every time the container starts.
- `/pb/pb_data` contains SQLite data and uploaded files. It must be a persistent
  Coolify volume and must never be copied into the image.

## Coolify deployment

Create a Docker Compose resource from this repository and set its Compose file
to `/docker-compose.pocketbase.yml`. Assign the public PocketBase domain to
container port `8090` and keep the generated `pocketbase-data` volume.

On a fresh deployment, create the first superuser once from the PocketBase
service terminal:

```bash
/pb/pocketbase superuser upsert admin@example.com 'replace-with-a-long-password' --dir=/pb/pb_data
```

Use the same public domain and credentials for `POCKETBASE_URL`,
`POCKETBASE_ADMIN_EMAIL`, and `POCKETBASE_ADMIN_PASSWORD` on the Runner.

## Adopting the existing deployment

1. Back up the current PocketBase `pb_data` volume.
2. Point the service at this repository/Compose file while preserving the
   existing volume mount at `/pb/pb_data`.
3. Deploy. The baseline migration leaves existing collections and records in
   place, creates only missing collections, and records itself as applied.
4. Check `https://<pocketbase-domain>/api/health`, then run the Runner's real
   PocketBase integration test before a show.

Do not run `scripts/pocketbase_bootstrap.py --force` on a database containing
data. That legacy tool remains only for older local setups.

## Adding a schema change

Add a new timestamped JavaScript file under `pb_migrations/`, test it against a
copy of production data, commit it, then redeploy. Never edit a migration that
has already run in production; add a later migration instead.

For local migration testing with a PocketBase binary:

```bash
pocketbase migrate up \
  --dir=/tmp/blackbox-pb-data \
  --migrationsDir="$PWD/pocketbase/pb_migrations"
```
