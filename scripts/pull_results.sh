#!/usr/bin/env bash
# Copy the newest bench run (or the one matching $1) from the bench-results branch into reports/
set -e
cd "$(dirname "$0")/.."
git fetch -q origin +refs/heads/bench-results:refs/remotes/origin/bench-results
RUN=$(git ls-tree --name-only origin/bench-results runs/ | grep -E "${1:-.}" | tail -1)
rm -rf reports && mkdir -p reports
for f in $(git ls-tree -r --name-only origin/bench-results "$RUN"); do
  mkdir -p "reports/$(dirname "${f#$RUN/}")"; git show "origin/bench-results:$f" > "reports/${f#$RUN/}"
done
echo "$RUN" > reports/RUN
ls reports
