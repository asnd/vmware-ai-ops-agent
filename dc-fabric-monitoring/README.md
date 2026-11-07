# dc-fabric-monitoring

Мониторинг алармов и uplink'ов DC-коммутаторов (Clos-фабрики). Не связан с
остальным содержимым репозитория.

* [`ARCHITECTURE.md`](ARCHITECTURE.md) — анализ текущей схемы, три варианта
  развития (A: stateful-скрипт, B: Prometheus + snmp_exporter + Alertmanager +
  Grafana, C: traps/syslog + gNMI) и поэтапная рекомендация.
* `alarm_delta.py` — прототип движка дельты (только stdlib): NEW / CLEARED /
  CHANGED / UNREACHABLE, confirm/clear-after против флапов, группировка по
  фабрикам, Teams (Workflows webhook, Adaptive Card) и e-mail.
* `run_cycle.sh` — пример цикла для cron: опрос → дельта → коммит только при
  изменении.

```bash
# пробный прогон без отправки и без сохранения state
python3 alarm_delta.py --snapshot-dir snapshots/ --inventory inventory.yaml --dry-run

# тесты
python3 -m pytest dc-fabric-monitoring/tests -o addopts=""
```
