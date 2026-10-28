import pytest

# Assertion rewriting must be registered before the plugin imports the helpers.
pytest.register_assert_rewrite("tests.support")

pytest_plugins = ["tests.support.suite_fixtures"]
