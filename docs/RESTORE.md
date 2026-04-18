# Panoptic Disaster Recovery

Restoring Panoptic data from the nightly backups.

Backups are taken by the two scripts in `~/panoptic-store/backup/`, run
from cron (see `~/.crontab` or `crontab -l`):

| Backup | Source | Target on disk | Retention |
|---|---|---|---|
| `pg_dump.sh` | `panoptic` Postgres DB (all tables) | `/data/panoptic-store/backups/panoptic-<stamp>.sql.gz` | 14 days |
| `qdrant_snapshot.sh` | all Qdrant collections | `/data/panoptic-store/qdrant-snapshots/<collection>/<name>.snapshot` | 7 per collection |

Neither backs up the on-disk JPEGs at `/data/panoptic-store/images/`.
That's an explicit choice — they're the largest class of data by far
(order of magnitude larger than everything else combined) and cheap
to re-acquire if a trailer ingests again. If you want them backed up,
wire `~/panoptic-store/backup/images_rsync.sh` to a separate target
disk.

---

## What you can restore from what

| Restore target | Recovers |
|---|---|
| Latest `panoptic-*.sql.gz` | Jobs, summaries, buckets, trailers, images (rows), stream cursors, auth registry |
| Latest per-collection `.snapshot` | Qdrant vectors only (vectors re-generate from Postgres anyway, but this avoids re-embed compute) |

If you lose both Postgres AND Qdrant: restore Postgres first, then
either restore Qdrant snapshots *or* accept the cost of re-running
the embedding workers over `panoptic_images` / `panoptic_summaries`.
Images on disk are preserved so re-embed is purely compute.

---

## Drill — proven 2026-04-18

Both procedures below were rehearsed against the production Postgres
+ Qdrant as of the above date. Counts matched live exactly (141
images / 233 summaries / 885 jobs; 233 Qdrant points in
`panoptic_summaries`). Re-run this drill quarterly — a backup you
haven't restored from is faith-based, not operational.

---

## Postgres restore

### Restore to a **temp DB** (safe — for drills or spot-checks)

```bash
BACKUP=/data/panoptic-store/backups/panoptic-<stamp>.sql.gz

docker exec panoptic-postgres \
    psql -U panoptic -d panoptic \
    -c "CREATE DATABASE panoptic_restore_test"

gunzip -c "$BACKUP" | \
    docker exec -i panoptic-postgres \
    psql -U panoptic -d panoptic_restore_test

# Verify
docker exec panoptic-postgres psql -U panoptic -d panoptic_restore_test -c \
    "SELECT count(*) FROM panoptic_images; SELECT count(*) FROM panoptic_summaries;"

# Clean up
docker exec panoptic-postgres psql -U panoptic -d panoptic -c \
    "DROP DATABASE panoptic_restore_test"
```

### Restore to **live DB** (destructive — real disaster recovery only)

Stop all Panoptic workers first. They write to Postgres continuously
and will either fail or corrupt the restore.

```bash
# 1. Stop workers
tmux kill-session -t panoptic   # or however you're running them

# 2. Drop and recreate the live DB
docker exec panoptic-postgres psql -U panoptic -d postgres -c \
    "DROP DATABASE panoptic; CREATE DATABASE panoptic"

# 3. Restore from dump
BACKUP=/data/panoptic-store/backups/panoptic-<stamp>.sql.gz
gunzip -c "$BACKUP" | \
    docker exec -i panoptic-postgres psql -U panoptic -d panoptic

# 4. Restart workers
cd ~/panoptic && bash scripts/tmux-dev.sh
```

Expect gaps:
- Any webhook pushes between dump time and restore time are lost
- Any in-flight jobs in Redis streams that were already ACKed pre-dump
  but not yet persisted to Postgres are lost
- The trailer's retry queue will re-push anything it didn't get a 2xx
  for (at-least-once semantics work in our favor here)

---

## Qdrant restore

Snapshots live inside the container at `/qdrant/snapshots/<collection>/`
which is mounted from the host at `/data/panoptic-store/qdrant-snapshots/`.

### Restore to a **new collection name** (safe — for drills)

```bash
SNAP=/qdrant/snapshots/panoptic_summaries/<filename>.snapshot

curl -X PUT "http://localhost:6333/collections/panoptic_summaries_restore_test/snapshots/recover" \
    -H "Content-Type: application/json" \
    -d "{\"location\": \"file://$SNAP\"}"

# Verify
curl -s "http://localhost:6333/collections/panoptic_summaries_restore_test" | \
    python3 -c "import json,sys; print(json.load(sys.stdin)['result']['points_count'])"

# Clean up
curl -X DELETE "http://localhost:6333/collections/panoptic_summaries_restore_test"
```

### Restore to the **live collection** (destructive)

Stop workers that write to that collection first:
- `panoptic_summaries` → stop `sum_embed` worker
- `image_caption_vectors` → stop `cap_embed` worker
- `panoptic_image_vectors` → stop `img_embed` worker

```bash
SNAP=/qdrant/snapshots/panoptic_summaries/<filename>.snapshot
COLLECTION=panoptic_summaries

# Recover REPLACES the existing collection's data
curl -X PUT "http://localhost:6333/collections/$COLLECTION/snapshots/recover" \
    -H "Content-Type: application/json" \
    -d "{\"location\": \"file://$SNAP\"}"
```

After restart, workers will re-embed any new rows that accrued since
the snapshot; that's the reconciliation cost.

### If Qdrant itself is gone (fresh container)

1. `docker compose up -d qdrant` in `~/panoptic-store/` — comes up empty.
2. For each collection you want back: recreate via the usual Alembic/init
   path (or just let the workers recreate on first write), then run the
   recover API above against the latest snapshot for that collection.

---

## Re-acquire images via re-embed (if snapshots unavailable)

If you've lost Qdrant completely and don't have snapshots, but
Postgres is intact and JPEGs are on disk:

```bash
cd ~/panoptic && set -a && . ./.env && set +a

# Caption-based image embeddings (image_caption_vectors)
# … no dedicated reembed script exists; deleting the caption-embed
# cursor in Redis and restarting cap_embed worker re-processes all
# caption rows, or use:
.venv/bin/python scripts/reembed_images.py    # (M5 — image_embed path)

# Summary embeddings: same pattern via sum_embed worker
```

The cost is GPU time, not data loss.

---

## Off-box backup (not yet configured)

Right now the backups live on the same disk as the primary data.
That's not a real backup — a disk failure loses both.

Next move (tracked in M6 prep): `rsync` nightly backups to either:
- the DO gateway droplet (good enough for DB dumps; Qdrant snapshots
  are too big to send over WAN regularly)
- a second local box when M6 moves `panoptic-store` off the Spark —
  a `--link-dest` rsync target on the remaining Spark would give us
  hardlinked snapshots for cheap

Do not declare backup "done" until the restore drill runs against
off-box data.
