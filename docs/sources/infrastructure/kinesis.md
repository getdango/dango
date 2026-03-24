# Amazon Kinesis

**Category:** Streaming | **Auth:** AWS Credentials | **Wizard:** Yes

## Setup

1. Create an IAM user with Kinesis read permissions
2. Generate access keys
3. Create a JSON config:
   ```json
   {
     "aws_access_key_id": "AKIA...",
     "aws_secret_access_key": "...",
     "region_name": "us-east-1"
   }
   ```
4. Run `dango source add`, select **Amazon Kinesis**, and enter stream name and credentials

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `stream_name` | Yes | Kinesis stream name |
| `credentials_env` | Yes | AWS credentials as JSON (env var: `AWS_CREDENTIALS`) |
| `initial_at_timestamp` | No | Start timestamp (default: `0` = from beginning) |
| `chunk_size` | No | Records per request (default: 1000) |
| `parse_json` | No | Parse messages as JSON (default: true) |

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Incremental loading supported via shard iterators
