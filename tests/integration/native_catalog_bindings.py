"""tests/integration/native_catalog_bindings.py

Minimal exact-pin ordinary-role catalog smoke; import/collection performs no native IO.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# Development evidence only. Product runtime profile remains candidate/refused.
ARTIFACTS = [
    (
        "duckdb-2.0.0-cp312-abi3-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
        "https://files.pythonhosted.org/packages/cc/33/01ad2535ee897db2bad70cbc800655167c53afd0b955eed84f0759c2f417/duckdb-2.0.0-cp312-abi3-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
        "ab51ec57d329d6f3df37c0287b55d71c3fc07bb54b61d1afed09104f953b1431",
        24371005,
    ),
    (
        "typing_extensions-4.16.0-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/typing_extensions-4.16.0-py3-none-any.whl",
        "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
        45571,
    ),
    (
        "cpython-3.14.10+20261026-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.14.10%2B20261026-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "1b38024924eb7f4cf0cab45d7b871d1f1991354190fccd2625ac7529a43786e2",
        76564514,
    ),
    (
        "pyyaml-6.0.3-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
        "https://files.pythonhosted.org/packages/88/f9/16491d7ed2a919954993e48aa941b200f38040928474c9e85ea9e64222c3/pyyaml-6.0.3-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
        "c458b6d084f9b935061bc36216e8a69a7e293a2f1e68bf956dcd9e6cbcd143f5",
        794175,
    ),
    (
        "pydantic-2.12.4-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/82/2f/e68750da9b04856e2a7ec56fc6f034a5a79775e9b9a81882252789873798/pydantic-2.12.4-py3-none-any.whl",
        "92d3d202a745d46f9be6df459ac5a064fdaa3c1c4cd8adcfa332ccf3c05f871e",
        463400,
    ),
    (
        "pydantic_core-2.41.5-cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "https://files.pythonhosted.org/packages/4c/d2/ef2074dc020dd6e109611a8be4449b98cd25e1b9b8a303c2f0fca2f2bcf7/pydantic_core-2.41.5-cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "22f0fb8c1c583a3b6f24df2470833b40207e907b90c928cc8d3594b76f874375",
        2064877,
    ),
    (
        "annotated_types-0.7.0-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/78/b6/6307fbef88d9b5ee7421e68d78a9f162e0da4900bc5f5793f6d3d0e34fb8/annotated_types-0.7.0-py3-none-any.whl",
        "1f02e8b43a8fbbc3f3e0d4f0f4bfc8131bcb4eebe8849b8e5c773f3a1c582a53",
        13643,
    ),
    (
        "typing_inspection-0.4.2-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/dc/9b/47798a6c91d8bdb567fe2698fe81e0c6b7cb7ef4d13da4114b41d239f65d/typing_inspection-0.4.2-py3-none-any.whl",
        "4ed1cacbdc298c220f1bd249ed5287caa16f34d44ef4e9c3d0cbad5b521545e7",
        14611,
    ),
    (
        "keyring-24.3.1-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/7c/23/d557507915181687e4a613e1c8a01583fd6d7cb7590e1f039e357fe3b304/keyring-24.3.1-py3-none-any.whl",
        "df38a4d7419a6a60fea5cef1e45a948a3e8430dd12ad88b0f423c5c143906218",
        38092,
    ),
    (
        "jaraco.classes-3.4.0-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/7f/66/b15ce62552d84bbfcec9a4873ab79d993a1dd4edb922cbfccae192bd5b5f/jaraco.classes-3.4.0-py3-none-any.whl",
        "f662826b6bed8cace05e7ff873ce0f9283b5c924470fe664fff1c2f00f581790",
        6777,
    ),
    (
        "more_itertools-10.8.0-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/a4/8e/469e5a4a2f5855992e425f3cb33804cc07bf18d48f2db061aec61ce50270/more_itertools-10.8.0-py3-none-any.whl",
        "52d4362373dcf7c52546bc4af9a86ee7c4579df9a8dc268be0a2f949d376cc9b",
        69667,
    ),
    (
        "secretstorage-3.4.0-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/91/ff/2e2eed29e02c14a5cb6c57f09b2d5b40e65d6cc71f45b52e0be295ccbc2f/secretstorage-3.4.0-py3-none-any.whl",
        "0e3b6265c2c63509fb7415717607e4b2c9ab767b7f344a57473b779ca13bd02e",
        15272,
    ),
    (
        "jeepney-0.9.0-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/b2/a3/e137168c9c44d18eff0376253da9f1e9234d0239e0ee230d2fee6cea8e55/jeepney-0.9.0-py3-none-any.whl",
        "97e5714520c16fc0a45695e5365a2e11b81ea79bba796e26f9f1d178cb182683",
        49010,
    ),
    (
        "cryptography-46.0.3-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
        "https://files.pythonhosted.org/packages/5c/49/498c86566a1d80e978b42f0d702795f69887005548c041636df6ae1ca64c/cryptography-46.0.3-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
        "01ca9ff2885f3acc98c29f1860552e37f6d7c7d013d7334ff2a9de43a449315d",
        4450807,
    ),
    (
        "cffi-2.0.0-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
        "https://files.pythonhosted.org/packages/47/d9/d83e293854571c877a92da46fdec39158f8d7e68da75bf73581225d28e90/cffi-2.0.0-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
        "afb8db5439b81cf9c9d0c80404b60c3cc9c3add93e114dcae767f1477cb53775",
        219244,
    ),
    (
        "pycparser-2.23-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/a0/e3/59cd50310fc9b59512193629e1984c1f95e5c8ae6e5d8c69532ccc65a7fe/pycparser-2.23-py3-none-any.whl",
        "e5c6e8d3fbad53479cab09ac03729e0a9faf2bee3db8208a550daf5af81a5934",
        118140,
    ),
]
EXTENSIONS = {
    "ducklake": {
        "url": "https://extensions.duckdb.org/v2.0.0/linux_amd64/ducklake.duckdb_extension.gz",
        "sha256": "ff1c730a5d5a7cb14ed9547ba15c259c24805eb4cf12c17727c2a4faffe1ada7",
        "expanded_sha256": "4b497cf95d768004d34a97e7098d33f687e994dc523945948c321af25061b39b",
        "license": "MIT",
        "revision": "417a9117f5",
        "compressed": 17217388,
        "expanded_upper": 54882304,
    },
    "postgres_scanner": {
        "url": "https://extensions.duckdb.org/v2.0.0/linux_amd64/postgres_scanner.duckdb_extension.gz",
        "sha256": "54b3d751cafc9ffa439886b1607d0ee758dab4dc070f0171e779b95f2f3e554f",
        "expanded_sha256": "39e661c5cfb678c9472f5acd88235f84f3ef4126e7444c7c8a01214d9b291601",
        "license": "MIT",
        "revision": "c0f5a3c0c4",
        "compressed": 18910802,
        "expanded_upper": 59224064,
    },
}
IMAGES = {
    "node": {
        "ref": "node@sha256:e1582112fb9f152ed38eefcd712efc93fee1becb519fdfa57574d5e07eaa5e7d",
        "config": "sha256:4d4657ef82b5cf8fd56373dbf8b145cdc6d1d7aabbaca1e6a361efedae04e052",
        "diff_ids": [
            "sha256:44a87794b739d471ec360bec56cc0ceac640d06bbf579ee7d9061a0b88d09818",
            "sha256:23f2aab4f776183294beee7beb650a9359824a3ed980d76afce5e09860af7cee",
            "sha256:3785b052b3c9f4092084fecd6631da41eaf96a84b856c7f47088d631f26aa2d3",
            "sha256:70afe7f321fea522177500b2fef4cfc55551ef8714a9256067f3b0dc7ffcd34a",
            "sha256:96c88215b08ea33ee67c605f7d7ec659cbe961da44a7a1f7115ac0c1ca66edd3",
        ],
        "license": "MIT / Debian package licenses",
        "disk_bound": {
            "compressed": 81041595,
            "layer_logical": 230363511,
            "allocated_layer_upper": 275017728,
            "measurement": "Frozen manifest compressed-size sum; verified uncompressed layer member logical sizes and per-member 4096-byte allocation upper bound. Identity bound to this exact manifest/config/ordered layers.",
        },
    },
    "postgres": {
        "ref": "postgres@sha256:8d5f8486c9be456cedd25c7acff28cb9ed8f3f23bf9c865a5f899d6ea8efadf0",
        "config": "sha256:0bc1a3b59e58de4d036b4fdc2340e8b63f8aac637d6b619cf31387eb6d1912b9",
        "diff_ids": [
            "sha256:e48af84b2108a5d73effd9e16685b42ac33e7994e606398c46690918f5f3604a",
            "sha256:2611641010ad2e32b8e8543039ae1aa31c0fad9bf9d85c921eee30ef9ebea988",
            "sha256:3f42861919a9a13a005d336ea94e458ccf9b1d56c45a03935d2f46198c4f1411",
            "sha256:d89e77f3fa01774d92c0928654f5600f3a7f69af35e5a42bc44d148af4eb28c3",
            "sha256:69e36c5c0b913993b5a9ef068a68671362eaf33227da66b2cdb7677f74069952",
            "sha256:023a997b84daf3f2365c8c8f72316840e12b72a327b2dd4a3c792e0e6cac1b44",
            "sha256:f6edbf385cf5595543cd33a2e69e811baa0ed62519373bc26e2580f2fc2cfda1",
            "sha256:cb5b40f8a25c7c71a53c87bb28bf2d35f53f95b95464fe2d47ebcd68dcfd4956",
            "sha256:6caaa85305f6e4062fa56c4a8cd20873266adda45247a1e5e80df04dc9a01446",
            "sha256:93584518d9de668ce497d2958904015e964a04652c56d4f81e3b2f7228196928",
            "sha256:8a55e4cb36208b7e01376f285e3ec71498857d85ab9c602a9a3a1df0e16c58c4",
            "sha256:e6ba0749a08a782ab42d60e1e7f26a42753b6bf058bbd2b6a388ad1d0d4c6888",
            "sha256:b9be123313b5b715ae11bcd59edc78413823abaad241cda84a1263c6e379e858",
            "sha256:c012763d034bccec3408f88a93e45a81d2ac1663394c5741b548ed06cab70bb9",
        ],
        "license": "PostgreSQL / Debian package licenses",
        "disk_bound": {
            "compressed": 155003113,
            "layer_logical": 435032886,
            "allocated_layer_upper": 497881088,
            "measurement": "Frozen manifest compressed-size sum; verified uncompressed layer member logical sizes and per-member 4096-byte allocation upper bound. Identity bound to this exact manifest/config/ordered layers.",
        },
    },
}


def disk_gate(root: Path, remaining: int = 0) -> None:
    """Reserve finite remaining allocations and measured two-GiB headroom in task storage."""
    assert root.is_mount(), "Owned bounded filesystem is required"
    free = shutil.disk_usage(root).free
    assert free >= 2147483648 + remaining, "Task filesystem headroom/reservation failed"


def acquire(root: Path) -> None:
    """Acquire only frozen minimal artifacts; host controller is already slice-constrained."""
    disk_gate(root, 2 * 1024**3)
    tools = root / "tools"
    artifacts = tools / "artifacts"
    artifacts.mkdir(parents=True)
    wheel_upper = 0
    for filename, url, digest, size in ARTIFACTS:
        disk_gate(root, size + 1024**3)
        target = artifacts / filename
        with urllib.request.urlopen(url, timeout=120) as response, target.open("xb") as output:
            remaining = size
            while remaining:
                block = response.read(min(1048576, remaining))
                assert block, "Truncated frozen artifact"
                output.write(block)
                remaining -= len(block)
            assert not response.read(1), "Artifact exceeds frozen byte count"
        assert (
            target.stat().st_size == size
            and hashlib.sha256(target.read_bytes()).hexdigest() == digest
        )
        if filename.endswith(".tar.gz"):
            with tarfile.open(target) as archive:
                members = archive.getmembers()
                upper = sum(((m.size + 4095) // 4096 + 1) * 4096 for m in members)
                assert upper <= 299630592, "Frozen interpreter extraction bound"
                disk_gate(root, upper + 1024**3)
                archive.extractall(tools, filter="data")
            binary = tools / "python/bin/python3.14"
            assert (
                hashlib.sha256(binary.read_bytes()).hexdigest()
                == "a3c5f01bf9e5cd625b74fd700bb58a579c82df228c50b57d2485d006582a8b01"
            )
        else:
            with zipfile.ZipFile(target) as archive:
                wheel_upper += sum(
                    ((m.file_size + 4095) // 4096 + 1) * 4096 for m in archive.infolist()
                )
            assert wheel_upper <= 160 * 1024**2, "Offline wheel installation bound"
    extensions = tools / "extensions"
    extensions.mkdir()
    for name, pin in EXTENSIONS.items():
        disk_gate(root, pin["compressed"] + pin["expanded_upper"] + 1024**3)
        with urllib.request.urlopen(pin["url"], timeout=120) as response:
            compressed = response.read(pin["compressed"] + 1)
        assert (
            len(compressed) == pin["compressed"]
            and hashlib.sha256(compressed).hexdigest() == pin["sha256"]
        )
        expanded = gzip.decompress(compressed)
        assert (
            len(expanded) <= pin["expanded_upper"]
            and hashlib.sha256(expanded).hexdigest() == pin["expanded_sha256"]
        )
        (extensions / (name + ".duckdb_extension")).write_bytes(expanded)
    (root / "work/acquisition.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "artifacts": [r[0] for r in ARTIFACTS],
                "wheel_allocated_upper": wheel_upper,
            }
        )
    )


def native_smoke(root: Path, commit: str) -> None:
    """Run populated actual-adapter prepare/reopen/write denial and owned cleanup."""
    import duckdb

    from dango.operations.contracts import OperationContext
    from dango.security.native_credentials import SecretRef
    from dango.storage.catalogs import CatalogAllocator, NativeCatalogPort, _literal, _quote

    assert sys.version_info[:3] == (3, 14, 10) and duckdb.__version__ == "2.0.0"
    root.mkdir(mode=0o700)
    for name in ("state", "data"):
        (root / name).mkdir(mode=0o700)
    credential = json.loads(Path("/secrets/catalog.json").read_text())
    ref = SecretRef(
        project_id="task-project", actor_id="task-owner", credential_id="ordinary", version="v1"
    )
    context = OperationContext(
        operation_id="native-smoke",
        project_id="task-project",
        actor_id="task-owner",
        capability="catalog.admin",
        permission_revision="current",
        trace_id="synthetic",
    )
    port = NativeCatalogPort(Path("/tools/extensions"))

    def allocator():
        return CatalogAllocator(
            root / "state",
            root / "data",
            port=port,
            credential_ref=ref,
            catalog_database="t04a_catalog",
            bundle_id="t44a-python314-duckdb2-linux-amd64-development",
            resolve=lambda value: dict(credential) if value == ref else None,
            authorize=lambda ctx, binding, action: (
                ctx == context
                and binding.project_id == context.project_id
                and binding.owner_actor_id == context.actor_id
            ),
        )

    with port._postgres(credential) as connection:
        role = port._query(
            connection,
            "SELECT current_user,session_user,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=current_user",
        )
    assert role == [("t04a_writer", "t04a_writer", False, False, False, False, False, True)], (
        "Ordinary role attributes required"
    )
    (root.parent / "native-stage.txt").write_text("prepare")
    owner = allocator()
    binding = owner.allocate(context, "production")
    (root.parent / "native-stage.txt").write_text("populate")
    with owner.attach(context, binding, read_only=False) as connection:
        connection.execute("CREATE SCHEMA catalog.sales; CREATE SCHEMA catalog.finance")
        connection.execute("CREATE TABLE catalog.sales.orders(id INTEGER, amount INTEGER)")
        connection.execute("INSERT INTO catalog.sales.orders VALUES (1,100),(2,200),(3,50)")
        connection.execute("CREATE TABLE catalog.finance.orders AS SELECT 7::INTEGER amount")

    def metadata():
        schema = _quote(binding.metadata_schema)
        with port._postgres(credential) as connection:
            return {
                "namespace": port._query(
                    connection,
                    "SELECT oid::bigint FROM pg_namespace WHERE nspname="
                    + _literal(binding.metadata_schema),
                ),
                "metadata": port._query(
                    connection,
                    "SELECT key,value FROM "
                    + schema
                    + ".ducklake_metadata WHERE scope IS NULL ORDER BY key,value",
                ),
                "snapshots": port._query(
                    connection,
                    "SELECT snapshot_id FROM " + schema + ".ducklake_snapshot ORDER BY snapshot_id",
                ),
                "tables": port._query(
                    connection,
                    "SELECT s.schema_name,t.table_name,t.table_uuid::text FROM "
                    + schema
                    + ".ducklake_table t JOIN "
                    + schema
                    + ".ducklake_schema s USING(schema_id) WHERE t.end_snapshot IS NULL AND s.end_snapshot IS NULL ORDER BY s.schema_name,t.table_name",
                ),
                "spec": port._query(
                    connection,
                    "SELECT c.oid::bigint,c.relname,a.attnum,a.attname,a.atttypid::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname="
                    + _literal(binding.metadata_schema)
                    + " AND a.attnum>0 ORDER BY c.relname,a.attnum",
                ),
            }

    (root.parent / "native-stage.txt").write_text("read_only_reopen")
    before = metadata()
    assert [(r[0], r[1]) for r in before["tables"]] == [("finance", "orders"), ("sales", "orders")]
    fresh = allocator()
    reopened = fresh.allocate(context, "production")
    assert reopened == binding
    denial = None
    with fresh.attach(context, reopened, read_only=True) as connection:
        test_same_table_in_two_original_schemas(connection)
        denial = test_read_only_prod_binding_refuses_write(connection)
    test_native_prepare_and_reopen_no_schema_mutation(before, metadata())
    (root.parent / "native-stage.txt").write_text("owned_cleanup")
    fresh.remove_owned(context, reopened)
    assert not Path(binding.data_root_ref).exists()
    with port._postgres(credential) as connection:
        assert not port._namespace(connection, binding)
    report = {
        "status": "PASS_NATIVE_ADAPTER_SMOKE",
        "commit": commit,
        "pins": {
            "python": sys.version,
            "duckdb": duckdb.__version__,
            "extensions": {name: pin["expanded_sha256"] for name, pin in EXTENSIONS.items()},
            "artifacts": [{"file": row[0], "sha256": row[2]} for row in ARTIFACTS],
        },
        "role": role,
        "expected": {"sales": 350, "finance": 7},
        "actual": {"sales": 350, "finance": 7},
        "metadata_before_reopen": before,
        "metadata_unchanged": True,
        "write_denial_type": denial,
        "owned_cleanup": True,
        "scope": "Read-only catalog attachment, not a SQL sandbox, writer fence or tenant security proof",
    }
    payload = json.dumps(report, default=str)
    assert credential["password"] not in payload
    (root.parent / "native-result.json").write_text(payload + "\n")


def test_same_table_in_two_original_schemas(connection) -> None:
    """Execute independent fixture arithmetic and exact original-schema row checks."""
    assert connection.execute("SELECT * FROM catalog.sales.orders ORDER BY id").fetchall() == [
        (1, 100),
        (2, 200),
        (3, 50),
    ]
    assert connection.execute("SELECT sum(amount) FROM catalog.sales.orders").fetchone() == (350,)
    assert connection.execute("SELECT * FROM catalog.finance.orders").fetchall() == [(7,)]


def test_read_only_prod_binding_refuses_write(connection) -> str:
    """Require actual engine read-only rejection, never a generic bootstrap failure."""
    import duckdb

    try:
        connection.execute("UPDATE catalog.sales.orders SET amount=999 WHERE id=1")
    except duckdb.Error as error:
        assert "read-only" in str(error).lower() or "read only" in str(error).lower()
        return type(error).__name__
    raise AssertionError("An actual native UPDATE must be denied")


def test_native_prepare_and_reopen_no_schema_mutation(before, after) -> None:
    """Compare independent raw PostgreSQL identities/spec/snapshots across reopen."""
    assert after == before, "Read-only reopen/write denial mutated native metadata"


def main() -> None:
    """Select an explicit acquisition or native smoke phase; never execute during import."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--acquire", type=Path)
    parser.add_argument("--native-case", choices=["smoke"])
    parser.add_argument("--task-root", type=Path)
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.acquire:
        acquire(args.acquire)
    else:
        assert args.native_case == "smoke" and args.task_root and args.commit
        try:
            native_smoke(args.task_root, args.commit)
        except Exception as error:
            (args.task_root.parent / "native-result.json").write_text(
                json.dumps(
                    {
                        "status": "FAIL",
                        "commit": args.commit,
                        "error_type": type(error).__name__,
                        "stage": (args.task_root.parent / "native-stage.txt").read_text()
                        if (args.task_root.parent / "native-stage.txt").exists()
                        else "initialization",
                    }
                )
                + "\n"
            )
            raise SystemExit("Native adapter smoke failed; sanitized result retained") from None


if __name__ == "__main__":
    main()
