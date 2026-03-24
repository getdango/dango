# Local Files

**Category:** Local & Custom | **Auth:** None | **Wizard:** Yes

## Setup

1. Place your data files in a directory (CSV, JSON, Parquet, Excel supported)
2. Ensure consistent schema across files of the same type
3. Run `dango source add`, select **Local Files**, and enter the directory path and file pattern

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `directory` | Yes | Directory containing data files |
| `file_pattern` | Yes | File pattern (e.g., `*.csv`, `sales_*.json`) |
| `deduplication_strategy` | No | `latest_only`, `append_only`, `scd_type2`, or `none` |
| `primary_key` | No | Primary key column for deduplication |
| `timestamp_column` | No | Timestamp column for incremental loading |
| `notes` | No | Notes on how to refresh this data |

## Known Limitations

- File format is auto-detected from extension
- All files matching the pattern should have the same schema
- Incremental loading supported via timestamp column
