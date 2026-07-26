# Dashboard and scrape config

`hippo-overview.json` is a Grafana dashboard, provisioned rather than
hand-built so it is reviewable in a diff. Import it, or drop this directory in
as a provisioning path:

```yaml
# /etc/grafana/provisioning/dashboards/hippo.yaml
apiVersion: 1
providers:
  - name: hippo
    folder: Hippo
    type: file
    options:
      path: /var/lib/grafana/dashboards/hippo
```

Prometheus needs to scrape the API:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: hippo
    scrape_interval: 30s
    static_configs:
      - targets: ["app:8000"]
```

## What the panels are ordered by

Not by subsystem. By what an operator looks at first when something is wrong:

1. **The security story.** ACL propagation age against the five-minute promise,
   and sync lag per stream. ARCHITECTURE §11 puts propagation here rather than
   under performance, because a stale permission is a wrong answer rather than
   a slow one.
2. **Is anything broken.** Failing streams, the dead letter, queue depth, and
   the age of the oldest runnable job — which is the clearest single signal
   that no worker is running.
3. **The write path.** Actions by status, and the age of the oldest action
   caught mid-write. That last one rises only when an executor died between
   capturing an inverse and finishing.
4. **What it costs.** Token spend, read from the trace log so it survives a
   restart, split by input and output because they are priced differently.
5. **Whether to believe any of it.** Every panel above comes from a query run
   at scrape time, so `hippo_metrics_scrape_failed` is the panel that says
   whether the others are current.

## The alerts worth having

Thresholds are deliberately not baked into the JSON — they depend on how often
your connectors are allowed to run. These are the four conditions worth paging
on, in the order they matter:

| Condition | Why |
|---|---|
| `hippo_acl_staleness_seconds > 300` | A revoked permission has outlived the promise. Nothing else in the system notices. |
| `hippo_jobs_queue_depth{status="dead"} > 0` | Work has silently stopped happening. |
| `hippo_actions_in_flight_seconds > 900` | An executor died mid-write and nobody knows whether it landed. |
| `hippo_sync_stream_failed == 1` for 15m | The upstream cause of the first row. |
