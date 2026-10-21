# mysqlq

Read-only MySQL queries by named profile, optionally through a SOCKS5 proxy.

```sh
mysqlq --profile uat "SELECT * FROM v1_jobs WHERE id = 1"
mysqlq --profile uat -d fms_tasking -E "SELECT ..."
echo "SHOW TABLES" | mysqlq --profile uat
```

A profile identifies a server, not a database, so one profile reaches every database the user can read. Pick one per
run with `-d`, or qualify table names (`SELECT * FROM fms_tasking.v1_jobs`).

Only `SELECT`, `SHOW`, `DESCRIBE`/`DESC`, `EXPLAIN` and `WITH` statements are accepted, and the session is set
read-only on the server. Requires Python 3.8+, a MySQL 5.7.8+ server, and a recent MySQL `mysql` client on `PATH`.
The MariaDB client (what Debian/Ubuntu install as `mysql`) is not supported: it has no `--commands` option.

## Limits

Each `SELECT` returns at most 1000 rows and is aborted after 30 seconds. The server enforces both limits through
`sql_select_limit` and `max_execution_time`, so a `LIMIT` in your query still wins. Change them per run, using `0` to
remove a limit:

```sh
mysqlq --profile uat --max-rows 0 --timeout 300 "SELECT * FROM v1_jobs"
```

## Credentials

mysqlq reads profiles from **`~/.mysqlq/credentials`**. To use a different file, set `MYSQLQ_CREDENTIALS` to its
path.

Start from the example in this repo:

```sh
mkdir -p ~/.mysqlq
cp credentials.example ~/.mysqlq/credentials
chmod 700 ~/.mysqlq
chmod 600 ~/.mysqlq/credentials
```

Then edit `~/.mysqlq/credentials` and fill in your real hosts, users and passwords. mysqlq refuses to run if the
file is owned by someone else or readable by group or others, so keep it at `chmod 600`. Keep the real file outside this
repo, and never commit it. Only `credentials.example` belongs in git.

Each `[section]` is a profile:

| Key           | Required | Description                                       |
|---------------|----------|---------------------------------------------------|
| `host`        | yes      | MySQL host                                        |
| `user`        | yes      | MySQL user (ideally one with read-only grants)    |
| `password`    | yes      | MySQL password                                    |
| `port`        | no       | Defaults to `3306`                                |
| `socks_proxy` | no       | `host:port` of a SOCKS5 proxy (no authentication) |

Inline comments must start with `;`.

Instead of passing `--profile` every time, you can set a default:

```sh
export MYSQLQ_PROFILE=uat
```

## Tests

```sh
python3 -m unittest -v
```

The tests use a fake `mysql` client, so they need no database.
