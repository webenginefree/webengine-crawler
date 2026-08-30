#!/usr/bin/env bash
# Lanceur : ./webengine.sh crawl https://monsite.fr
cd "$(dirname "$0")" && exec python3 -m webengine "$@"
