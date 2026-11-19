-- Two small inserts into a local Iceberg table: two data files, three metadata files.
SET allow_insert_into_iceberg = 1;
CREATE TABLE t (a Int32) ENGINE = IcebergLocal('/var/lib/clickhouse/user_files/t/', 'Parquet');
INSERT INTO t VALUES (1);
INSERT INTO t VALUES (2);
