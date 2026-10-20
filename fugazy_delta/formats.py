"""Every importer, in one place: importing this module registers them all
with `importers`. The order here does not matter; `importers.identify`
tries binary signatures before text ones."""

from . import gopro, gpx, nmea, racebox_csv, racechrono, trackaddict, vbo, xrk  # noqa: F401
