"""Source that loads tables form Airtable.
Supports whitelisting of tables or loading of all tables from a specified base.
"""
import logging
from typing import Any, Dict, Iterable, Iterator, List, Optional

import dlt
import pyairtable
from dlt.sources import DltResource


logger = logging.getLogger(__name__)  # dlt's own logger does not exist before a pipeline is created


@dlt.source
def airtable_source(
    base_id: str = dlt.config.value,
    table_names: Optional[List[str]] = dlt.config.value,
    access_token: str = dlt.secrets.value,
) -> Iterable[DltResource]:
    """
    Represents tables for a single Airtable base.
    Args:
        base_id (str): The id of the base. Obtain it e.g. from the URL in your webbrowser.
            It starts with "app". See https://support.airtable.com/docs/finding-airtable-ids
        table_names (Optional[List[str]]): A list of table IDs or table names to load.
            Unless specified otherwise, all tables in the schema are loaded.
            Names are freely user-defined. IDs start with "tbl". See https://support.airtable.com/docs/finding-airtable-ids
        access_token (str): The personal access token.
            See https://support.airtable.com/docs/creating-and-using-api-keys-and-access-tokens#personal-access-tokens-basic-actions
    """
    api = pyairtable.Api(access_token)
    all_tables_url = api.build_url(f"meta/bases/{base_id}/tables")
    tables = api.request(method="GET", url=all_tables_url).get("tables")
    for t in tables:
        if table_names:
            if t.get("id") in table_names or t.get("name") in table_names:
                yield airtable_resource(api, base_id, t)
        else:
            yield airtable_resource(api, base_id, t)


def _warn_on_colliding_fields(table: Dict[str, Any]) -> None:
    """Dango addition: warn once per table when field names normalise to the same column.

    dlt snake_cases `fields.<name>` into `fields__<name>`, so `Name` and `name` (or `Full Name` and
    `Full-Name`) load into one column and only the last value in each record survives. Loaded values are
    left as they are; the warning tells the user which fields to rename.
    """
    naming = dlt.Schema("airtable_source").naming
    columns: Dict[str, List[str]] = {}
    for field in table["fields"]:
        columns.setdefault(naming.normalize_identifier(field["name"]), []).append(field["name"])
    clashes = []
    for column, names in columns.items():
        if len(names) > 1:
            quoted = ", ".join("'" + n + "'" for n in names)
            clashes.append(f"{quoted} -> fields__{column}")
    if clashes:
        logger.warning(
            f"Airtable table '{table['name']}': these fields load into the same column, and only "
            f"the last value in each record is kept: {'; '.join(clashes)}. "
            "Rename one of them in Airtable to keep every value."
        )


def airtable_resource(
    api: pyairtable.Api,
    base_id: str,
    table: Dict[str, Any],
) -> DltResource:
    """
    Represents a single airtable.
    Args:
        api (pyairtable.Api): The API connection object
        base_id (str): The id of the base. Obtain it e.g. from the URL in your webbrowser.
            It starts with "app". See https://support.airtable.com/docs/finding-airtable-ids
        table (Dict[str, Any]): Metadata about an airtable, does not contain the actual records
    """
    table_name: str = table["name"]
    _warn_on_colliding_fields(table)
    # Dango change: key on the Airtable record id. Records arrive as
    # {"id": ..., "createdTime": ..., "fields": {...}}, so a field's value lands in the
    # flattened column `fields__<name>`. The previous key (the primary field's own name) did not
    # match a loaded column unless that name normalised to a top-level column (`id`,
    # `created_time`), and failed normalisation with UnboundColumnException otherwise.
    # The record id is always present, unique per table and does not rename any column.
    primary_key: List[str] = ["id"]
    air_table = api.table(base_id, table["id"])

    # Table.iterate() supports rich customization options, such as chunk size, fields, cell format, timezone, locale, and view
    # Dango change: pass a generator function, not the generator object. A generator object can be
    # consumed once, so the runner's extract retry failed with SourceExhausted and hid the real error.
    def air_table_records() -> Iterator[List[Any]]:
        yield from air_table.iterate()

    return dlt.resource(
        air_table_records,
        name=table_name,
        primary_key=primary_key,
        write_disposition="replace",
    )
