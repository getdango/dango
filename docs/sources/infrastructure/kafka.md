# Apache Kafka

**Category:** Streaming | **Auth:** Config JSON | **Wizard:** Yes

## Setup

1. Get your Kafka broker addresses and authentication credentials
2. Create a JSON config with your credentials:
   ```json
   {
     "bootstrap.servers": "broker1:9092,broker2:9092",
     "security.protocol": "SASL_SSL",
     "sasl.mechanism": "PLAIN",
     "sasl.username": "your-key",
     "sasl.password": "your-secret"
   }
   ```
3. Run `dango source add`, select **Apache Kafka**, and enter topics and credentials

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `topics` | Yes | Kafka topics to consume (comma-separated) |
| `credentials_env` | Yes | Kafka connection config as JSON (env var: `KAFKA_CREDENTIALS`) |
| `batch_size` | No | Messages per request (default: 3000) |
| `batch_timeout` | No | Batch timeout in seconds (default: 3) |
| `start_from` | No | Start timestamp (empty = from beginning) |

**Pip dependency:** `confluent-kafka` (installed automatically)

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Incremental loading supported via consumer offsets
