#!/bin/bash

read -p "Please type a short commit description here: " desc

if [ -z "$desc" ]; then
  >&2 echo "You did not add a commit description, please run again !"
  exit 1
fi

git add .
git commit -m "$desc"
git push

