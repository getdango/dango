"""tests/unit/test_model_wizard.py

Tests for dbt model wizard.
"""

import pytest

from dango.cli.model_wizard import ModelWizard


@pytest.fixture
def project_root(tmp_path):
    """Create a minimal valid project structure for ModelWizard"""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text(
        "project:\n  name: test\n  created_by: test\n  purpose: test project\n"
    )
    return tmp_path


@pytest.fixture
def project_with_models(project_root):
    """Create a project with sample dbt models"""
    models_dir = project_root / "dbt" / "models"

    # Create staging models
    staging_dir = models_dir / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "stg_stripe_customers.sql").write_text("SELECT 1")
    (staging_dir / "stg_stripe_orders.sql").write_text("SELECT 1")
    (staging_dir / "stg_github_repos.sql").write_text("SELECT 1")

    # Create intermediate models
    intermediate_dir = models_dir / "intermediate"
    intermediate_dir.mkdir(parents=True, exist_ok=True)
    (intermediate_dir / "int_customer_summary.sql").write_text("SELECT 1")

    # Create marts models
    marts_dir = models_dir / "marts"
    marts_dir.mkdir(parents=True, exist_ok=True)
    (marts_dir / "customers.sql").write_text("SELECT 1")

    return project_root


def test_generate_sql_template_with_upstream_tables(project_root):
    """Test SQL generation with multiple upstream tables"""
    wizard = ModelWizard(project_root)

    sql = wizard._generate_sql_template(
        layer="intermediate",
        name="int_test.sql",
        description="Test model",
        materialization="table",
        upstream_tables=["stg_stripe_customers", "stg_stripe_orders"],
    )

    # Check header
    assert "-- int_test" in sql
    assert "-- Test model" in sql

    # Check config block
    assert "{{ config(" in sql
    assert "materialized='table'" in sql
    assert "schema='intermediate'" in sql

    # Check CTE structure
    assert "WITH customers AS (" in sql
    assert "orders AS (" in sql
    assert "{{ ref('stg_stripe_customers') }}" in sql
    assert "{{ ref('stg_stripe_orders') }}" in sql

    # Check final SELECT
    assert "SELECT" in sql
    assert "customers.*" in sql
    assert "FROM customers" in sql

    # Ensure no placeholder
    assert "1 AS placeholder" not in sql


def test_generate_sql_template_no_upstream_tables_none(project_root):
    """Test SQL generation with upstream_tables=None produces placeholder"""
    wizard = ModelWizard(project_root)

    sql = wizard._generate_sql_template(
        layer="marts",
        name="customer_revenue.sql",
        description="",
        materialization="table",
        upstream_tables=None,
    )

    # Check placeholder is present
    assert "1 AS placeholder" in sql

    # Check no CTE structure
    assert "WITH" not in sql
    assert "{{ ref(" not in sql


def test_generate_sql_template_no_upstream_tables_empty_list(project_root):
    """Test SQL generation with empty upstream_tables list produces placeholder"""
    wizard = ModelWizard(project_root)

    sql = wizard._generate_sql_template(
        layer="intermediate",
        name="int_test.sql",
        description="",
        materialization="table",
        upstream_tables=[],
    )

    # Check placeholder is present
    assert "1 AS placeholder" in sql

    # Check no CTE structure
    assert "WITH" not in sql
    assert "{{ ref(" not in sql


def test_get_available_tables_empty_project(project_root):
    """Test _get_available_tables returns empty list for project with no models"""
    wizard = ModelWizard(project_root)
    tables = wizard._get_available_tables()

    assert tables == []


def test_get_available_tables_populates_source(project_with_models):
    """Test _get_available_tables correctly extracts source name for staging tables"""
    wizard = ModelWizard(project_with_models)
    tables = wizard._get_available_tables(layer_filter="staging")

    assert len(tables) == 3

    # Find stg_stripe_customers
    stripe_customers = next(t for t in tables if t["name"] == "stg_stripe_customers")
    assert stripe_customers["layer"] == "staging"
    assert stripe_customers["source"] == "stripe"

    # Find stg_github_repos
    github_repos = next(t for t in tables if t["name"] == "stg_github_repos")
    assert github_repos["layer"] == "staging"
    assert github_repos["source"] == "github"


def test_get_available_tables_all_layers(project_with_models):
    """Test _get_available_tables returns models from all layers when no filter"""
    wizard = ModelWizard(project_with_models)
    tables = wizard._get_available_tables()

    # Should have: 3 staging + 1 intermediate + 1 marts = 5 total
    assert len(tables) == 5

    layers = [t["layer"] for t in tables]
    assert "staging" in layers
    assert "intermediate" in layers
    assert "marts" in layers


def test_get_available_tables_layer_filter_staging(project_with_models):
    """Test _get_available_tables respects layer_filter for staging"""
    wizard = ModelWizard(project_with_models)
    tables = wizard._get_available_tables(layer_filter="staging")

    assert len(tables) == 3
    assert all(t["layer"] == "staging" for t in tables)


def test_get_available_tables_layer_filter_intermediate(project_with_models):
    """Test _get_available_tables respects layer_filter for intermediate"""
    wizard = ModelWizard(project_with_models)
    tables = wizard._get_available_tables(layer_filter="intermediate")

    assert len(tables) == 1
    assert tables[0]["name"] == "int_customer_summary"
    assert tables[0]["layer"] == "intermediate"


def test_alias_derivation_single_table(project_root):
    """Test that table name alias is correctly extracted from last component"""
    wizard = ModelWizard(project_root)

    sql = wizard._generate_sql_template(
        layer="intermediate",
        name="int_test.sql",
        description="",
        materialization="table",
        upstream_tables=["stg_stripe_customers"],
    )

    # Verify the alias "customers" appears in the WITH clause
    assert "WITH customers AS (" in sql
    assert "FROM customers" in sql


def test_alias_derivation_multiple_tables(project_root):
    """Test that aliases are correctly extracted for multiple tables"""
    wizard = ModelWizard(project_root)

    sql = wizard._generate_sql_template(
        layer="intermediate",
        name="int_test.sql",
        description="",
        materialization="table",
        upstream_tables=["stg_stripe_customers", "stg_stripe_orders"],
    )

    # Both aliases should be present
    assert "WITH customers AS (" in sql
    assert "orders AS (" in sql
    # First one should be first alias reference
    assert "FROM customers" in sql


def test_alias_collision_resolution(project_root):
    """Test that colliding aliases fall back to full table name"""
    wizard = ModelWizard(project_root)

    # Both tables end with "customers" — would collide
    sql = wizard._generate_sql_template(
        layer="intermediate",
        name="int_test.sql",
        description="",
        materialization="table",
        upstream_tables=["stg_stripe_customers", "stg_hubspot_customers"],
    )

    # First should use derived alias (customers from stripe)
    assert "WITH customers AS (" in sql
    # Second should fall back to full name (collision detected)
    assert "stg_hubspot_customers AS (" in sql
    # Both refs should be present
    assert "{{ ref('stg_stripe_customers') }}" in sql
    assert "{{ ref('stg_hubspot_customers') }}" in sql
    # First alias used in final SELECT
    assert "FROM customers" in sql
