"""tests/integration/t44a_qualification/engine_behavior.py

Exact engine cases through frozen Python/dbt; no compatibility switches or substitutes.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACT = json.loads((HERE / "engine-cases.json").read_text())
EXTRA_PEAK = CONTRACT["limits"]["extra_peak"]
FILES = ["engine_behavior.py", "engine-cases.json", "engine_client.cjs"]
ARTIFACTS = ["manifest.json", "run_results.json", "catalog.json"]


def digest(data):
    """Hash exact bytes, including the original native library and extension."""
    return hashlib.sha256(data).hexdigest()


def known():
    """Read only dedicated synthetic credentials, never ambient configuration."""
    file = Path("/secrets/app.json")
    return json.loads((file if file.exists() else Path("/secrets/catalog.json")).read_text())


def safe(data):
    """Refuse raw secrets before any redaction or evidence export."""
    assert not any(value.encode() in data for value in known().values()), "Raw engine secret leak"
    return data


def save(name, value):
    """Bound each new sanitized receipt before writing."""
    data = safe(json.dumps(value, indent=2, default=str).encode() + b"\n")
    assert len(data) <= CONTRACT["limits"]["report"], "Engine report bound"
    Path("/work", name).write_bytes(data)


def verify_python():
    """Bind the installed engine, including statically linked extensions, before import."""
    pins = json.loads((HERE / "pins.json").read_text())
    pin = next(p for p in pins["artifacts"] if p.get("name") == "duckdb")
    archive = Path("/tools/artifacts", pin["file"])
    assert digest(archive.read_bytes()) == pin["sha256"]
    with zipfile.ZipFile(archive) as wheel:
        names = [n for n in wheel.namelist() if n.startswith("_duckdb.") and n.endswith(".so")]
        assert len(names) == 1
        expected = digest(wheel.read(names[0]))
    spec = importlib.util.find_spec("_duckdb")
    assert spec and digest(Path(spec.origin).read_bytes()) == expected
    return {"wheel_sha256": pin["sha256"], "native_sha256": expected}


def verify_extension(name):
    """Hash the already staged signed extension before any native bootstrap."""
    pin = json.loads((HERE / "pins.json").read_text())["extensions"][name]
    file = Path("/home/probe/.duckdb/extensions/v2.0.0/linux_amd64", name + ".duckdb_extension")
    assert file.is_file() and not file.is_symlink()
    assert digest(file.read_bytes()) == pin["expanded_sha256"]
    return pin


def connect(catalog=False):
    """Use small native sessions with an owned directory and zero spill allowance."""
    verify_python()
    for name in ["ducklake", "postgres_scanner"] if catalog else ["httpfs"]:
        verify_extension(name)
    import duckdb

    assert duckdb.__version__ == "2.0.0"
    directory = Path("/work/engine-spill")
    directory.mkdir(mode=0o700, exist_ok=True)
    assert directory.resolve() == directory and not directory.is_symlink()
    con = duckdb.connect(
        config={
            "memory_limit": "128MB",
            "threads": "1",
            "temp_directory": str(directory),
            "max_temp_directory_size": "0B",
        }
    )
    try:
        assert con.execute(
            "SELECT current_setting('temp_directory'), current_setting('max_temp_directory_size')"
        ).fetchone() == (str(directory), "0 bytes")
        assert con.execute("SELECT current_setting('allow_unsigned_extensions')").fetchone() == (
            False,
        )
        if catalog:
            os.environ["PGPASSFILE"] = "/secrets/pgpass-python"
            con.execute("LOAD postgres_scanner")
            con.execute("LOAD ducklake")
            con.execute(
                "ATTACH 'ducklake:postgres:host=127.0.0.1 port=5432 dbname=probe "
                "user=postgres' AS lake (READ_ONLY, AUTOMATIC_MIGRATION FALSE)"
            )
        else:
            con.execute("LOAD httpfs")
        return con
    except Exception:
        con.close()
        raise


def expected_error(case, message):
    """Only the declared native diagnostic can satisfy an engine refusal."""
    safe(message.encode())
    assert case["error"]["native_type"] + " Error:" in message
    assert case["error"]["message_contains"] in message, "Wrong engine refusal layer"


def auth_error(message):
    """Require a native403 for the owned S3 object, not an install/bootstrap failure."""
    safe(message.encode())
    assert "http" in message.lower() and "403" in message and "synthetic-readiness" in message
    assert any(token in message for token in ["Forbidden", "SignatureDoesNotMatch", "AccessDenied"])
    return message


def inventory(con, names):
    """Observe loaded identities in this exact Python connection, without secret inspection."""
    rows = con.execute(
        "SELECT extension_name,loaded,installed,extension_version,install_mode "
        "FROM duckdb_extensions() ORDER BY extension_name"
    ).fetchall()
    pins = json.loads((HERE / "pins.json").read_text())["extensions"]
    selected = {r[0]: r for r in rows if r[0] in names}
    assert set(selected) == set(names)
    for name, row in selected.items():
        assert row[1] is True and row[2] is True
        if name in pins:
            assert row[3] == pins[name]["revision"], "Wrong loaded extension revision"
        else:
            assert name == "icu" and row[4] == "STATICALLY_LINKED"
    return list(selected.values())


def run_dbt(report):
    """Materialize real positives and retain three separate genuine native error stages."""
    project, stages = Path("/work/engine-project"), Path("/work/engine-artifacts")
    assert not project.exists() and not stages.exists()
    (project / "models").mkdir(parents=True)
    stages.mkdir()
    for name in ["dbt_project.yml", "profiles.yml", "catalogs.yml"]:
        text = Path("/work/project", name).read_text()
        if name == "profiles.yml":
            assert text.count("schema: sales") == 1
            text = text.replace("schema: sales", "schema: engine_checks")
            text = text.replace("memory_limit: 512MB", "memory_limit: 128MB")
            text = text.replace("max_temp_directory_size: 512MB", "max_temp_directory_size: 0B")
            text = text.replace("/work/duckdb-temp", "/work/engine-spill")
        (project / name).write_text(text)
    for case in CONTRACT["cases"]:
        prefix = "negative" if "error" in case else "positive"
        (project / "models" / ("engine_" + prefix + "_" + case["id"] + ".sql")).write_text(
            case["sql"] + "\n"
        )
    report["dbt"] = []
    selections = [("positive", "engine_positive_*", None)] + [
        (c["id"], "engine_negative_" + c["id"], c) for c in CONTRACT["cases"] if "error" in c
    ]
    for stage, selection, case in selections:
        target = project / "target"
        if target.exists():
            shutil.rmtree(target)  # Exact new synthetic project only; no stale catalog credit.
        argv = [
            "/work/venv/bin/dbt",
            "run",
            "--select",
            selection,
            "--write-catalog",
            "--threads",
            "1",
            "--no-parallel",
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(project),
            "--no-version-check",
            "--no-send-anonymous-usage-stats",
        ]
        child = subprocess.run(
            argv,
            capture_output=True,
            timeout=180,
            env={**os.environ, "ADBC_DRIVER_PATH": "/home/probe/adbc"},
        )
        output = safe(child.stdout + child.stderr)
        assert len(output) <= 8 * 1024**2
        row = {
            "stage": stage,
            "argv": argv,
            "exit": child.returncode,
            "stdout": child.stdout.decode(),
            "stderr": child.stderr.decode(),
            "artifacts": {},
        }
        report["dbt"].append(row)
        for name in ARTIFACTS:
            file = target / name
            if file.exists():
                data = safe(file.read_bytes())
                assert len(data) <= CONTRACT["limits"]["stage_artifact"]
                dest = stages / (stage + "-" + name)
                dest.write_bytes(data)
                row["artifacts"][dest.name] = digest(data)
        results = json.loads((target / "run_results.json").read_text())["results"]
        assert "manifest.json" in {n.split(stage + "-", 1)[-1] for n in row["artifacts"]}
        if case:
            assert child.returncode == 1 and len(results) == 1 and results[0]["status"] == "error"
            assert results[0]["unique_id"] == "model.t44a_probe." + selection
            expected_error(case, output.decode())
            with connect(True) as recovery:
                sql = "SELECT sum(amount) FROM lake.sales.orders"
                actual = recovery.execute(sql).fetchall()
                row["recovery"] = {
                    "connection": "fresh bounded READ_ONLY",
                    "sql": sql,
                    "actual": actual,
                    "expected": [[350]],
                }
                assert actual == [(350,)], "Known350 recovery failed after dbt native error"
        else:
            assert child.returncode == 0
            positives = [c for c in CONTRACT["cases"] if "expected" in c]
            assert len(results) == len(positives) and all(r["status"] == "success" for r in results)
            with connect(True) as con:
                for positive in positives:
                    actual = con.execute(
                        "SELECT * FROM lake.engine_checks.engine_positive_" + positive["id"]
                    ).fetchall()
                    assert json.loads(json.dumps(actual)) == positive["expected"]
    assert (
        sum(
            p.stat().st_blocks * 512
            for p in [project, stages, *project.rglob("*"), *stages.rglob("*")]
        )
        <= 448 * 1024**2
    )


def early():
    """Reach actual Python and ordinary ADBC dbt calls before app dependency acquisition."""
    from held_reader import captures

    identity = verify_python()
    import duckdb

    report = {
        "status": "FAIL",
        "client": "Python DuckDB2 + genuine dbtOSS2 ADBC",
        "cases": [],
        "contract_sha256": digest((HERE / "engine-cases.json").read_bytes()),
    }
    try:
        report["engine_identity"] = identity
        with connect(True) as con:
            before = captures(con)
            for case in CONTRACT["cases"]:
                row = {"id": case["id"], "sql": case["sql"]}
                report["cases"].append(row)
                try:
                    actual = con.execute(case["sql"]).fetchall()
                except duckdb.Error as error:
                    row.update(native_type=type(error).__name__, message=str(error))
                    assert "error" in case
                    expected_error(case, str(error))
                else:
                    row["actual"] = actual
                    assert "expected" in case and json.loads(json.dumps(actual)) == case["expected"]
                row["status"] = "PASS"
            report["extensions"] = inventory(con, ["ducklake", "postgres_scanner", "icu"])
        run_dbt(report)
        with connect(True) as con:
            assert con.execute("SELECT sum(amount) FROM lake.sales.orders").fetchone() == (350,)
            assert captures(con) == before
        report.update(status="PASS_PYTHON_AND_DBT_ENGINE_CASES", selected_capture=before)
    finally:
        save("engine-python-early.json", report)


def s3():
    """Use three fresh native instances: exact body, native403, same body again."""
    verify_python()
    import duckdb

    report = {"status": "FAIL", "client": "Python DuckDB2/httpfs", "sequence": []}
    secrets = known()
    try:
        for label, key in [
            ("correct_before", "s3Secret"),
            ("wrong_secret", "wrongS3"),
            ("correct_after", "s3Secret"),
        ]:
            row = {"stage": label, "sql": CONTRACT["s3"]["sql"]}
            report["sequence"].append(row)
            with connect() as con:
                row["extensions"] = inventory(con, ["httpfs"])

                def literal(text):
                    return "'" + text.replace("'", "''") + "'"

                # Never emit the interpolated credential SQL, including on native error.
                con.execute(
                    "CREATE SECRET engine_s3 (TYPE s3, PROVIDER config, KEY_ID "
                    + literal(secrets["s3Access"])
                    + ", SECRET "
                    + literal(secrets[key])
                    + ", ENDPOINT '127.0.0.1:3900', REGION 'garage', "
                    "SCOPE 's3://t44a-results/synthetic-readiness', URL_STYLE 'path', USE_SSL false)"
                )
                try:
                    rows = con.execute(row["sql"]).fetchall()
                except duckdb.Error as error:
                    message = auth_error(str(error))
                    row.update(native_type=type(error).__name__, message=message)
                    assert label == "wrong_secret"
                else:
                    row["actual"] = rows
                    assert label != "wrong_secret" and rows == [
                        (CONTRACT["s3"]["sha256"], CONTRACT["s3"]["bytes"])
                    ]
                row["status"] = "PASS"
        report["status"] = "PASS_PYTHON_HTTPFS_SECRET_GOOD_BAD_GOOD"
    finally:
        save("engine-python-s3.json", report)


def reserve(root):
    """Charge all extra artifacts, duplicate exports, Garage state and reports before growth."""
    mode = json.loads((root / "evidence/result.json").read_text()).get("qualification_mode")
    return EXTRA_PEAK if mode == "engine" else 0


def require_early(root):
    """An engine-mode acquisition requires the exact successful native/dbt prerequisite."""
    result = json.loads((root / "evidence/result.json").read_text())
    if result.get("qualification_mode") != "engine":
        return
    data = (root / "evidence/engine-python-early.json").read_bytes()
    report = json.loads(data)
    assert result["engine_early"] == digest(data)
    assert report["status"] == "PASS_PYTHON_AND_DBT_ENGINE_CASES"
    assert report["contract_sha256"] == digest((HERE / "engine-cases.json").read_bytes())
    assert len(report["cases"]) == len(CONTRACT["cases"])
    assert all(c["status"] == "PASS" for c in report["cases"])


def export(rig, credentials, disk_gate):
    """Retain full original known-secret-checked native artifacts with both copies accounted."""
    files = sorted((rig.root / "work").glob("engine-*.json"))
    files += sorted((rig.root / "work/engine-artifacts").glob("*.json"))
    assert sum(p.stat().st_size for p in files) <= 400 * 1024**2
    disk_gate(rig, sum(p.stat().st_size for p in files), "before-engine-evidence-export")
    for file in files:
        data = file.read_bytes()
        assert not any(value.encode() in data for value in credentials.values())
        name = file.name if file.name.startswith("engine-") else "engine-" + file.name
        (rig.root / "evidence" / name).write_bytes(data)
    disk_gate(rig, 0, "after-engine-evidence-export")


if __name__ == "__main__":
    try:
        {"early": early, "s3": s3}[sys.argv[1]]()
    except Exception as error:
        message = str(error)
        leaked = any(v in message for v in known().values())
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "raw_secret_leak": leaked,
                    "error": "Raw engine secret exposure refused" if leaked else message,
                }
            )
        )
        sys.exit(1)
