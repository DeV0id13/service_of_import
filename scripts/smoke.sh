#!/bin/sh
set -eu

API_URL=${API_URL:-http://localhost:8000}
SMOKE_TIMEOUT_SECONDS=${SMOKE_TIMEOUT_SECONDS:-90}
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/service-import-smoke.XXXXXX")
RUN_TOKEN="$(date +%s)-$$"
WAREHOUSE_CODE="DEMO-BASIC-$RUN_TOKEN"

cleanup() {
    rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT HUP INT TERM

fail() {
    printf 'smoke failed: %s\n' "$1" >&2
    exit 1
}

curl_upload_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$1"
    else
        printf '%s\n' "$1"
    fi
}

json_id() {
    docker compose exec -T api python -c \
        'import json, sys; print(json.load(sys.stdin)["id"])'
}

json_status() {
    docker compose exec -T api python -c \
        'import json, sys; print(json.load(sys.stdin)["status"])'
}

wait_for_status() {
    report_id=$1
    expected_status=$2
    started_at=$(date +%s)
    response_file="$TMP_DIR/report-$report_id.json"

    while :; do
        http_code=$(curl -sS -o "$response_file" -w '%{http_code}' \
            "$API_URL/api/v1/reports/$report_id")
        [ "$http_code" = "200" ] || fail "report $report_id returned HTTP $http_code"
        status=$(json_status < "$response_file")
        if [ "$status" = "$expected_status" ]; then
            return 0
        fi
        if [ "$status" = "completed" ] || [ "$status" = "failed" ]; then
            fail "report $report_id reached $status instead of $expected_status"
        fi
        now=$(date +%s)
        if [ $((now - started_at)) -ge "$SMOKE_TIMEOUT_SECONDS" ]; then
            fail "timeout waiting for report $report_id"
        fi
        sleep 1
    done
}

readiness_code=$(curl -sS -o "$TMP_DIR/readiness.json" -w '%{http_code}' \
    "$API_URL/health/ready")
[ "$readiness_code" = "200" ] || fail "readiness returned HTTP $readiness_code"

valid_file="$TMP_DIR/valid-$RUN_TOKEN.csv"
sed "s/DEMO-BASIC/$WAREHOUSE_CODE/g" \
    "$ROOT_DIR/examples/csv/valid_basic.csv" > "$valid_file"
valid_upload_path=$(curl_upload_path "$valid_file")
valid_response="$TMP_DIR/valid-upload.json"
valid_upload_code=$(curl -sS -o "$valid_response" -w '%{http_code}' \
    -F "file=@$valid_upload_path;type=text/csv" "$API_URL/api/v1/reports")
[ "$valid_upload_code" = "202" ] || fail "valid upload returned HTTP $valid_upload_code"
valid_report_id=$(json_id < "$valid_response")
wait_for_status "$valid_report_id" completed

stocks_response="$TMP_DIR/stocks.json"
stocks_code=$(curl -sS -G -o "$stocks_response" -w '%{http_code}' \
    --data-urlencode "warehouse_code=$WAREHOUSE_CODE" "$API_URL/api/v1/stocks")
[ "$stocks_code" = "200" ] || fail "stocks returned HTTP $stocks_code"
docker compose exec -T api python -c \
    'import json, sys; payload=json.load(sys.stdin); assert payload["total"] == 3' \
    < "$stocks_response" || fail "valid report did not create three stock balances"

valid_download="$TMP_DIR/valid-downloaded.csv"
valid_download_code=$(curl -sS -o "$valid_download" -w '%{http_code}' \
    "$API_URL/api/v1/reports/$valid_report_id/original")
[ "$valid_download_code" = "200" ] || fail "valid original returned HTTP $valid_download_code"
cmp "$valid_file" "$valid_download" || fail "valid original differs from uploaded bytes"

invalid_file="$ROOT_DIR/examples/csv/invalid_negative_quantity.csv"
invalid_upload_path=$(curl_upload_path "$invalid_file")
invalid_response="$TMP_DIR/invalid-upload.json"
invalid_upload_code=$(curl -sS -o "$invalid_response" -w '%{http_code}' \
    -F "file=@$invalid_upload_path;type=text/csv" "$API_URL/api/v1/reports")
[ "$invalid_upload_code" = "202" ] || fail "invalid upload returned HTTP $invalid_upload_code"
invalid_report_id=$(json_id < "$invalid_response")
wait_for_status "$invalid_report_id" failed

errors_response="$TMP_DIR/errors.json"
errors_code=$(curl -sS -o "$errors_response" -w '%{http_code}' \
    "$API_URL/api/v1/reports/$invalid_report_id/errors")
[ "$errors_code" = "200" ] || fail "report errors returned HTTP $errors_code"
docker compose exec -T api python -c \
    'import json, sys; payload=json.load(sys.stdin); assert payload["total"] > 0' \
    < "$errors_response" || fail "failed report has no stored errors"

invalid_download="$TMP_DIR/invalid-downloaded.csv"
invalid_download_code=$(curl -sS -o "$invalid_download" -w '%{http_code}' \
    "$API_URL/api/v1/reports/$invalid_report_id/original")
[ "$invalid_download_code" = "200" ] || fail "failed original returned HTTP $invalid_download_code"
cmp "$invalid_file" "$invalid_download" || fail "failed original differs from uploaded bytes"

printf 'VALID_REPORT_ID=%s\n' "$valid_report_id"
printf 'INVALID_REPORT_ID=%s\n' "$invalid_report_id"
printf 'smoke passed\n'
