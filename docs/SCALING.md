# Panoptic — Scaling Reference

Capacity planning. What breaks at what scale, and what to change when.

---

## 1. Per-trailer traffic model

Conservative numbers from `1422725077375` (the first real trailer, after
its first ~10 hours of real traffic):

| Event class | Rate per trailer |
|---|---|
| Bucket notifications | ~96 / day (one per 15 min per active camera × up to 8 cameras, merged) |
| Images (all triggers: baseline + alert + anomaly) | ~240 / day (conservative, varies with anomaly firing) |
| Workers invoked per image | 3 — caption, caption_embed, image_embed |
| Workers invoked per bucket | 2 — bucket_summary, embedding_upsert |

Derived load per trailer:

| Resource | Per trailer / day |
|---|---|
| Job records written to Postgres | ~1,500 |
| Qdrant points written (across 3 collections) | ~600 |
| Disk (images, ~500 KB JPEGs) | ~120 MB |
| Gemma-4-26b calls (captions + summaries) | ~340 |
| Qwen3 embeddings (text + VL) | ~700 |

---

## 2. Projection table

30-day steady state, no deletions yet:

| Trailers | Buckets | Images | Qdrant points (all 3 collections) | Raw image disk | Qdrant disk (vectors + index) | Est. HNSW RAM |
|---|---|---|---|---|---|---|
| **1** | ~3,000 | ~7,000 | ~21,000 | 3.5 GB | 1.2 GB | < 1 GB |
| **10** | 30,000 | 72,000 | 210,000 | 35 GB | 12 GB | 5 GB |
| **50** | 150,000 | 360,000 | 1.0 M | 175 GB | 60 GB | 25 GB |
| **500** | 1.5 M | 3.6 M | **10 M** | **1.75 TB** | **600 GB** | **250 GB** |

Assumptions: 4096-dim cosine vectors × 4 bytes = 16 KB each; HNSW adds
~40% overhead; images ≈500 KB JPEG.

---

## 3. What breaks at what scale

### 3.1 Qdrant file descriptors

**Standard operational config. Not a scaling concern.**

Qdrant uses RocksDB, which holds SST + log files open per segment. Every
RocksDB-backed system (Qdrant, Kafka, Cassandra, LevelDB, anything LSM)
needs nofile bumped past the 1024 Linux default. Set once:

```yaml
# panoptic-store/docker-compose.yml (current config)
qdrant:
  ulimits:
    nofile: { soft: 65536, hard: 65536 }
```

**Estimated FD usage at scale** (8 FDs/segment × total segments + ~1000 socket/internal):

| Trailers | ~Segments (3 collections) | Est. FDs |
|---|---|---|
| 1 | 45 | 1,360 |
| 50 | ~200 | 2,600 |
| 500 | ~600 | 5,800 |

65,536 leaves 10× headroom even at 500 trailers. No ongoing tuning needed.

### 3.2 Qdrant RAM for HNSW indexes

**Real scaling concern. Arrives around 50 trailers.**

Default Qdrant keeps HNSW indexes in RAM for fast queries. Our DGX Spark
has 128 GB unified memory (shared with GPU services that already use
~53 GB). So RAM budget for Qdrant today is ~50-60 GB.

From the table:
- 50 trailers / 30 days = **25 GB HNSW** → fits comfortably
- 500 trailers / 30 days = **250 GB HNSW** → does NOT fit in any single Spark

**Mitigation at ~50 trailers:** enable Qdrant's `on_disk_payload: true`
(already default) and, when a collection crosses ~10 GB index, configure
`hnsw_config.on_disk: true` for that collection. ~2× query latency hit,
still well under a second, and RAM becomes proportional to hot data only.

**Beyond 50 trailers:** plan for Qdrant cluster mode.

### 3.3 Qdrant cluster / sharding

**Real scaling concern. Needed around 100+ trailers or 5M+ points/collection.**

Qdrant supports multi-node clustering out of the box. At that scale we
replace the single-container deploy with:

- 3+ Qdrant nodes with replication factor 2
- Collections sharded (e.g. 8 shards per collection)
- Shard assignment based on `serial_number` so each trailer's data lands
  on one shard (locality + smaller index per shard)

Operationally this is a whole different chapter, not a config tweak.
Flag when we hit ~5M points in any single collection.

### 3.4 Disk — `/data/panoptic-store/`

**Needs planning at ~50 trailers, urgent at ~100.**

At 500 trailers × 30 days, we're at **~1.75 TB of raw images alone**,
plus ~600 GB of Qdrant storage. That's ~2.5 TB/month if we retain
everything.

Current Spark disk: 3.7 TB NVMe total. Would fill in ~6 weeks at 500
trailers with no pruning.

**Mitigations (in order of cheapness):**

1. **Image retention policy.** Keep baseline images for N days; keep
   alert/anomaly indefinitely. `scripts/prune_images.py` (not built yet).
2. **Tiered storage.** Move cold images to cheaper bulk storage (NAS or
   object store) while keeping hot images local. `panoptic_images.storage_path`
   is already a string, so a URI scheme (`file://`, `s3://`) is a small
   change.
3. **Move the whole data tier off the Spark.** M6 from `NEXT_STEPS.md`.
   The dedicated box can have 10-40 TB of bulk storage.

### 3.5 Postgres row counts

