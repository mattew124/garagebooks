# Backup and restore

GarageBooks keeps its data in the `garagebooks` schema of the TeslaMate database. Two things it
needs are **not** in the database, so a database backup alone is not enough to recover:

| What | Where it lives | How to back it up |
|---|---|---|
| Expenses, categories, settings (including the [notification address](notifications.md), which is a secret), the saved OneDrive sign-in (encrypted) | the `garagebooks` schema | a TeslaMate backup that includes it, or `scripts/backup.sh dump` (below) |
| The encryption key | `GARAGEBOOKS_ENCRYPTION_KEY` in the app's `.env` | copy it to a password manager or another safe place, **never** into the same backup as the database |
| The invoice files | OneDrive, or the [folder on the server](local-folder.md) | OneDrive keeps them; back the folder up with whatever backs up your share |

The database holds only the *paths* of the invoices. Restoring the database without the files gives
you every expense, with an invoice link that doesn't work.

The key is kept apart on purpose: it is what stops a copy of the database from opening your OneDrive.
Without it a restored install works as before, except that OneDrive asks you to enter the client secret
and connect again (see [OneDrive uploads](onedrive.md)). If you only use the invoice folder on the
server, you have no key to keep.

## Dump and restore the schema on its own

`scripts/backup.sh` is in the repository. It runs `pg_dump` and `psql` inside TeslaMate's database
container (like `scripts/provision-db.sh`), so the server needs docker and nothing else. Set
`DB_CONTAINER` if yours isn't `teslamate-database-1`, and `DB_USER` and `DB_NAME` if they aren't the
container's own.

```bash
scripts/backup.sh dump                     # garagebooks-<date>-<time>.sql.gz in this folder
```

```bash
scripts/backup.sh dump /backups/garagebooks.sql.gz   # or choose the file (a name not ending .gz is plain SQL)
```

It dumps only the `garagebooks` schema (`pg_dump --schema=garagebooks`), never TeslaMate's tables, and
only reads from the database. It refuses to overwrite a file, and the file is readable by you alone. The
dump holds your expenses and the encrypted OneDrive sign-in, so keep it private. The key is not in it.

To restore, stop the app first so nothing is writing, then:

```bash
scripts/backup.sh restore /backups/garagebooks.sql.gz
```

Restoring runs as one transaction: either all of it loads or none of it does. It refuses to continue
if the database already has a `garagebooks` schema with tables in it, which is the normal state of an
install that has started once. To throw that schema away and load the backup instead (you lose
anything newer than the backup):

```bash
scripts/backup.sh restore --replace /backups/garagebooks.sql.gz
```

It also refuses a file that isn't a `garagebooks` dump, a whole-database backup, and a file with
`psql` commands in it. Start the app again afterwards. If you run the
[restricted database role](setup.md#the-database) (`SKIP_MIGRATE=1`), run `scripts/provision-db.sh`
once more, because a dump carries no grants.

The schema's tables point at TeslaMate's `cars` table, so the cars must already be in the database, with the
same ids: restore TeslaMate first. Without them the restore fails and changes nothing.

Restore into the same release of GarageBooks, or a newer one: the app upgrades the schema on start, but
it can't downgrade it.

## Check that your TeslaMate backup already has it

The schema is in the same database as TeslaMate's tables, so a backup of that whole database, such as
the `pg_dump` in TeslaMate's own backup instructions, includes it. Check yours rather than assume:

```bash
scripts/backup.sh check /backups/teslamate.sql.gz
```

It reads plain SQL, gzip and custom-format (`pg_dump -Fc`) backups, and changes nothing. It says one of:

- *includes the garagebooks schema, with its data*: exit status 0. You are covered.
- *includes the garagebooks schema, structure only*: the tables are there but not the rows (a schema-only
  dump). Exit status 1.
- *does not include the garagebooks schema*: exit status 1. Typically the backup only dumps TeslaMate's
  own tables, or is older than the first start of GarageBooks. Dump the schema with `backup.sh dump`
  alongside it.

Restoring a whole-database backup brings the schema back with everything else, with TeslaMate's own
steps; don't use `backup.sh restore` for it.

## Restoring onto a new server

1. Set up TeslaMate and its database on the new server, and restore TeslaMate's data if you have it.
2. Put the same `.env` back, including `GARAGEBOOKS_ENCRYPTION_KEY` from where you kept it.
3. **Before the app's first start**, load the schema with `scripts/backup.sh restore <file>`. If the
   app has already started, the schema exists and is not empty, so add `--replace`.
4. Restore the invoice folder to the same path, or reconnect OneDrive. A OneDrive connection works again
   with the same key; without it, connect again. Check that the folder still holds its `.garagebooks`
   marker file, since the app treats a folder without it as an unmounted share.
5. Start the app. Open it and check an expense's invoice link.

## Try a restore before you need one

A backup you have never restored is a guess. Restore into a throwaway database now and then, never the
live one. Create a scratch database, load a TeslaMate backup into it with TeslaMate's own steps (the
schema needs its `cars` table), then load the dump over it:

```bash
DB_NAME=scratch scripts/backup.sh restore --replace /backups/garagebooks.sql.gz
```

Open the result with `psql` to look at it, then drop the scratch database.
