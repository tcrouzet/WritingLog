#!/bin/bash

#chmod +x git.sh
current_date=$(date +"%Y-%m-%d %H:%M:%S")
git add .
if ! git diff --cached --quiet; then
  git commit -m "sync $current_date"
fi
git push -u origin main