**Not a concern. Postgres handles billions.**

At 500 trailers / 30 days: 1.5 M bucket rows, 3.6 M image rows, 7.5 M
job rows. Postgres 16 on modest hardware is fine here. Index-heavy work
like `panoptic_jobs` state transitions: all indexed; O(log n) lookups.

Long-term: `panoptic_job_history` is append-only and grows unbounded.
Adding a partitioned-by-day or periodic archive is an M6-era task.

### 3.6 vLLM / Gemma throughput

**Starts to matter around 30-50 trailers.**

A single Gemma call is ~1-2 s on Spark. Per-trailer rate is ~340 calls/day
≈ 1 call every 4 minutes. Workers are single-consumer (one vLLM at a
time), so at steady state each worker processes ~1 call/sec.

- 1 trailer → 0.004 calls/s → not saturated
- 50 trailers → 0.2 calls/s → not saturated
- 500 trailers → 2 calls/s → **exceeds single-vLLM throughput**

Mitigations as we approach saturation: vLLM supports `--tensor-parallel-size
> 1` for larger models; or run a second vLLM instance behind a load
balancer. A GPU cluster is where this goes long-term.

### 3.7 Qdrant / retrieval HTTP throughput

**Not a concern at any scale we're targeting.**

Qdrant handles thousands of queries per second on modest hardware.
Panoptic's query rate (search traffic + internal upserts) tops out in
the tens of QPS even at 500 trailers. Non-issue.

### 3.8 Redis streams

**Not a concern.** Redis 7 handles 100k+ ops/sec. Our steady-state rate
is << 100/sec. Caps on stream length (`_STREAM_MAXLEN = 50,000`) prevent
unbounded growth.

### 3.9 Network / ingress

At 500 trailers pushing:
- ~500 × 240 images × 500 KB / 86400 sec = **~700 KB/s average** inbound
- Bursts up to 10-50× during alert events = ~10-35 MB/s

Fine on any commodity pipe. The FRP tunnel → Caddy on DO droplet works
at this rate, though we'd want to monitor it.

---

## 4. Milestone-by-milestone scaling checklist

### Next 10 trailers (M3 → early M6)

- [x] Qdrant `ulimit.nofile = 65536` (done 2026-04-18)
- [ ] Disk monitoring alert when `/data` crosses 70%
- [ ] Image retention policy draft
- [ ] Backup cadence nailed down (pg_dump, Qdrant snapshots)

### 10-50 trailers (M6 time)

- [ ] Move `panoptic-store` to dedicated hardware with bulk storage
- [ ] Enable Qdrant `hnsw_config.on_disk: true` for image collections
- [ ] Switch panoptic images to tiered storage (hot on NVMe, cold on bulk)
- [ ] First real backup restore drill
- [ ] Monitor Gemma saturation; plan second vLLM if needed

### 50-500 trailers (future)

- [ ] Qdrant cluster mode (3+ nodes, replication + sharding by serial)
- [ ] Multiple vLLM instances behind a load balancer
- [ ] Postgres partition `panoptic_job_history` + archive job_history > 90 days
- [ ] Dedicated search replica for Qdrant (read-only)
- [ ] Fleet-wide alerting + anomaly detection
- [ ] Image archival to object store (S3 or MinIO)

### 500+ trailers

At that point Panoptic is in a different architecture class. Probably:
- Qdrant Cloud or a self-hosted cluster of 5+ nodes
- Kafka or similar in place of single-node Redis streams
- Multiple Spark compute tiers horizontally scaled
- Regional deployment with per-region data locality

Not a single-system-design problem anymore — a platform-design problem.

---

## 5. The FD question, put to bed

Raising `ulimit -n` looks weird the first time you see it, but it's the
**one-time standard operational step** for every RocksDB, LevelDB, LSM-tree,
or index-heavy database. Each of these systems below asks for ≥10k:

| System | Typical ulimit -n |
|---|---|
| Elasticsearch / OpenSearch | 65,536 |
| MongoDB | 64,000 |
| Cassandra | 100,000 |
| Kafka | 100,000+ |
| Redis (production) | 10,000+ |
| Qdrant | ≥10,000 (per their docs) |

The 1024 default dates from 1980s Unix and has been inappropriate for
database workloads for ~25 years. Set once in the compose file, forget
about it, it scales linearly with a resource the kernel tracks for free.

What IS a real scaling concern is **segments × RAM** (the HNSW index
size) — addressed by on-disk HNSW and eventual clustering. FDs themselves
are accounting, not cost.

---

## 6. Monitoring for early warning

Things to watch as we scale. Today most are eyeballed via
`scripts/dashboard.sh`. At 10+ trailers they need automated alerting.

| Signal | Threshold of concern | Fix |
|---|---|---|
| `/data/panoptic-store` disk used | > 70% | retention or tiering |
| Qdrant process FD count | > 40,000 | investigate segment count |
| Qdrant segment count per collection | > 200 | tune optimizer; consider compaction |
| Qdrant query latency p99 | > 500 ms | on-disk HNSW or cluster mode |
| vLLM request latency p99 | > 5 s | scale out vLLM |
| Bucket → summary lag | > 5 min | scale summary worker or vLLM |
| Job `retry_wait` count | > 10 | check dep health, DLQ is coming |
| DLQ depth | > 0 sustained | investigate + replay |
