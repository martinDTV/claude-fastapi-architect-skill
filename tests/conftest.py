"""pytest configuration: fixture projects under tests/fixtures are test data, not tests."""

collect_ignore_glob = ["fixtures/*", "fixtures/**/*"]
