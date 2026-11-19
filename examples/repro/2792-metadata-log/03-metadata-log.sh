# chlab: node clickhouse1
# The newest metadata file must not name itself as the last metadata-log entry.
cd /var/lib/clickhouse/user_files/t/metadata
latest=$(ls | grep -E '^v[0-9]+.*\.metadata\.json$' | sort -V | tail -1)
last=$(tr -d '\n ' < "$latest" | grep -o '"metadata-log":\[[^]]*\]' \
  | grep -o '"metadata-file":"[^"]*' | tail -1 | sed 's#.*/##')
if [ "$last" = "$latest" ]; then
  echo "$latest lists itself in metadata-log"
else
  echo "metadata-log ends with the previous file"
fi
