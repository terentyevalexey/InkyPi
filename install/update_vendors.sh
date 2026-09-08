#!/usr/bin/env bash

set -euo pipefail

# Versions
SELECT2_VERSION="4.1.0-beta.1"
FULLCALENDAR_VERSION="6.1.17"
JQUERY_VERSION="3.6.0"

# Define vendor files: name | url | output path
declare -a VENDORS=(
  "Select2 CSS|https://cdnjs.cloudflare.com/ajax/libs/select2/${SELECT2_VERSION}/css/select2.min.css|src/static/styles/select2.min.css"
  "Select2 JS|https://cdnjs.cloudflare.com/ajax/libs/select2/${SELECT2_VERSION}/js/select2.min.js|src/static/scripts/select2.min.js"
  "jQuery|https://code.jquery.com/jquery-${JQUERY_VERSION}.min.js|src/static/scripts/jquery.min.js"
  "Chart JS|https://cdn.jsdelivr.net/npm/chart.js|src/static/scripts/chart.js"
  "Fullcalendar JS|https://cdn.jsdelivr.net/npm/fullcalendar@${FULLCALENDAR_VERSION}/index.global.min.js|src/static/scripts/calendar.min.js"
)

# The main bundle ships the English locale only, so date names come from Intl but UI
# strings such as "all-day" stay untranslated without a locale file. These are fetched
# one file per language rather than as the combined 79-locale bundle, because a render
# loads exactly one of them and parsing the rest is wasted work on a slow device.
LOCALE_DIR="src/static/scripts/fc-locales"
LOCALE_BASE="https://cdn.jsdelivr.net/npm/@fullcalendar/core@${FULLCALENDAR_VERSION}/locales"
LOCALE_CODES=$(sed -n '/^LOCALE_MAP/,/^}/p' src/plugins/calendar/constants.py |
  sed -n 's/^ *"\([a-z][a-z0-9-]*\)".*/\1/p')

# Download each vendor file
for vendor in "${VENDORS[@]}"; do
  IFS='|' read -r name url output <<< "$vendor"
  echo "Updating $name..."
  if curl -sL "$url" -o "$output"; then
    echo "  ✓ Downloaded to $output"
  else
    echo "  ✗ Failed to download $name" >&2
    exit 1
  fi
done

echo "Updating Fullcalendar locales..."
mkdir -p "$LOCALE_DIR"
locale_failures=0
for code in $LOCALE_CODES; do
  if ! curl -sL --fail "$LOCALE_BASE/${code}.global.min.js" \
       -o "$LOCALE_DIR/${code}.global.min.js"; then
    locale_failures=$((locale_failures + 1))
    rm -f "$LOCALE_DIR/${code}.global.min.js"
  fi
done
if [ "$locale_failures" -gt 0 ]; then
  echo "  ! $locale_failures locale(s) unavailable; those languages fall back to English" >&2
fi
echo "  ✓ Locales in $LOCALE_DIR"

echo "All vendor files updated."
