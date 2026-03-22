"""Tests for web routes."""

import io
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Set test directories before importing app
os.environ["MERCHRUNS_DIR"] = tempfile.mkdtemp()
os.environ["VENDORS_YAML_PATH"] = ""  # Will be mocked

# Mock Redis/RQ before importing app
with patch("merchfill_web.job_queue.get_redis_connection"):
    with patch("merchfill_web.job_queue.get_queue"):
        from merchfill_web.app import app


@pytest.fixture
def client() -> TestClient:
    """Create a test client."""
    return TestClient(app)


@pytest.fixture
def vendors_yaml(tmp_path: Path) -> Path:
    """Create a temporary vendors.yaml."""
    vendors_file = tmp_path / "vendors.yaml"
    vendors_file.write_text("""
vendors:
  TestVendor:
    domain: test.com
    use_playwright: false
""")
    os.environ["VENDORS_YAML_PATH"] = str(vendors_file)
    return vendors_file


class TestIndexPage:
    """Tests for the index page."""

    def test_index_returns_200(self, client: TestClient, vendors_yaml: Path):
        """Test that index page loads."""
        response = client.get("/")

        assert response.status_code == 200
        assert "Merchfill" in response.text

    def test_index_shows_form(self, client: TestClient, vendors_yaml: Path):
        """Test that index page shows upload form."""
        response = client.get("/")

        assert "Input CSV File" in response.text
        assert "Concurrency" in response.text
        assert "Start Run" in response.text


class TestValidation:
    """Tests for CSV validation."""

    def test_validate_valid_csv(self, client: TestClient, vendors_yaml: Path):
        """Test validating a valid CSV."""
        csv_content = b"""Product Handle,Vendor,Has Image,Has Variant Images,Has Description
test-1,TestVendor,false,false,false
test-2,TestVendor,true,true,true
"""
        response = client.post(
            "/validate",
            files={"file": ("test.csv", io.BytesIO(csv_content), "text/csv")},
        )

        assert response.status_code == 200
        assert "Valid" in response.text
        assert "2 rows" in response.text

    def test_validate_missing_columns(self, client: TestClient, vendors_yaml: Path):
        """Test validating a CSV with missing columns."""
        csv_content = b"""Product Handle,Vendor
test-1,TestVendor
"""
        response = client.post(
            "/validate",
            files={"file": ("test.csv", io.BytesIO(csv_content), "text/csv")},
        )

        assert response.status_code == 200
        assert "Invalid" in response.text
        assert "Has Image" in response.text or "Missing" in response.text

    def test_validate_empty_file(self, client: TestClient, vendors_yaml: Path):
        """Test validating an empty file."""
        response = client.post(
            "/validate",
            files={"file": ("test.csv", io.BytesIO(b""), "text/csv")},
        )

        assert response.status_code == 200
        assert "Empty" in response.text or "Invalid" in response.text


class TestRunCreation:
    """Tests for run creation."""

    @patch("merchfill_web.routes.enqueue_run")
    def test_create_run_success(self, mock_enqueue, client: TestClient, vendors_yaml: Path):
        """Test creating a run with valid CSV."""
        mock_enqueue.return_value = "job-123"

        csv_content = b"""Product Handle,Vendor,Has Image,Has Variant Images,Has Description
test-1,TestVendor,false,false,false
"""
        response = client.post(
            "/runs",
            data={"concurrency": "5", "max_rows": "", "force": "", "dry_run": ""},
            files={"file": ("test.csv", io.BytesIO(csv_content), "text/csv")},
            follow_redirects=False,
        )

        # Should redirect to run detail page
        assert response.status_code == 303
        assert "/runs/run_" in response.headers["location"]

    def test_create_run_invalid_csv(self, client: TestClient, vendors_yaml: Path):
        """Test creating a run with invalid CSV."""
        csv_content = b"""Invalid,Headers,Only
data,here,now
"""
        response = client.post(
            "/runs",
            data={"concurrency": "5"},
            files={"file": ("test.csv", io.BytesIO(csv_content), "text/csv")},
        )

        assert response.status_code == 400

    def test_create_run_empty_file(self, client: TestClient, vendors_yaml: Path):
        """Test creating a run with empty file."""
        response = client.post(
            "/runs",
            data={"concurrency": "5"},
            files={"file": ("test.csv", io.BytesIO(b""), "text/csv")},
        )

        assert response.status_code == 400


class TestRunDetail:
    """Tests for run detail page."""

    @patch("merchfill_web.routes.enqueue_run")
    def test_run_detail_exists(self, mock_enqueue, client: TestClient, vendors_yaml: Path):
        """Test viewing an existing run."""
        mock_enqueue.return_value = "job-123"

        # Create a run first
        csv_content = b"""Product Handle,Vendor,Has Image,Has Variant Images,Has Description
test-1,TestVendor,false,false,false
"""
        create_response = client.post(
            "/runs",
            data={"concurrency": "5"},
            files={"file": ("test.csv", io.BytesIO(csv_content), "text/csv")},
            follow_redirects=False,
        )

        run_url = create_response.headers["location"]

        # View the run
        response = client.get(run_url)

        assert response.status_code == 200
        assert "run_" in response.text
        assert "QUEUED" in response.text

    def test_run_detail_not_found(self, client: TestClient, vendors_yaml: Path):
        """Test viewing a non-existent run."""
        response = client.get("/runs/nonexistent_run_id")

        assert response.status_code == 404


class TestProgressEndpoint:
    """Tests for progress partial endpoint."""

    @patch("merchfill_web.routes.enqueue_run")
    def test_progress_endpoint(self, mock_enqueue, client: TestClient, vendors_yaml: Path):
        """Test progress partial returns correct data."""
        mock_enqueue.return_value = "job-123"

        # Create a run
        csv_content = b"""Product Handle,Vendor,Has Image,Has Variant Images,Has Description
test-1,TestVendor,false,false,false
"""
        create_response = client.post(
            "/runs",
            data={"concurrency": "5"},
            files={"file": ("test.csv", io.BytesIO(csv_content), "text/csv")},
            follow_redirects=False,
        )

        run_id = create_response.headers["location"].split("/")[-1]

        # Get progress
        response = client.get(f"/runs/{run_id}/progress")

        assert response.status_code == 200
        assert "QUEUED" in response.text


class TestResultsEndpoint:
    """Tests for results partial endpoint."""

    @patch("merchfill_web.routes.enqueue_run")
    def test_results_empty(self, mock_enqueue, client: TestClient, vendors_yaml: Path):
        """Test results endpoint with no results."""
        mock_enqueue.return_value = "job-123"

        # Create a run
        csv_content = b"""Product Handle,Vendor,Has Image,Has Variant Images,Has Description
test-1,TestVendor,false,false,false
"""
        create_response = client.post(
            "/runs",
            data={"concurrency": "5"},
            files={"file": ("test.csv", io.BytesIO(csv_content), "text/csv")},
            follow_redirects=False,
        )

        run_id = create_response.headers["location"].split("/")[-1]

        # Get results
        response = client.get(f"/runs/{run_id}/results")

        assert response.status_code == 200


class TestDownloads:
    """Tests for download endpoints."""

    def test_download_not_found(self, client: TestClient, vendors_yaml: Path):
        """Test downloading from non-existent run."""
        response = client.get("/runs/nonexistent/download/shopify_update.csv")

        assert response.status_code == 404
