"""`cpsim aidb-bench`: the simulator as the load and workload generator for EDB AI Accelerator (AIDB) itself.

An optional, operator-run mode (nothing in serve/run/workloads imports it). It drives an AIDB database under sustained load and writes a
JSON and a Markdown report: pipeline throughput and freshness lag, retrieval latency (app vs AIDB backend), concurrent agent
conversations and governance overhead. Results are INDICATIVE: see docs/AIDB-BENCH.md.
"""
