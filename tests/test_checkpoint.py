"""Tests for Dolt checkpoint creation before push."""

import pytest
from unittest.mock import MagicMock, patch, call

from lookout.push.checkpoint import CheckpointError, create_dolt_checkpoint


class TestCreateDoltCheckpoint:
    """Test checkpoint creation via Dolt SQL procedures."""

    def _mock_session(self, execute_side_effects=None):
        """Create a mock SQLAlchemy session context manager."""
        session = MagicMock()
        if execute_side_effects:
            session.execute.side_effect = execute_side_effects

        store = MagicMock()
        store.session.return_value.__enter__ = MagicMock(return_value=session)
        store.session.return_value.__exit__ = MagicMock(return_value=False)
        return store, session

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_creates_commit_and_tag(self, MockStore):
        store, session = self._mock_session()
        MockStore.return_value = store

        tag = create_dolt_checkpoint("mysql://pi5:3306/shopify", "enrich-20260406")

        assert tag.startswith("pre-push/enrich-20260406_")
        calls = session.execute.call_args_list
        # First call: dolt_commit
        assert "dolt_commit" in str(calls[0])
        # Second call: dolt_tag
        assert "dolt_tag" in str(calls[1])

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_nothing_to_commit_still_tags(self, MockStore):
        """When livesync already committed everything, the commit call
        raises 'nothing to commit' but tagging should still proceed."""
        from sqlalchemy.exc import OperationalError

        nothing_err = OperationalError(
            "CALL dolt_commit", {}, Exception("nothing to commit")
        )
        store, session = self._mock_session(
            execute_side_effects=[nothing_err, MagicMock()]
        )
        MockStore.return_value = store

        tag = create_dolt_checkpoint("mysql://pi5:3306/shopify", "test-run")

        assert tag.startswith("pre-push/test-run_")
        assert session.execute.call_count == 2

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_commit_error_raises_checkpoint_error(self, MockStore):
        """Non-'nothing to commit' errors should raise CheckpointError."""
        from sqlalchemy.exc import OperationalError

        real_err = OperationalError(
            "CALL dolt_commit", {}, Exception("table locked")
        )
        store, session = self._mock_session(
            execute_side_effects=[real_err]
        )
        MockStore.return_value = store

        with pytest.raises(CheckpointError, match="table locked"):
            create_dolt_checkpoint("mysql://pi5:3306/shopify", "test-run")

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_tag_error_raises_checkpoint_error(self, MockStore):
        from sqlalchemy.exc import OperationalError

        tag_err = OperationalError(
            "CALL dolt_tag", {}, Exception("tag already exists")
        )
        store, session = self._mock_session(
            execute_side_effects=[MagicMock(), tag_err]
        )
        MockStore.return_value = store

        with pytest.raises(CheckpointError, match="tag already exists"):
            create_dolt_checkpoint("mysql://pi5:3306/shopify", "test-run")

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_connection_error_raises_checkpoint_error(self, MockStore):
        MockStore.side_effect = Exception("Can't connect to MySQL server on 'pi5'")

        with pytest.raises(CheckpointError, match="Can't connect"):
            create_dolt_checkpoint("mysql://pi5:3306/shopify", "test-run")

    @patch("lookout.push.checkpoint.ShopifyStore")
    def test_tag_name_includes_timestamp(self, MockStore):
        store, session = self._mock_session()
        MockStore.return_value = store

        tag = create_dolt_checkpoint("mysql://pi5:3306/shopify", "enrich-20260406")

        parts = tag.split("_")
        assert len(parts) >= 2
        timestamp_part = parts[-1]
        assert len(timestamp_part) == 6  # HHMMSS
        assert timestamp_part.isdigit()
