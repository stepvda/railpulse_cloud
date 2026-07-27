/* ===========================================================================
   RailPulse Cloud — 04_seed_reference.sql
   Reference data. Idempotent: safe to re-run on every migration.
   ===========================================================================

   These are the service classes SNCB/NMBS publishes in `vehicleinfo.type`,
   with the S-line number stripped off by the loader (so 'S1', 'S10' and 'S32'
   all arrive here as 'S'). Codes observed live on 2026-07-27 across
   Brussels-Central, Brussels-Midi, Antwerp-Central and Ghent-Sint-Pieters:
   IC, S1..S53, L, EC, ECD, EUR, ICE, T, CHAR.

   `is_seeded = 1` marks a code documented here. The loader inserts anything
   else it meets with `is_seeded = 0` and the code as its own label, so a new
   service class shows up in v_vehicle_type_performance as undocumented instead
   of breaking the ingest on a foreign-key violation. When one appears, add it
   below and re-run this file.
   =========================================================================== */

MERGE dbo.vehicle_types WITH (HOLDLOCK) AS target
USING (VALUES
    ('IC',   N'InterCity',
             N'Fast domestic service calling only at major stations. The backbone of the network.'),
    ('S',    N'Suburban (S-train)',
             N'City-region service on a numbered S line; the line number is kept separately in vehicles.service_line.'),
    ('L',    N'Local',
             N'Omnibus service calling at every station on its route.'),
    ('P',    N'Peak-hour',
             N'Extra commuter train timetabled only during the morning or evening peak.'),
    ('EXT',  N'Extra',
             N'Additional service inserted outside the published timetable, e.g. for an event or a disruption.'),
    ('EC',   N'EuroCity',
             N'International service to a neighbouring country.'),
    ('ECD',  N'EuroCity Direct',
             N'Amsterdam-Brussels direct service operated jointly with NS.'),
    ('EUR',  N'Eurostar',
             N'High-speed international service (formerly Thalys/Eurostar) to Paris, London, Amsterdam or Cologne.'),
    ('ICE',  N'ICE',
             N'Deutsche Bahn high-speed service to Frankfurt/Cologne.'),
    ('TGV',  N'TGV',
             N'SNCF high-speed service.'),
    ('THA',  N'Thalys',
             N'Legacy Thalys branding, retained because historical rows may still carry it.'),
    ('T',    N'Other scheduled train',
             N'Published by the feed without a recognised service class. Not an error: the operator uses it for services that do not fit the standard products. Treated as its own family rather than guessed at.'),
    ('CHAR', N'Charter',
             N'Chartered or special-purpose train, not part of the public timetable.'),
    ('TRN',  N'Unclassified',
             N'iRail placeholder for a train whose class the upstream feed did not report.'),
    ('BUS',  N'Replacement bus',
             N'Road service substituting for a train, typically during engineering works.')
) AS source (type_code, label, description)
    ON target.type_code = source.type_code
WHEN MATCHED THEN UPDATE SET
    target.label       = source.label,
    target.description = source.description,
    target.is_seeded   = 1
WHEN NOT MATCHED BY TARGET THEN
    INSERT (type_code, label, description, is_seeded)
    VALUES (source.type_code, source.label, source.description, 1);
GO
