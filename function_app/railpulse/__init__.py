"""RailPulse Cloud ingestion package.

Deliberately empty of imports. The modules here split along a line that matters
for testing: `transform`, `hubs` and `irail` have no database dependency and are
unit-tested offline, while `database`, `loader`, `pipeline`, `migrations` and
`reporting` need pyodbc and a real Azure SQL target. Importing anything here
would drag pyodbc into the offline test run and make the fast tests need a
driver they have no use for.
"""

__all__ = [
    "config",
    "database",
    "hubs",
    "irail",
    "loader",
    "migrations",
    "pipeline",
    "reporting",
    "transform",
]
